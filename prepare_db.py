#!/usr/bin/env python3
"""
prepare_db.py — One-time (re-runnable) preparation of forum_index.db for
serving with app.py. Run this once after copying the database to the server,
and again any time the database is replaced/updated:

    python3 prepare_db.py --db forum_index.db

What it does (all inside the .db file itself — no other files touched):

  1. Indexes
       posts(thread_id, page_number, post_number)  -> per-thread post fetch
                                                      becomes a millisecond
                                                      index range scan
       threads(section_id)                         -> cheap section lookups

  2. thread_starters table
       (thread_id, author) — the first post's author per thread, so the app
       never has to touch the posts table at startup just to know who
       started each thread.

  3. site_meta table
       total_posts + prepared_at, so the app doesn't need a COUNT(*) full
       scan of posts on every startup.

  4. thread_fts — an FTS5 full-text index, one row per thread:
       title    the thread title
       posters  every distinct author who posted in the thread
       content  plain-text of all posts (tags stripped, entities decoded),
                using the same html_to_text() as generate_site.py
     rowid == threads.id, so search results join straight back to threads.

Safe to re-run: everything is DROP/CREATE or IF NOT EXISTS.
"""

import argparse
import sqlite3
import sys
import time
from itertools import groupby
from pathlib import Path

# Same text extraction as generate_site.py's html_to_text(), copied here
# verbatim rather than imported: importing generate_site pulls in pandas
# (needed only for the xlsx), which costs 100MB+ of RAM before this script
# does any work — a real problem on a small VPS.
import re
from html import unescape as unescape_html

_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(html):
    if not html:
        return ""
    text = _SCRIPT_BLOCK_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = unescape_html(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def prepare(db_path):
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # LOW-MEMORY settings: this script must run on a small VPS. 8MB page
    # cache, temp data on disk, WAL journaling (constant memory, appends to
    # a sidecar file instead of building big rollback state).
    cur.execute("PRAGMA cache_size = -8192")
    cur.execute("PRAGMA temp_store = FILE")
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA wal_autocheckpoint = 1000")

    # ---- 1. Indexes -------------------------------------------------------
    print("Creating indexes (skipped instantly if they already exist)...")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_thread "
                "ON posts(thread_id, page_number, post_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_threads_section "
                "ON threads(section_id)")
    # Section listing pages sort sticky-first then newest-first — this
    # composite index answers each page with a pure index range scan.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_threads_listing "
                "ON threads(section_id, is_sticky DESC, "
                "last_post_date_unix DESC)")
    # Thread pages resolve /threads/<slug>.<thread_id>.html by thread_id.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_threads_thread_id "
                "ON threads(thread_id)")
    conn.commit()

    # ---- 2 + 4. One streaming pass over posts builds both -----------------
    # thread_starters and the FTS rows, exactly like generate_site.py's
    # single-pass approach (never two queries per thread).
    print("Rebuilding thread_starters and thread_fts "
          "(one sequential pass over all posts)...")
    cur.execute("DROP TABLE IF EXISTS thread_starters")
    cur.execute("CREATE TABLE thread_starters (thread_id INTEGER PRIMARY KEY, "
                "author TEXT)")
    cur.execute("DROP TABLE IF EXISTS thread_fts")
    cur.execute("CREATE VIRTUAL TABLE thread_fts USING fts5("
                "title, posters, content, "
                "tokenize='unicode61 remove_diacritics 2')")

    # Titles are looked up one at a time by primary key (microseconds each)
    # instead of preloading them all into a dict — on a large archive that
    # dict alone can be tens of MB.
    title_cur = conn.cursor()

    def title_for(thread_id):
        row = title_cur.execute("SELECT title FROM threads WHERE id = ?",
                                (thread_id,)).fetchone()
        return (row["title"] or "") if row else ""

    read_cur = conn.cursor()
    read_cur.execute("SELECT thread_id, author, content_html FROM posts "
                     "ORDER BY thread_id, page_number, post_number")

    write_cur = conn.cursor()
    n_threads = 0
    t_pass = time.time()
    for thread_id, group in groupby(read_cur, key=lambda r: r["thread_id"]):
        text_parts = []
        posters = []
        seen = set()
        starter = None
        for p in group:
            author = p["author"] or "Unknown"
            if starter is None:
                starter = author
            if author not in seen:
                seen.add(author)
                posters.append(author)
            piece = html_to_text(p["content_html"])
            if piece:
                text_parts.append(piece)

        write_cur.execute("INSERT INTO thread_starters VALUES (?, ?)",
                          (thread_id, starter))
        write_cur.execute(
            "INSERT INTO thread_fts (rowid, title, posters, content) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, title_for(thread_id), " ".join(posters),
             " ".join(text_parts)))
        n_threads += 1
        if n_threads % 1000 == 0:
            conn.commit()
            print(f"  {n_threads:,} threads indexed "
                  f"({time.time() - t_pass:.1f}s elapsed)...")
    conn.commit()
    print(f"  {n_threads:,} threads indexed in {time.time() - t_pass:.1f}s.")

    # ---- 3. site_meta -----------------------------------------------------
    cur.execute("DROP TABLE IF EXISTS site_meta")
    cur.execute("CREATE TABLE site_meta (key TEXT PRIMARY KEY, value TEXT)")
    total_posts = cur.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    cur.execute("INSERT INTO site_meta VALUES ('total_posts', ?)",
                (str(total_posts),))
    cur.execute("INSERT INTO site_meta VALUES ('prepared_at', "
                "datetime('now'))")
    conn.commit()

    # ---- Optimize ---------------------------------------------------------
    print("Optimizing FTS index and database statistics...")
    try:
        cur.execute("INSERT INTO thread_fts(thread_fts) VALUES ('optimize')")
        conn.commit()
    except (MemoryError, sqlite3.OperationalError) as e:
        # Non-fatal: the index works fine without this compaction step.
        conn.rollback()
        print(f"  Note: FTS optimize skipped ({e}) — search still works.")
    cur.execute("ANALYZE")
    conn.commit()
    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    print(f"Done in {time.time() - t0:.1f}s. "
          f"{n_threads:,} threads, {total_posts:,} posts. "
          f"The database is ready for app.py.")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare forum_index.db for serving with app.py")
    parser.add_argument("--db", default="forum_index.db",
                        help="Path to forum_index.db")
    args = parser.parse_args()
    if not Path(args.db).exists():
        sys.exit(f"Database not found: {args.db}")
    prepare(args.db)


if __name__ == "__main__":
    main()
