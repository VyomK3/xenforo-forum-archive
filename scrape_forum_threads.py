#!/usr/bin/env python3
"""
XenForo forum indexer for xenforo based forum

Crawls the full forum tree (categories -> sub-forums -> threads) and stores
an index of every thread it finds in a local SQLite database, "forum_index.db".

Usage:
    python3 scrape_forum_threads.py                     # crawl everything, fresh run
    python3 scrape_forum_threads.py --resume            # skip forum-pages already scraped
    python3 scrape_forum_threads.py --db my_forum.db    # custom db path
    python3 scrape_forum_threads.py --delay 1.5         # seconds between requests (politeness)
    python3 scrape_forum_threads.py --max-pages 5       # cap pages per sub-forum (testing)
    python3 scrape_forum_threads.py --start-url xenforo_based_forum_URL

Requires: pip install requests beautifulsoup4
"""

import argparse
import re
import sqlite3
import sys
import time
import logging
from collections import deque
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "xenforo_based_forum_URL"
USER_AGENT = (
    "Mozilla/5.0 (compatible; ForumIndexerBot/1.0; "
    "+personal-use-index-script)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("forum_scraper")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    parent_id       INTEGER,
    top_level_name  TEXT,      -- name of the top-level category this belongs to
    depth           INTEGER NOT NULL DEFAULT 0,
    description     TEXT,
    FOREIGN KEY (parent_id) REFERENCES sections(id)
);

CREATE TABLE IF NOT EXISTS threads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id           TEXT,           -- xenforo internal thread id (from URL)
    url                 TEXT UNIQUE NOT NULL,
    title               TEXT NOT NULL,
    prefix              TEXT,
    section_id          INTEGER,
    top_level_section   TEXT,           -- e.g. "Mobile Phones & Tablets"
    sub_section         TEXT,           -- e.g. "Buying Advice"
    poster              TEXT,
    post_date_unix      INTEGER,
    post_date_text      TEXT,
    replies             INTEGER,
    views               INTEGER,
    last_post_date_unix INTEGER,
    last_post_date_text TEXT,
    last_post_author    TEXT,
    is_sticky           INTEGER DEFAULT 0,
    is_closed           INTEGER DEFAULT 0,
    scraped_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (section_id) REFERENCES sections(id)
);

-- Tracks which (paginated) forum listing pages have already been scraped,
-- so a crawl can be safely resumed without re-fetching everything.
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url    TEXT UNIQUE NOT NULL,
    status      TEXT NOT NULL,   -- 'ok' or 'error'
    thread_count INTEGER DEFAULT 0,
    scraped_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_threads_section ON threads(section_id);
CREATE INDEX IF NOT EXISTS idx_threads_top ON threads(top_level_section);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);
"""


def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_or_create_section(conn, url, name, parent_id, top_level_name, depth, description=None):
    cur = conn.execute("SELECT id FROM sections WHERE url = ?", (url,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO sections (url, name, parent_id, top_level_name, depth, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, name, parent_id, top_level_name, depth, description),
    )
    conn.commit()
    return cur.lastrowid


def page_already_scraped(conn, page_url):
    cur = conn.execute("SELECT status FROM crawl_log WHERE page_url = ?", (page_url,))
    row = cur.fetchone()
    return row is not None and row[0] == "ok"


def log_page(conn, page_url, status, thread_count=0):
    conn.execute(
        "INSERT INTO crawl_log (page_url, status, thread_count) VALUES (?, ?, ?) "
        "ON CONFLICT(page_url) DO UPDATE SET status=excluded.status, "
        "thread_count=excluded.thread_count, scraped_at=CURRENT_TIMESTAMP",
        (page_url, status, thread_count),
    )
    conn.commit()


def upsert_thread(conn, data):
    conn.execute(
        """
        INSERT INTO threads (
            thread_id, url, title, prefix, section_id, top_level_section,
            sub_section, poster, post_date_unix, post_date_text, replies,
            views, last_post_date_unix, last_post_date_text, last_post_author,
            is_sticky, is_closed
        ) VALUES (:thread_id, :url, :title, :prefix, :section_id, :top_level_section,
            :sub_section, :poster, :post_date_unix, :post_date_text, :replies,
            :views, :last_post_date_unix, :last_post_date_text, :last_post_author,
            :is_sticky, :is_closed)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title, prefix=excluded.prefix, section_id=excluded.section_id,
            top_level_section=excluded.top_level_section, sub_section=excluded.sub_section,
            poster=excluded.poster, post_date_unix=excluded.post_date_unix,
            post_date_text=excluded.post_date_text, replies=excluded.replies,
            views=excluded.views, last_post_date_unix=excluded.last_post_date_unix,
            last_post_date_text=excluded.last_post_date_text,
            last_post_author=excluded.last_post_author, is_sticky=excluded.is_sticky,
            is_closed=excluded.is_closed, scraped_at=CURRENT_TIMESTAMP
        """,
        data,
    )
    conn.commit()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, delay=1.0, max_retries=4):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay
        self.max_retries = max_retries

    def get(self, url):
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code == 200:
                    time.sleep(self.delay)
                    return resp.text
                elif resp.status_code == 429:
                    wait = self.delay * (2 ** attempt)
                    log.warning(f"429 rate-limited on {url}, backing off {wait:.1f}s")
                    time.sleep(wait)
                else:
                    log.warning(f"HTTP {resp.status_code} for {url}")
                    time.sleep(self.delay)
                    return None
            except requests.RequestException as e:
                wait = self.delay * (2 ** attempt)
                log.warning(f"Error fetching {url}: {e}. Retry in {wait:.1f}s")
                time.sleep(wait)
        log.error(f"Giving up on {url} after {self.max_retries} attempts")
        return None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def extract_thread_id(url):
    m = re.search(r"\.(\d+)/?(?:$|page-\d+/?$|#.*)?$", url)
    return m.group(1) if m else None


def parse_int(text):
    if not text:
        return None
    text = text.strip().upper().replace(",", "")
    m = re.match(r"^([\d.]+)\s*K$", text)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.match(r"^([\d.]+)\s*M$", text)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    m = re.match(r"^\d+$", text)
    return int(text) if m else None


def get_child_forums(soup, base_url):
    """
    Parse a XenForo node-list (category/forum listing) to get child forum links.
    Returns list of dicts: {url, name, description}
    """
    results = []
    seen = set()
    for node in soup.select(".node-list .node--forum, .node-list .node--category"):
        title_el = node.select_one(".node-title a") or node.select_one("a.node-title")
        if not title_el or not title_el.get("href"):
            continue
        href = urljoin(base_url, title_el["href"])
        if href in seen:
            continue
        seen.add(href)
        desc_el = node.select_one(".node-description")
        results.append({
            "url": href,
            "name": title_el.get_text(strip=True),
            "description": desc_el.get_text(strip=True) if desc_el else None,
            "is_category": "node--category" in node.get("class", []),
        })
    return results


def get_pagination_last_page(soup):
    nav = soup.select_one(".pageNav-main")
    if not nav:
        return 1
    pages = [a.get_text(strip=True) for a in nav.select("a.pageNav-page")]
    pages += [li.get_text(strip=True) for li in nav.select("li.pageNav-page")]
    nums = [int(p) for p in pages if p.isdigit()]
    return max(nums) if nums else 1


def build_page_url(forum_url, page_num):
    if page_num <= 1:
        return forum_url
    return urljoin(forum_url.rstrip("/") + "/", f"page-{page_num}")


def parse_thread_list(soup, base_url):
    """
    Parse a forum's thread listing page. Returns list of thread dicts.
    """
    threads = []
    items = soup.select(".structItem--thread")
    for item in items:
        title_container = item.select_one(".structItem-title")
        if not title_container:
            continue
        # The title container may hold a leading prefix badge <a class="labelLink">
        # followed by the real title <a>. Skip anchors that only wrap a label badge.
        title_el = None
        prefix = None
        for a in title_container.select("a"):
            if a.select_one(".label") or "labelLink" in a.get("class", []):
                label_el = a.select_one(".label")
                prefix = label_el.get_text(strip=True) if label_el else a.get_text(strip=True)
                continue
            title_el = a
            break
        if not title_el or not title_el.get("href"):
            continue
        href = urljoin(base_url, title_el["href"])

        # Poster + start date live in the first "minor" parts list item
        poster_el = item.select_one(".structItem-minor .structItem-parts li:first-child a") \
            or item.select_one(".structItem-parts a")
        poster = poster_el.get_text(strip=True) if poster_el else None

        start_time_el = item.select_one(".structItem-minor time") or item.select_one("time")
        post_date_unix = None
        post_date_text = None
        if start_time_el:
            post_date_text = start_time_el.get_text(strip=True)
            ts = start_time_el.get("data-time") or start_time_el.get("datetime")
            if ts and str(ts).isdigit():
                post_date_unix = int(ts)

        # Replies / Views
        replies = views = None
        for dl in item.select(".structItem-cell--meta dl"):
            dt = dl.select_one("dt")
            dd = dl.select_one("dd")
            if not dt or not dd:
                continue
            label = dt.get_text(strip=True).lower()
            val = parse_int(dd.get_text(strip=True))
            if "repl" in label or "message" in label:
                replies = val
            elif "view" in label:
                views = val

        # Last post info
        last_post_date_unix = None
        last_post_date_text = None
        last_post_author = None
        latest_cell = item.select_one(".structItem-cell--latest")
        if latest_cell:
            lp_time_el = latest_cell.select_one("time")
            if lp_time_el:
                last_post_date_text = lp_time_el.get_text(strip=True)
                ts = lp_time_el.get("data-time") or lp_time_el.get("datetime")
                if ts and str(ts).isdigit():
                    last_post_date_unix = int(ts)
            lp_author_el = latest_cell.select_one("a")
            if lp_author_el:
                last_post_author = lp_author_el.get_text(strip=True)

        is_sticky = 1 if "is-sticky" in item.get("class", []) or item.select_one(".structItem-status--sticky") else 0
        is_closed = 1 if item.select_one(".structItem-status--locked") else 0

        threads.append({
            "thread_id": extract_thread_id(href),
            "url": href,
            "title": title_el.get_text(strip=True),
            "prefix": prefix,
            "poster": poster,
            "post_date_unix": post_date_unix,
            "post_date_text": post_date_text,
            "replies": replies,
            "views": views,
            "last_post_date_unix": last_post_date_unix,
            "last_post_date_text": last_post_date_text,
            "last_post_author": last_post_author,
            "is_sticky": is_sticky,
            "is_closed": is_closed,
        })
    return threads


# --------------------------------------------------------------------------
# Crawl orchestration
# --------------------------------------------------------------------------

def crawl(conn, fetcher, start_url, resume=False, max_pages=None, debug=False):
    """
    BFS over the forum tree. Each queue item is:
      (url, name, parent_section_id, top_level_name, depth, description)

    depth=0 is only used for the very first item as a bootstrap marker; the
    node itself is still fully processed (both for child forums AND for its
    own thread listing, if it has one) exactly like any other depth.
    """
    queue = deque()
    queue.append((start_url, None, None, None, 0, None))
    visited_forums = set()
    total_threads_seen = 0

    while queue:
        url, name, parent_id, top_level_name, depth, description = queue.popleft()
        if url in visited_forums:
            continue
        visited_forums.add(url)

        html = fetcher.get(url)
        if html is None:
            log_page(conn, url, "error")
            continue
        soup = BeautifulSoup(html, "html.parser")

        if debug:
            n_nodes = len(soup.select(".node-list .node--forum, .node-list .node--category"))
            n_threads = len(soup.select(".structItem--thread"))
            log.info(
                f"[DEBUG] {url} -> html_len={len(html)} "
                f"node_children_found={n_nodes} structItem_threads_found={n_threads}"
            )
            if n_nodes == 0 and n_threads == 0:
                debug_path = f"debug_{extract_thread_id(url) or abs(hash(url))}.html"
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(html)
                log.warning(f"[DEBUG] No nodes or threads found on this page — "
                            f"saved raw HTML to {debug_path} for inspection")

        # This is the bootstrap node (the URL passed in as start_url).
        is_bootstrap = (parent_id is None and name is None)
        if is_bootstrap:
            has_own_threads = soup.select_one(".structItemContainer") is not None
            if has_own_threads:
                # Started directly at a real forum/section that has threads
                # (e.g. a leaf sub-forum) -> treat it as a genuine section.
                h1 = soup.select_one("h1.p-title-value") or soup.select_one("h1")
                name = h1.get_text(strip=True) if h1 else "Section"
                depth = 1
            else:
                # Started at a pure hub/index page (like the real forum
                # homepage) that only lists categories -> don't create a
                # section row for it; let its children become the top-level
                # sections, same as before.
                name = None
                depth = 0

        if name is not None:
            this_top_level = top_level_name if top_level_name else name
            section_id = get_or_create_section(
                conn, url, name, parent_id, this_top_level, depth, description
            )
            log.info(f"{'  ' * depth}[Section] {name}  ({url})")
        else:
            this_top_level = None
            section_id = None

        # 1) Discover child forums/categories on this page
        children = get_child_forums(soup, url)
        for child in children:
            queue.append((
                child["url"], child["name"], section_id,
                this_top_level if this_top_level else child["name"],
                depth + 1, child["description"],
            ))

        # 2) If this node itself hosts threads, paginate through its listing.
        if soup.select_one(".structItemContainer"):
            last_page = get_pagination_last_page(soup)
            if max_pages:
                last_page = min(last_page, max_pages)
            log.info(f"{'  ' * depth}  -> {last_page} page(s) of threads")

            for page_num in range(1, last_page + 1):
                page_url = build_page_url(url, page_num)

                if resume and page_already_scraped(conn, page_url):
                    log.info(f"{'  ' * depth}  (skip, already scraped) page {page_num}")
                    continue

                if page_num == 1:
                    page_soup = soup  # already fetched
                else:
                    page_html = fetcher.get(page_url)
                    if page_html is None:
                        log_page(conn, page_url, "error")
                        continue
                    page_soup = BeautifulSoup(page_html, "html.parser")

                thread_rows = parse_thread_list(page_soup, page_url)
                for t in thread_rows:
                    t["section_id"] = section_id
                    t["top_level_section"] = this_top_level
                    t["sub_section"] = name
                    upsert_thread(conn, t)

                total_threads_seen += len(thread_rows)
                log_page(conn, page_url, "ok", len(thread_rows))
                log.info(
                    f"{'  ' * depth}  page {page_num}/{last_page}: "
                    f"{len(thread_rows)} threads (total so far: {total_threads_seen})"
                )

    log.info(f"Crawl complete. Total thread rows upserted: {total_threads_seen}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Index a XenForo forum into SQLite")
    ap.add_argument("--db", default="forum_index.db", help="Path to SQLite DB file")
    ap.add_argument("--start-url", default=BASE_URL, help="Forum root URL to start from")
    ap.add_argument("--delay", type=float, default=1.0, help="Delay between requests (seconds)")
    ap.add_argument("--resume", action="store_true", help="Skip forum-pages already logged as scraped")
    ap.add_argument("--max-pages", type=int, default=None, help="Cap pages per sub-forum (for testing)")
    ap.add_argument("--debug", action="store_true",
                     help="Log element counts per page and dump raw HTML when nothing is found")
    args = ap.parse_args()

    conn = init_db(args.db)
    fetcher = Fetcher(delay=args.delay)

    log.info(f"Starting crawl at {args.start_url}")
    log.info(f"Database: {args.db}")
    try:
        crawl(conn, fetcher, args.start_url, resume=args.resume, max_pages=args.max_pages, debug=args.debug)
    except KeyboardInterrupt:
        log.warning("Interrupted by user. Progress so far is saved in the DB — "
                    "re-run with --resume to continue.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
