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
        "https://geek.digit.in/community/threads/new-monitor-unable-to-show-correct-resolution.137002/" \
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


def checkpoint_postfix(succeeded: int, failed: int, skipped: int, backup_every: int, no_backup: bool) -> str:
    """
    Build a compact, fixed-order status string for tqdm.set_postfix_str() so
    the live progress bar shows, at a glance: how many more successful
    scrapes are needed before the next backup checkpoint fires, plus running
    succeeded/failed/skipped counts.

    A plain string (rather than tqdm.set_postfix()'s dict form) gives full
    control over both ordering and length: next_bkup is listed first so it
    survives being cut off first on a narrow terminal, and short labels with
    no extra separators keep the whole line as short as possible.
    """
    if no_backup or backup_every <= 0:
        next_backup = "off"
    else:
        next_backup = f"in {backup_every - (succeeded % backup_every)}"
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


def clear_existing_posts(conn: sqlite3.Connection, thread_id: int):
    """Re-scraping the same thread replaces its posts rather than duplicating them."""
    conn.execute("DELETE FROM posts WHERE thread_id = ?", (thread_id,))
    conn.commit()


def save_posts(conn: sqlite3.Connection, thread_id: int, posts: list):
    conn.executemany(
        """
        INSERT INTO posts (
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
                 session: requests.Session, assets_dir: str, cache: dict):
    soup = BeautifulSoup(html, "html.parser")
    posts = soup.select("article.message")
    results = []

    for offset, post in enumerate(posts):
        post_number = start_post_number + offset
        site_post_id = post.get("id") or post.get("data-content") or f"page{page_number}-{offset + 1}"

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

    return results


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def scrape_one_thread(thread_url: str, conn: sqlite3.Connection, session: requests.Session,
                       image_cache: dict, assets_dir: str, delay: float, thread_pbar=None) -> bool:
    """
    Scrape a single thread URL into the given (already-open) database connection.
    If thread_pbar is given (the outer, thread-level progress bar), its
    description is updated to show which thread is currently being scraped.
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
    clear_existing_posts(conn, thread_id)

    next_post_number = 1
    any_page_failed = False
    with tqdm(total=last_page, desc="  Pages", unit="page", position=1, leave=False, dynamic_ncols=True) as page_pbar:
        for page_number in range(1, last_page + 1):
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
            posts = parse_posts(html, url, page_number, next_post_number, session, assets_dir, image_cache)
            save_posts(conn, thread_id, posts)
            log(f"  Saved {len(posts)} post(s) from page {page_number}.")
            next_post_number += len(posts)
            page_pbar.update(1)

    if any_page_failed:
        log(f"Done with {base_url}, but with 1+ pages skipped — {next_post_number - 1} post(s) saved. Marking for retry.\n")
        mark_thread_error(conn, base_url, title=title)
        return False

    log(f"Done with {base_url} — {next_post_number - 1} post(s) saved.\n")
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
        help="Only meaningful with --from-db: re-scrape every thread even if its last_scrape_status "
        "is already 'ok' (by default, --from-db skips those threads).",
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
            thread_pbar.set_postfix_str(checkpoint_postfix(succeeded, failed, skipped, args.backup_every, args.no_backup))
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
                        url, conn, session, image_cache, args.assets_dir, args.delay, thread_pbar=thread_pbar
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
                    checkpoint_postfix(succeeded, failed, skipped, args.backup_every, args.no_backup)
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
