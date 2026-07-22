#!/usr/bin/env python3
"""
Scrape a XenForo forum thread into a structured SQLite database.

Unlike download_forum_thread.py (which saves raw/self-contained HTML pages),
this script parses each individual post into structured fields — author,
avatar, profile link, timestamp, and content (kept as HTML so formatting,
links, and quotes survive) — and stores them in a SQLite database. That lets
you later rebuild the thread in a completely different design via
render_thread.py, without re-scraping.

Accepts a single thread URL, a CSV file listing one thread URL per line (no
header needed), or --from-db to read the list of thread URLs directly from
the `threads` table of the target database (the `url` column). In every
case, scraped posts are written into the `posts` table of --db. When a
threads table row for a URL already exists (e.g. it was populated by a
separate indexing/crawl step, as in --from-db mode), that row is reused
as-is to link posts to it — only a `last_scraped_at` / `last_scrape_status`
tracking column is added/updated, no other columns are touched. All
avatar/image downloads are deduplicated across threads.

With --from-db, the thread list can be filtered down with --poster and/or
--thread-id (each repeatable), matching the `poster` / `thread_id` columns
of the threads table. Threads whose last_scrape_status is already 'ok' are
skipped by default in --from-db mode; pass --rescrape-all to re-scrape them
anyway. The same status-based skip is available in single-URL/--csv mode via
--skip-existing.

Shows two live progress bars: one tracking overall thread progress (useful
in CSV/db batch modes), and one tracking page progress within the thread
currently being scraped. Requires the `tqdm` package (pip install tqdm).

Usage:
    python scrape_thread_to_db.py <thread_url> [--db forum_data.db] \
        [--assets-dir forum_assets] [--delay 1.0]

    python scrape_thread_to_db.py --csv threads.csv [--db forum_data.db] \
        [--assets-dir forum_assets] [--delay 1.0]

    python scrape_thread_to_db.py --from-db --db forum_index.db \
        [--assets-dir forum_assets] [--delay 1.0] \
        [--poster NAME ...] [--thread-id ID ...] [--rescrape-all]

Example:
    python scrape_thread_to_db.py \
        "FORUM_THREAD_URL" \
        --db forum_data.db --assets-dir forum_assets

    python scrape_thread_to_db.py --csv threads.csv --db forum_data.db --assets-dir forum_assets

    python scrape_thread_to_db.py --from-db --db forum_index.db --assets-dir forum_assets

    python scrape_thread_to_db.py --from-db --db forum_index.db --poster johndoe --poster janedoe

    python scrape_thread_to_db.py --from-db --db forum_index.db --thread-id 137002 --thread-id 140120
"""

import argparse
import csv
import hashlib
import mimetypes
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}


def log(message: str = "") -> None:
    """
    Print a status message without corrupting any active tqdm progress bars,
    prefixed with a wall-clock timestamp (matching the external log file) so
    it's clear when each step happened while watching the console live.
    """
    if message == "":
        tqdm.write("")
        return
    ts = datetime.now().strftime("%H:%M:%S")
    tqdm.write(f"[{ts}] {message}")


def default_log_file_path(db_path: str) -> str:
    """Default external log file: '<db_stem>_scrape_log.txt' next to the database."""
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    stem = os.path.splitext(os.path.basename(db_path))[0]
    return os.path.join(db_dir, f"{stem}_scrape_log.txt")


def log_to_file(log_file_path: str, message: str) -> None:
    """
    Append a timestamped line to the external run-log text file. Failures to
    write the log (e.g. permissions) are reported to the console but never
    abort the scrape itself.
    """
    if not log_file_path:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_file_path)) or ".", exist_ok=True)
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError as exc:
        log(f"  Warning: could not write to log file {log_file_path}: {exc}")


def checkpoint_postfix(
    succeeded: int, failed: int, skipped: int, backup_every: int, no_backup: bool, remaining: int
) -> str:
    """
    Build a compact, fixed-order status string for tqdm.set_postfix_str() so
    the live progress bar shows, at a glance: how many more successful
    scrapes are needed before the next backup checkpoint fires, plus running
    succeeded/failed/skipped counts.

    A plain string (rather than tqdm.set_postfix()'s dict form) gives full
    control over both ordering and length: next_bkup is listed first so it
    survives being cut off first on a narrow terminal, and short labels with
    no extra separators keep the whole line as short as possible.

    remaining: how many not-yet-attempted threads are left in this run. The
    countdown is capped at this value so a small batch (e.g. 5 threads left
    with --backup-every 200) shows an honest "in 5" instead of "in 200" — a
    checkpoint that will never actually fire before the run ends.
    """
    if no_backup or backup_every <= 0:
        next_backup = "off"
    else:
        next_backup = f"in {min(backup_every - (succeeded % backup_every), remaining)}"
    return f"next_bkup={next_backup} ok={succeeded} fail={failed} skip={skipped}"


def backup_database(conn: sqlite3.Connection, db_path: str) -> str:
    """
    Take a point-in-time snapshot of the live database into a uniquely
    timestamped copy in the same directory as db_path. Uses SQLite's own
    online backup API (Connection.backup) rather than a raw file copy, so the
    snapshot is always structurally consistent even if it's taken while the
    connection is open/mid-transaction — a plain file copy could otherwise
    capture a half-written file and produce a corrupt backup.

    Returns the path to the backup file.
    """
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    db_name = os.path.basename(db_path)
    stem, ext = os.path.splitext(db_name)
    ext = ext or ".db"
    # Microsecond precision keeps the filename unique even if two backups
    # somehow get triggered within the same second.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(db_dir, f"{stem}_backup_{timestamp}{ext}")

    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.commit()  # flush any pending writes before snapshotting
        conn.backup(backup_conn)
    finally:
        backup_conn.close()

    return backup_path


# --------------------------------------------------------------------------
# Fetching / pagination helpers (same approach as download_forum_thread.py)
# --------------------------------------------------------------------------

def normalize_base_url(thread_url: str) -> str:
    thread_url = thread_url.strip()
    thread_url = re.sub(r"/page-\d+/?$", "/", thread_url)
    if not thread_url.endswith("/"):
        thread_url += "/"
    return thread_url


def get_last_page_number(soup: BeautifulSoup, base_url: str) -> int:
    """
    Determine how many pages this thread has, using ONLY the thread's own
    pagination controls.

    Deliberately scoped, rather than scanning the whole page: forum pages
    commonly include sidebar/footer widgets (e.g. "Similar threads",
    "Latest activity", "New posts") that link to OTHER threads — some of
    which can be extremely long (this forum has threads with 1000+ pages).
    An unscoped href/text search can misread one of those unrelated links or
    phrases as this thread's own page count, making a genuinely single-page
    thread appear to have hundreds or thousands of pages.
    """
    thread_path = urlparse(base_url).path  # e.g. /community/threads/some-thread.90667/
    max_page = 1

    for a in soup.find_all("a", href=True):
        href_path = urlparse(urljoin(base_url, a["href"])).path
        if not href_path.startswith(thread_path):
            continue  # belongs to a different thread/page entirely — ignore it
        m = re.search(r"/page-(\d+)/?$", href_path)
        if m:
            max_page = max(max_page, int(m.group(1)))

    # "Page X of Y"-style text is only trusted inside an actual pagination
    # control (XenForo's pageNav/pageNavSimple/pageNavWrapper elements), not
    # the whole page body — a page-wide search can false-positive on
    # unrelated "N of M" phrases (star ratings, "showing X of Y members",
    # poll results, etc.).
    nav = soup.select_one(".pageNav, .pageNavSimple, .pageNavWrapper")
    if nav:
        text_match = re.search(r"\b\d+\s+of\s+(\d+)\b", nav.get_text())
        if text_match:
            max_page = max(max_page, int(text_match.group(1)))

    return max_page


def page_url(base_url: str, page_num: int) -> str:
    if page_num == 1:
        return base_url
    return urljoin(base_url, f"page-{page_num}")


def _retry_after_seconds(resp: requests.Response, fallback: float) -> float:
    """
    Parse a Retry-After header (seconds or an HTTP-date) if the server sent
    one; otherwise fall back to our own backoff delay.
    """
    header = resp.headers.get("Retry-After")
    if not header:
        return fallback
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        target = parsedate_to_datetime(header)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        seconds = (target - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0.0)
    except (TypeError, ValueError):
        return fallback


def _request_with_backoff(url: str, session: requests.Session, retries: int = 3, delay: float = 2.0):
    """
    GET a URL with retry/backoff. Ordinary failures (timeouts, connection
    errors, 4xx/5xx other than rate-limiting) back off by a flat `delay`.
    A 429 or 503 response is treated as an explicit "slow down" signal: we
    honor the server's Retry-After header when present, or otherwise back
    off more aggressively (delay * attempt) than for a plain failure.
    Returns the requests.Response on success, or None after exhausting retries.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            log(f"  Attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt < retries:
                time.sleep(delay)
            continue

        if resp.status_code in (429, 503):
            wait = _retry_after_seconds(resp, fallback=delay * attempt)
            log(
                f"  Rate limited (HTTP {resp.status_code}) on {url}; "
                f"waiting {wait:.1f}s before retry {attempt}/{retries}..."
            )
            if attempt < retries:
                time.sleep(wait)
            continue

        try:
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            log(f"  Attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt < retries:
                time.sleep(delay)

    return None


def fetch(url: str, session: requests.Session, retries: int = 3, delay: float = 2.0):
    resp = _request_with_backoff(url, session, retries=retries, delay=delay)
    return resp.text if resp is not None else None


def slug_from_url(url: str) -> str:
    m = re.search(r"/threads/([^/]+?)/?$", url)
    return m.group(1) if m else "thread"


def read_urls_from_db(
    conn: sqlite3.Connection, posters: list = None, thread_ids: list = None, exclude_ok: bool = False
) -> list:
    """
    Read the list of thread URLs to scrape from the `threads` table of the
    already-open database connection (the `url` column), ordered by id so
    runs are stable/reproducible.

    posters: optional list of poster names to filter on (case-insensitive
        exact match against the `poster` column). A thread matching ANY of
        the given posters is included.
    thread_ids: optional list of values to filter on the `thread_id` column
        (the forum's own thread id, as opposed to this table's internal
        autoincrement `id`). A thread matching ANY of the given ids is
        included.
    exclude_ok: if True, threads whose last_scrape_status is already 'ok'
        are excluded directly in the SQL query, so they're never loaded into
        memory, iterated over, or logged as "skipping" one by one — the
        returned list only ever contains threads that actually need work.

    If both posters and thread_ids are given, only threads matching at least
    one poster AND at least one thread_id are included.
    """
    query = "SELECT url FROM threads WHERE 1=1"
    params = []

    if posters:
        placeholders = ", ".join("?" for _ in posters)
        query += f" AND poster COLLATE NOCASE IN ({placeholders})"
        params.extend(posters)

    if thread_ids:
        placeholders = ", ".join("?" for _ in thread_ids)
        query += f" AND thread_id IN ({placeholders})"
        params.extend(thread_ids)

    if exclude_ok:
        query += " AND (last_scrape_status IS NULL OR last_scrape_status != 'ok')"

    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()
    return [r[0] for r in rows if r[0]]


def read_urls_from_csv(csv_path: str) -> list:
    """
    Read one thread URL per line from a CSV file. Each line is expected to
    contain a single URL (optionally quoted); blank lines are skipped.
    """
    urls = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            url = row[0].strip()
            if url:
                urls.append(url)
    return urls


# --------------------------------------------------------------------------
# Asset (avatar / inline image) downloading
# --------------------------------------------------------------------------

def local_asset_name(url: str) -> str:
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "file"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    root, ext = os.path.splitext(base)
    if not ext:
        guessed = mimetypes.guess_type(parsed.path)[0]
        ext = mimetypes.guess_extension(guessed) if guessed else ""
        ext = ext or ""
    return f"{root}_{digest}{ext}"


def download_binary_asset(url: str, session: requests.Session, assets_dir: str, cache: dict):
    """Download an image (avatar or inline post image) if not already cached."""
    if not url:
        return None
    if url in cache:
        return cache[url]

    resp = _request_with_backoff(url, session)
    if resp is None:
        log(f"    Failed to fetch image {url}")
        cache[url] = None
        return None

    filename = local_asset_name(url)
    local_path = os.path.join(assets_dir, filename)
    with open(local_path, "wb") as f:
        f.write(resp.content)

    rel_path = f"{os.path.basename(assets_dir)}/{filename}"
    cache[url] = rel_path
    return rel_path


def localize_post_images(body_tag, page_url_str: str, session: requests.Session, assets_dir: str, cache: dict):
    """Rewrite <img> tags inside a post body to point at locally downloaded copies."""
    for img in body_tag.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-url")
        if not src:
            continue
        absolute = urljoin(page_url_str, src)
        local_rel = download_binary_asset(absolute, session, assets_dir, cache)
        if local_rel:
            img["src"] = local_rel
            for junk_attr in ("data-src", "data-url", "srcset"):
                if img.has_attr(junk_attr):
                    del img[junk_attr]
    return body_tag


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            page_count INTEGER,
            scraped_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            page_number INTEGER,
            post_number INTEGER,
            site_post_id TEXT,
            author TEXT,
            author_profile_url TEXT,
            avatar_local_path TEXT,
            timestamp_iso TEXT,
            timestamp_display TEXT,
            content_html TEXT,
            FOREIGN KEY (thread_id) REFERENCES threads (id)
        )
        """
    )
    _ensure_scrape_tracking_columns(conn)
    _ensure_posts_unique_index(conn)
    conn.commit()
    return conn


def _ensure_scrape_tracking_columns(conn: sqlite3.Connection) -> None:
    """
    Make sure the threads table (whatever its origin — freshly created by
    this script, or a pre-existing/richer table like the one produced by a
    separate forum indexer) has columns to record scrape progress, without
    touching any of its other columns.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "last_scraped_at" not in cols:
        conn.execute("ALTER TABLE threads ADD COLUMN last_scraped_at TEXT")
    if "last_scrape_status" not in cols:
        conn.execute("ALTER TABLE threads ADD COLUMN last_scrape_status TEXT")


def _ensure_posts_unique_index(conn: sqlite3.Connection) -> None:
    """
    Ensure (thread_id, site_post_id) is unique, so save_posts can INSERT OR
    IGNORE and let already-stored posts fall through untouched instead of
    duplicating them on a re-scrape. The pair is scoped by thread_id on
    purpose: XenForo post ids are unique within a thread, but the same id can
    legitimately appear across two different thread rows (e.g. a thread reached
    under two URLs), so a global unique on site_post_id alone would wrongly
    collide.

    Best-effort: if the table already holds duplicate (thread_id, site_post_id)
    rows from an older run that inserted blindly, the index can't be created —
    we warn with a remedy rather than aborting the whole run, and dedup simply
    won't be enforced until those rows are cleaned up.
    """
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_thread_site "
            "ON posts (thread_id, site_post_id)"
        )
    except sqlite3.Error as exc:
        log(
            f"  Warning: could not create unique index on posts(thread_id, site_post_id): {exc}. "
            "Existing duplicate posts must be removed first; re-scrapes may duplicate posts until then."
        )


def thread_marked_scraped_ok(conn: sqlite3.Connection, url: str) -> bool:
    """
    True if this URL has a threads row whose last_scrape_status is 'ok' —
    i.e. it was fully scraped successfully on a previous run. Threads that
    were never scraped, or whose last attempt failed (last_scrape_status
    'error'), are not considered already-scraped.
    """
    row = conn.execute("SELECT last_scrape_status FROM threads WHERE url = ?", (url,)).fetchone()
    return row is not None and row[0] == "ok"


def _get_or_create_thread_row(conn: sqlite3.Connection, url: str, title: str, page_count) -> int:
    """
    Get the id of the threads row for `url`, creating it if needed. Does NOT
    touch last_scraped_at/last_scrape_status — callers stamp those explicitly
    via mark_thread_ok/mark_thread_error once the actual outcome is known.

    If a row for this URL already exists — e.g. it was populated ahead of
    time by an indexing step, possibly in a table with columns this script
    doesn't know about (no `page_count`, extra metadata fields, etc.) — it is
    left completely untouched. Otherwise a new row is inserted using only the
    columns that actually exist in this database's threads table.
    """
    row = conn.execute("SELECT id FROM threads WHERE url = ?", (url,)).fetchone()
    if row is not None:
        return row[0]

    now = datetime.now(timezone.utc).isoformat()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    candidate_fields = {
        "url": url,
        "title": title,
        "page_count": page_count,
        "scraped_at": now,
    }
    fields = {k: v for k, v in candidate_fields.items() if k in cols and v is not None}
    # `title` is NOT NULL on some schemas (e.g. a richly pre-populated
    # forum_index.db) — fall back to the URL itself if we don't know the
    # real title yet (e.g. we're creating this row purely to record a
    # page-1 fetch failure).
    if "title" in cols and "title" not in fields:
        fields["title"] = title or url
    colnames = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO threads ({colnames}) VALUES ({placeholders})", tuple(fields.values()))
    conn.commit()
    row = conn.execute("SELECT id FROM threads WHERE url = ?", (url,)).fetchone()
    return row[0]


def _stamp_thread_status(conn: sqlite3.Connection, url: str, status: str, title: str = None, page_count=None) -> None:
    """Create the threads row if needed, then set last_scraped_at/last_scrape_status."""
    thread_id = _get_or_create_thread_row(conn, url, title, page_count)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE threads SET last_scraped_at = ?, last_scrape_status = ? WHERE id = ?",
        (now, status, thread_id),
    )
    conn.commit()


def mark_thread_ok(conn: sqlite3.Connection, url: str) -> None:
    """
    Record that this thread was scraped completely and successfully — every
    page fetched and saved with no errors. Only call this once the entire
    scrape of the thread has actually finished; see mark_thread_error for the
    "anything went wrong" case.
    """
    _stamp_thread_status(conn, url, "ok")


def mark_thread_error(conn: sqlite3.Connection, url: str, title: str = None) -> None:
    """
    Record a failed (or incomplete) scrape attempt. Creates the threads row
    if it doesn't exist yet (e.g. a brand-new single-URL/--csv target that
    failed on its very first page fetch), using `title` as a placeholder if
    the real title isn't known yet — falls back to the URL itself.
    """
    _stamp_thread_status(conn, url, "error", title=title or url)


def upsert_thread(conn: sqlite3.Connection, url: str, title: str, page_count: int) -> int:
    """
    Get the id of the threads row for `url`, creating it if needed. Does NOT
    mark it as scraped-ok — see mark_thread_ok/mark_thread_error, which are
    called once the real outcome is known.

    If a row for this URL already exists — e.g. it was populated ahead of
    time by an indexing step, possibly in a table with columns this script
    doesn't know about (no `page_count`, extra metadata fields, etc.) — it is
    left completely untouched. Otherwise a new row is inserted using only the
    columns that actually exist in this database's threads table.
    """
    return _get_or_create_thread_row(conn, url, title, page_count)


def mark_threads_with_new_posts(conn: sqlite3.Connection, include_closed: bool = False) -> int:
    """
    Find already-scraped ('ok') threads that have GAINED posts since they were
    archived, and flip their last_scrape_status to 'stale' so a subsequent
    --from-db run (which skips 'ok' threads) picks them up for a full re-scrape.

    "Gained posts" is decided by comparing the forum's own reply count — the
    `replies` column, which a re-run of the indexer refreshes in place from the
    forum's live thread listings — against how many rows we actually hold in
    the `posts` table for that thread. XenForo's `replies` counts replies only
    (it excludes the opening post), so a fully-archived thread satisfies
    `replies + 1 == posts stored`. A thread is only flagged when
    `replies + 1 > posts stored`, i.e. the forum now has MORE posts than we do.
    Threads where we hold more than the forum reports (posts deleted upstream)
    are deliberately left alone — this only chases growth, never shrinkage.

    Requires a `replies` column on the threads table (present in the richer
    index database produced by the forum indexer, absent from a bare
    scrape-only database) — raises ValueError if it isn't there.

    include_closed: by default, threads marked closed/locked upstream (the
        `is_closed` column) are EXCLUDED even if their count looks grown, on
        the assumption a locked thread can't legitimately accept new posts —
        this keeps an accidental run from re-scraping the tens of thousands of
        closed threads. Pass True to check closed threads too. Has no effect
        if the table has no `is_closed` column.

    Returns the number of threads newly flagged as 'stale'.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "replies" not in cols:
        raise ValueError(
            "--check-updates needs a `replies` column on the threads table (the forum's reply "
            f"count), which {conn} does not have. Point --db at the richer index database (the one "
            "produced by the forum indexer), and re-run the indexer first so `replies` reflects the "
            "forum's current counts."
        )

    # A single-pass aggregate of stored posts-per-thread, keyed for O(log n)
    # correlated lookups below. Much cheaper than a correlated COUNT(*) over
    # the ~1.8M-row posts table once per thread.
    conn.execute("DROP TABLE IF EXISTS _post_counts")
    conn.execute("CREATE TEMP TABLE _post_counts (thread_id INTEGER PRIMARY KEY, n INTEGER)")
    conn.execute("INSERT INTO _post_counts (thread_id, n) SELECT thread_id, COUNT(*) FROM posts GROUP BY thread_id")

    where = (
        "last_scrape_status = 'ok' "
        "AND replies IS NOT NULL "
        "AND replies + 1 > COALESCE((SELECT n FROM _post_counts WHERE thread_id = threads.id), 0)"
    )
    if not include_closed and "is_closed" in cols:
        where += " AND COALESCE(is_closed, 0) = 0"

    cur = conn.execute(f"UPDATE threads SET last_scrape_status = 'stale' WHERE {where}")
    flagged = cur.rowcount
    conn.commit()
    conn.execute("DROP TABLE IF EXISTS _post_counts")
    return flagged


def clear_existing_posts(conn: sqlite3.Connection, thread_id: int):
    """
    Delete every stored post for a thread so it can be rebuilt from scratch.
    Used only on the --rescrape-all (full rebuild) path: wiping first is what
    lets a rebuild pick up upstream EDITS to existing posts and DELETIONS,
    which the default incremental INSERT OR IGNORE path deliberately leaves
    untouched.
    """
    conn.execute("DELETE FROM posts WHERE thread_id = ?", (thread_id,))
    conn.commit()


def save_posts(conn: sqlite3.Connection, thread_id: int, posts: list) -> int:
    """
    Insert the given posts, skipping any already stored for this thread.

    Dedup is by (thread_id, site_post_id) via INSERT OR IGNORE against the
    unique index from _ensure_posts_unique_index: a post whose id we already
    hold falls through untouched, so a re-scrape only appends genuinely new
    posts rather than duplicating existing ones — no wipe-then-rebuild needed.
    (On the --rescrape-all path the thread's posts have already been cleared,
    so every row here is a fresh insert anyway.) Returns how many rows were
    actually inserted.
    """
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO posts (
            thread_id, page_number, post_number, site_post_id, author,
            author_profile_url, avatar_local_path, timestamp_iso,
            timestamp_display, content_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                thread_id,
                p["page_number"],
                p["post_number"],
                p["site_post_id"],
                p["author"],
                p["author_profile_url"],
                p["avatar_local_path"],
                p["timestamp_iso"],
                p["timestamp_display"],
                p["content_html"],
            )
            for p in posts
        ],
    )
    conn.commit()
    return conn.total_changes - before


def existing_post_ids(conn: sqlite3.Connection, thread_id: int, min_page: int = None) -> set:
    """Return the set of site_post_ids already stored for this thread, so an
    incremental re-scrape can skip re-downloading their images. When min_page
    is given, only ids on that page or later are returned — enough to cover the
    pages an incremental resume will actually re-fetch, without loading every id
    from a long thread's earlier pages."""
    if min_page is None:
        rows = conn.execute("SELECT site_post_id FROM posts WHERE thread_id = ?", (thread_id,))
    else:
        rows = conn.execute(
            "SELECT site_post_id FROM posts WHERE thread_id = ? AND page_number >= ?", (thread_id, min_page)
        )
    return {r[0] for r in rows}


def incremental_resume_point(conn: sqlite3.Connection, thread_id: int, last_page: int, prior_status: str):
    """
    Decide which page an incremental (non-rebuild) scrape should start from, and
    what post_number that page's first post should get. Returns
    (start_page, start_post_number).

    New posts on a forum thread land at the end, so a thread we already hold in
    full only needs its last stored page (which may have gained posts) and any
    pages after it re-fetched — not the whole thing. The last stored page is
    re-fetched, not skipped, because it may have been partially full last time.

    Resuming mid-thread is only safe when the previous scrape finished fully and
    contiguously — which last_scrape_status='ok' guarantees (mark_thread_ok is
    only called when no page was skipped), as does 'stale' (only ever set by
    --check-updates on a thread that was previously 'ok'). For any other status
    — a partial/failed prior attempt, or a thread never finished — earlier pages
    may have gaps, so we start from page 1 and let the page-level dedup sort it
    out.
    """
    if prior_status not in ("ok", "stale"):
        return 1, 1
    max_page = conn.execute("SELECT MAX(page_number) FROM posts WHERE thread_id = ?", (thread_id,)).fetchone()[0]
    if not max_page:
        return 1, 1
    start_page = min(max_page, last_page)
    posts_before = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE thread_id = ? AND page_number < ?", (thread_id, start_page)
    ).fetchone()[0]
    return start_page, posts_before + 1


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def get_thread_title(soup: BeautifulSoup) -> str:
    title_tag = soup.select_one("h1.p-title-value")
    if title_tag:
        return title_tag.get_text(strip=True)
    if soup.title:
        return soup.title.get_text(strip=True)
    return "Untitled thread"


def parse_posts(html: str, page_url_str: str, page_number: int, start_post_number: int,
                 session: requests.Session, assets_dir: str, cache: dict, known_ids: set = None):
    """
    Parse the posts on one thread page. Returns (new_posts, seen_count):
    new_posts is the list of post dicts NOT already stored, and seen_count is
    the total number of posts on the page (new or not).

    known_ids: site_post_ids already stored for this thread. A post whose id is
    in this set is skipped WITHOUT downloading its avatar or inline images —
    the expensive work — since we already hold it. It's still counted toward
    seen_count so the caller's running post_number stays aligned with the
    post's true position in the thread. Pass None/empty (e.g. the --rescrape-all
    rebuild path) to re-process every post.
    """
    known_ids = known_ids or set()
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("article.message")
    results = []

    for offset, post in enumerate(posts):
        post_number = start_post_number + offset
        site_post_id = post.get("id") or post.get("data-content") or f"page{page_number}-{offset + 1}"

        if site_post_id in known_ids:
            # Already stored — skip avatar/inline-image downloads and parsing.
            continue

        author = post.get("data-author")
        name_tag = post.select_one(".message-name a") or post.select_one("a.username")
        if not author:
            author = name_tag.get_text(strip=True) if name_tag else "Unknown"

        author_profile_url = None
        if name_tag and name_tag.get("href"):
            author_profile_url = urljoin(page_url_str, name_tag["href"])

        avatar_url = None
        avatar_tag = post.select_one(".message-avatar img") or post.select_one("img.avatar")
        if avatar_tag:
            avatar_src = avatar_tag.get("src") or avatar_tag.get("data-src")
            if avatar_src:
                avatar_url = urljoin(page_url_str, avatar_src)
        avatar_local_path = download_binary_asset(avatar_url, session, assets_dir, cache) if avatar_url else None

        time_tag = post.select_one("time")
        timestamp_iso = time_tag.get("datetime") if time_tag else None
        timestamp_display = None
        if time_tag:
            timestamp_display = time_tag.get("title") or time_tag.get_text(strip=True)
        timestamp_display = timestamp_display or "Unknown time"

        body = post.select_one("div.bbWrapper")
        if body:
            body = localize_post_images(body, page_url_str, session, assets_dir, cache)
            content_html = body.decode_contents()
        else:
            content_html = ""

        results.append(
            {
                "page_number": page_number,
                "post_number": post_number,
                "site_post_id": site_post_id,
                "author": author,
                "author_profile_url": author_profile_url,
                "avatar_local_path": avatar_local_path,
                "timestamp_iso": timestamp_iso,
                "timestamp_display": timestamp_display,
                "content_html": content_html,
            }
        )

    return results, len(posts)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def scrape_one_thread(thread_url: str, conn: sqlite3.Connection, session: requests.Session,
                       image_cache: dict, assets_dir: str, delay: float, thread_pbar=None,
                       rebuild: bool = False) -> bool:
    """
    Scrape a single thread URL into the given (already-open) database connection.
    If thread_pbar is given (the outer, thread-level progress bar), its
    description is updated to show which thread is currently being scraped.

    rebuild: when True (the --rescrape-all path), the thread's existing posts
    are wiped first and everything is re-inserted from scratch, so upstream
    edits and deletions are reflected. When False (the default), posts already
    stored are left in place and only new ones are appended (INSERT OR IGNORE).
    """
    base_url = normalize_base_url(thread_url)

    log(f"Fetching page 1: {base_url}")
    first_html = fetch(base_url, session)
    if first_html is None:
        log(f"Failed to fetch {base_url}. Skipping this thread.")
        mark_thread_error(conn, base_url)
        return False

    soup = BeautifulSoup(first_html, "html.parser")
    last_page = get_last_page_number(soup, base_url)
    title = get_thread_title(soup)
    log(f"Thread: {title!r} — {last_page} page(s) detected.")

    if thread_pbar is not None:
        short_title = (title[:22] + "…") if len(title) > 22 else title
        thread_pbar.set_description(f"Threads [{short_title}]")

    thread_id = upsert_thread(conn, base_url, title, last_page)
    if rebuild:
        # Full rebuild: drop the old posts so edits/deletions upstream are
        # reflected, and re-fetch every page from the start.
        clear_existing_posts(conn, thread_id)
        known_ids = set()
        start_page, next_post_number = 1, 1
    else:
        # Incremental: resume from the last page we already hold (new posts land
        # at the end of a thread) and skip re-downloading images for posts we
        # already have. Falls back to a page-1 start when the prior scrape didn't
        # finish cleanly (see incremental_resume_point).
        prior_status = conn.execute(
            "SELECT last_scrape_status FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()[0]
        start_page, next_post_number = incremental_resume_point(conn, thread_id, last_page, prior_status)
        known_ids = existing_post_ids(conn, thread_id, min_page=start_page)
        if start_page > 1:
            log(f"  Resuming from page {start_page}/{last_page} ({next_post_number - 1} earlier post(s) already stored).")

    new_posts = 0
    any_page_failed = False
    with tqdm(total=last_page - start_page + 1, desc="  Pages", unit="page", position=1, leave=False,
              dynamic_ncols=True) as page_pbar:
        for page_number in range(start_page, last_page + 1):
            url = page_url(base_url, page_number)
            if page_number == 1:
                html = first_html
            else:
                log(f"Fetching page {page_number}/{last_page}: {url}")
                time.sleep(delay)
                html = fetch(url, session)
                if html is None:
                    log(f"  Skipping page {page_number} after repeated failures.")
                    any_page_failed = True
                    page_pbar.update(1)
                    continue

            log(f"  Parsing posts on page {page_number}...")
            posts, seen = parse_posts(html, url, page_number, next_post_number, session, assets_dir, image_cache, known_ids)
            inserted = save_posts(conn, thread_id, posts)
            new_posts += inserted
            log(f"  Page {page_number}: {seen} post(s) seen, {inserted} new.")
            next_post_number += seen
            page_pbar.update(1)

    seen = next_post_number - 1
    if any_page_failed:
        log(f"Done with {base_url}, but with 1+ pages skipped — {new_posts} new post(s) saved ({seen} seen). Marking for retry.\n")
        mark_thread_error(conn, base_url, title=title)
        return False

    log(f"Done with {base_url} — {new_posts} new post(s) saved ({seen} seen).\n")
    mark_thread_ok(conn, base_url)
    return True



def format_duration(seconds: float) -> str:
    """Format a duration in seconds as e.g. '1h 02m 03s', '02m 03s', or '3.4s'."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{seconds:.1f}s"


def main():
    parser = argparse.ArgumentParser(description="Scrape one or more forum threads into a SQLite database.")
    # NOTE: thread_url is deliberately NOT registered inside this mutually_exclusive_group.
    # Some Python versions have a long-standing argparse quirk where an optional positional
    # (nargs="?") placed inside a mutually exclusive group gets incorrectly treated as "provided"
    # even when omitted, producing a false "not allowed with argument --from-db" error. Source
    # validity is instead checked manually below, right after parsing.
    parser.add_argument("thread_url", nargs="?", default=None, help="URL of a single forum thread (any page)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--csv", dest="csv_path", help="Path to a CSV file listing one thread URL per line (no header needed)"
    )
    source.add_argument(
        "--from-db",
        dest="from_db",
        action="store_true",
        help=(
            "Read the list of thread URLs from the `threads` table (url column) of --db, "
            "instead of a single URL or --csv. Scraped posts are saved back into that same "
            "database's `posts` table. By default, threads whose last_scrape_status is already "
            "'ok' are skipped; pass --rescrape-all to re-scrape everything instead. Combine with "
            "--poster/--thread-id to only scrape a subset of the threads table."
        ),
    )
    parser.add_argument("--db", default="forum_data.db", help="SQLite database file (default: forum_data.db)")
    parser.add_argument(
        "--assets-dir", default="forum_assets", help="Directory to save avatars/inline images (default: forum_assets)"
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between page requests (default: 1.0)")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip threads whose last_scrape_status is already 'ok' in the database, instead of "
        "re-scraping them. (With --from-db this is the default; see --rescrape-all.)",
    )
    parser.add_argument(
        "--rescrape-all",
        action="store_true",
        help="Force a full rebuild: each scraped thread's existing posts are wiped and re-inserted "
        "from scratch, so upstream edits and deletions are reflected (the default instead appends only "
        "new posts, leaving stored ones untouched). With --from-db this also re-scrapes threads whose "
        "last_scrape_status is already 'ok' (normally skipped).",
    )
    parser.add_argument(
        "--check-updates",
        action="store_true",
        help="Requires --from-db. Before scraping, find already-'ok' threads whose forum reply count "
        "(the `replies` column) now exceeds the number of posts stored for them, mark those as 'stale', "
        "then scrape exactly those. Re-run the forum indexer first so `replies` reflects current counts. "
        "Only threads that GAINED posts are chased; upstream deletions are ignored. Closed/locked threads "
        "are excluded by default — see --include-closed.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only meaningful with --check-updates: do the check and mark grown threads 'stale', then "
        "stop WITHOUT scraping them (review first, e.g. re-run later with a plain --from-db).",
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Only meaningful with --check-updates: also check threads flagged closed/locked upstream "
        "(the `is_closed` column). By default these are excluded, since a locked thread shouldn't gain "
        "new posts and this avoids accidentally re-scraping tens of thousands of closed threads.",
    )
    parser.add_argument(
        "--poster",
        action="append",
        default=None,
        metavar="NAME",
        help="Only valid with --from-db. Only scrape threads whose `poster` column matches NAME "
        "(case-insensitive, exact match). Repeat the flag to match multiple posters, "
        "e.g. --poster alice --poster bob.",
    )
    parser.add_argument(
        "--thread-id",
        dest="thread_ids",
        action="append",
        default=None,
        metavar="ID",
        help="Only valid with --from-db. Only scrape the thread(s) with this `thread_id` value "
        "(the forum's own thread id, as stored in the threads table — not the database's internal "
        "row id). Repeat the flag to match multiple ids, e.g. --thread-id 137002 --thread-id 140120.",
    )
    parser.add_argument(
        "--backup-every",
        type=int,
        default=200,
        metavar="N",
        help="Back up --db to a uniquely timestamped copy in the same directory after every N "
        "successfully scraped threads (default: 200). Set to 0 to disable these periodic backups "
        "(a final backup at the end of the run still happens unless --no-backup is given).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable all database backups for this run, including the final end-of-run backup.",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        metavar="PATH",
        help="Path to an external text log file recording run start/end times, duration, and each "
        "backup taken. Default: '<db_name>_scrape_log.txt' next to --db.",
    )
    args = parser.parse_args()

    sources_given = sum([bool(args.thread_url), bool(args.csv_path), args.from_db])
    if sources_given == 0:
        parser.error("one of the arguments thread_url --csv --from-db is required")
    if sources_given > 1:
        parser.error("thread_url, --csv, and --from-db are mutually exclusive — pass only one")

    if (args.poster or args.thread_ids) and not args.from_db:
        parser.error("--poster and --thread-id can only be used together with --from-db")

    if args.check_updates and not args.from_db:
        parser.error("--check-updates can only be used together with --from-db")
    if args.check_only and not args.check_updates:
        parser.error("--check-only can only be used together with --check-updates")
    if args.include_closed and not args.check_updates:
        parser.error("--include-closed can only be used together with --check-updates")

    start_time = time.perf_counter()
    log_file = args.log_file or default_log_file_path(args.db)

    if args.csv_path:
        source_desc = f"csv={args.csv_path}"
    elif args.from_db:
        filt_bits = []
        if args.poster:
            filt_bits.append(f"poster={args.poster}")
        if args.thread_ids:
            filt_bits.append(f"thread_id={args.thread_ids}")
        source_desc = "from-db" + (f" ({', '.join(filt_bits)})" if filt_bits else "")
    else:
        source_desc = f"url={args.thread_url}"
    backup_desc = "disabled" if args.no_backup else f"every {args.backup_every} threads (+ final)"
    log_to_file(log_file, f"RUN START | {source_desc} | db={args.db} | backups={backup_desc}")

    succeeded, failed, skipped = 0, 0, 0
    backups_taken = []
    conn = None
    scraping_started = False
    try:
        os.makedirs(args.assets_dir, exist_ok=True)
        session = requests.Session()
        image_cache = {}
        conn = init_db(args.db)

        # --check-updates: flip already-'ok' threads that gained posts to
        # 'stale' first, so the normal --from-db pass below (which skips 'ok')
        # then re-scrapes exactly those. Runs before the URL list is built so
        # the freshly-staled threads are the ones that get loaded.
        if args.check_updates:
            try:
                flagged = mark_threads_with_new_posts(conn, include_closed=args.include_closed)
            except ValueError as exc:
                sys.exit(str(exc))
            closed_note = "" if args.include_closed else " (closed threads excluded)"
            log(f"Update check: {flagged} already-scraped thread(s) have new posts and were marked for re-scrape{closed_note}.")
            log_to_file(log_file, f"CHECK-UPDATES | flagged={flagged} | include_closed={args.include_closed}")
            if args.check_only:
                log("--check-only given; marked threads but not scraping. Re-run with --from-db to fetch them.")
                conn.close()
                conn = None
                return

        # Decide skip-existing behavior first so already-'ok' threads can be
        # excluded before the list is even built/loaded, rather than being
        # loaded, iterated, and logged one-by-one just to be skipped.
        if args.from_db:
            # --from-db defaults to skipping already-scraped threads; --rescrape-all overrides that.
            skip_existing = not args.rescrape_all
        else:
            skip_existing = args.skip_existing

        if args.csv_path:
            all_urls = read_urls_from_csv(args.csv_path)
            if not all_urls:
                sys.exit(f"No URLs found in {args.csv_path}.")
            if skip_existing:
                urls = [u for u in all_urls if not thread_marked_scraped_ok(conn, normalize_base_url(u))]
                skipped = len(all_urls) - len(urls)
                excl_msg = f" ({skipped} already scraped and excluded)" if skipped else ""
            else:
                urls = all_urls
                excl_msg = ""
            if not urls:
                sys.exit(f"All {len(all_urls)} thread(s) in {args.csv_path} already have last_scrape_status='ok'; nothing to do.")
            log(f"Loaded {len(urls)} thread URL(s) from {args.csv_path}{excl_msg}.\n")
        elif args.from_db:
            urls = read_urls_from_db(conn, posters=args.poster, thread_ids=args.thread_ids, exclude_ok=skip_existing)
            filt_parts = []
            if args.poster:
                filt_parts.append(f"poster in {args.poster}")
            if args.thread_ids:
                filt_parts.append(f"thread_id in {args.thread_ids}")
            filt_msg = f" (filtered by {' and '.join(filt_parts)})" if filt_parts else ""
            excl_msg = ""
            if skip_existing:
                total_matching = len(read_urls_from_db(conn, posters=args.poster, thread_ids=args.thread_ids))
                skipped = total_matching - len(urls)
                if skipped:
                    excl_msg = f" ({skipped} already scraped and excluded)"
            if not urls:
                sys.exit(
                    f"No thread URLs left to scrape in the threads table of {args.db}{filt_msg}{excl_msg} "
                    "(pass --rescrape-all to re-scrape anyway)."
                )
            log(f"Loaded {len(urls)} thread URL(s) from the threads table of {args.db}{filt_msg}{excl_msg}.\n")
        else:
            if skip_existing and thread_marked_scraped_ok(conn, normalize_base_url(args.thread_url)):
                sys.exit(
                    f"{args.thread_url} already has last_scrape_status='ok' in {args.db}; nothing to do "
                    "(run without --skip-existing to re-scrape it)."
                )
            urls = [args.thread_url]

        made_previous_request = False
        scraping_started = True
        with tqdm(total=len(urls), desc="Threads", unit="thread", position=0, dynamic_ncols=True) as thread_pbar:
            thread_pbar.set_postfix_str(
                checkpoint_postfix(succeeded, failed, skipped, args.backup_every, args.no_backup, remaining=len(urls))
            )
            for i, url in enumerate(urls, start=1):
                base_url = normalize_base_url(url)

                if made_previous_request:
                    # Same politeness delay used between pages, applied between
                    # threads too, so batch runs don't hit the server back-to-back.
                    time.sleep(args.delay)
                if len(urls) > 1:
                    log(f"[{i}/{len(urls)}] {url}")
                try:
                    ok = scrape_one_thread(
                        url, conn, session, image_cache, args.assets_dir, args.delay,
                        thread_pbar=thread_pbar, rebuild=args.rescrape_all,
                    )
                except Exception as exc:
                    log(f"  Unexpected error scraping {url}: {exc}")
                    try:
                        mark_thread_error(conn, base_url)
                    except Exception as mark_exc:
                        log(f"  Warning: could not mark {base_url} as errored: {mark_exc}")
                    ok = False
                made_previous_request = True
                thread_pbar.update(1)
                if ok:
                    succeeded += 1
                    if not args.no_backup and args.backup_every > 0 and succeeded % args.backup_every == 0:
                        backup_path = backup_database(conn, args.db)
                        backups_taken.append(backup_path)
                        log(
                            f"  Backup checkpoint: {succeeded} threads scraped successfully "
                            f"({i}/{len(urls)} processed overall) -> {backup_path}"
                        )
                        log_to_file(log_file, f"BACKUP | checkpoint at {succeeded} threads | {backup_path}")
                else:
                    failed += 1
                thread_pbar.set_postfix_str(
                    checkpoint_postfix(
                        succeeded, failed, skipped, args.backup_every, args.no_backup, remaining=len(urls) - i
                    )
                )

        if not args.no_backup:
            try:
                backup_path = backup_database(conn, args.db)
                backups_taken.append(backup_path)
                log(f"Final backup: {backup_path}")
                log_to_file(log_file, f"BACKUP | end of run | {backup_path}")
            except Exception as exc:
                log(f"  Warning: final backup failed: {exc}")
                log_to_file(log_file, f"BACKUP FAILED | end of run | {exc}")

        conn.close()
        conn = None

        is_batch_mode = bool(args.csv_path or args.from_db)
        if is_batch_mode:
            log(
                f"Batch complete: {succeeded} succeeded, {failed} failed, "
                f"{skipped} skipped (already scraped). Data saved to {args.db}."
            )
        elif succeeded:
            log(f"Data saved to {args.db}. Images saved under {args.assets_dir}/")
        else:
            sys.exit(1)
    finally:
        # Best-effort safety backup + cleanup if we're unwinding due to an
        # unexpected error partway through (conn is still open in that case).
        if conn is not None:
            if not args.no_backup and scraping_started:
                try:
                    backup_path = backup_database(conn, args.db)
                    backups_taken.append(backup_path)
                    log(f"Backup taken before exit due to error: {backup_path}")
                    log_to_file(log_file, f"BACKUP | before error exit | {backup_path}")
                except Exception as exc:
                    log(f"  Warning: could not take backup before exit: {exc}")
            conn.close()

        elapsed = time.perf_counter() - start_time
        log(f"Total execution time: {format_duration(elapsed)}")
        log_to_file(
            log_file,
            f"RUN END | duration={format_duration(elapsed)} | succeeded={succeeded} failed={failed} "
            f"skipped={skipped} | backups_taken={len(backups_taken)} | db={args.db}",
        )


if __name__ == "__main__":
    main()
