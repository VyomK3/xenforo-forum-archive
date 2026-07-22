#!/usr/bin/env python3
"""
dedupe_posts.py — find and remove duplicate rows in the `posts` table of a
forum-scrape SQLite database (as produced by scrape_thread_to_db.py).

WHAT COUNTS AS A DUPLICATE
---------------------------
Rows sharing the same (thread_id, site_post_id) are treated as duplicates.
`site_post_id` is the forum's own post identifier, scraped directly off the
page (e.g. XenForo's "post-860339"), so two rows sharing it are guaranteed
to be the exact same real post saved more than once — most commonly from a
thread being re-scraped without its old posts being fully cleared first, or
the same thread ending up scraped twice under two slightly different URLs
that landed as two separate `threads` rows.

Rows with a missing/blank `site_post_id` fall back to being grouped by
(thread_id, page_number, post_number) instead.

Within each duplicate group, only one row survives:
  - by default, the OLDEST row (lowest internal `id`) is kept
  - with --keep-latest, the NEWEST row (highest `id`) is kept instead
All other rows in the group are deleted.

USAGE
-----
    # Dry run (default) — just reports what would be removed, deletes nothing
    python dedupe_posts.py --db forum_index.db

    # Write a full CSV of every duplicate row found, for manual review
    python dedupe_posts.py --db forum_index.db --report-csv duplicates.csv

    # Actually remove the duplicates (a safety backup is taken first unless --no-backup)
    python dedupe_posts.py --db forum_index.db --apply

    # Keep the newest copy of each duplicate instead of the oldest
    python dedupe_posts.py --db forum_index.db --apply --keep-latest

    # Skip the automatic pre-delete backup (not recommended)
    python dedupe_posts.py --db forum_index.db --apply --no-backup
"""
import argparse
import csv
import datetime
import sqlite3
import sys
from collections import defaultdict


def find_duplicate_groups(conn: sqlite3.Connection) -> dict:
    """
    Returns {dedup_key: [row_id, ...]} (ids in ascending/insertion order),
    including only groups that actually have more than one row.
    """
    groups = defaultdict(list)
    rows = conn.execute(
        "SELECT id, thread_id, site_post_id, page_number, post_number FROM posts ORDER BY id"
    ).fetchall()

    for row_id, thread_id, site_post_id, page_number, post_number in rows:
        if site_post_id:
            key = ("site_post_id", thread_id, site_post_id)
        else:
            key = ("page_post", thread_id, page_number, post_number)
        groups[key].append(row_id)

    return {key: ids for key, ids in groups.items() if len(ids) > 1}


def write_report_csv(conn: sqlite3.Connection, groups: dict, keep_latest: bool, path: str) -> None:
    """Write full details of every row in every duplicate group to a CSV for manual review."""
    all_ids = [row_id for ids in groups.values() for row_id in ids]
    rows_by_id = {}

    # SQLite caps the number of bound parameters per statement (commonly
    # 999). With large duplicate counts, all_ids can easily run into the
    # hundreds of thousands, so fetch in batches rather than one giant
    # `WHERE id IN (...)` query.
    CHUNK_SIZE = 500
    for start in range(0, len(all_ids), CHUNK_SIZE):
        chunk = all_ids[start : start + CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in chunk)
        query = f"""
            SELECT p.id, p.thread_id, t.url, t.title, p.page_number, p.post_number,
                   p.site_post_id, p.author, p.timestamp_display, p.content_html
            FROM posts p
            LEFT JOIN threads t ON t.id = p.thread_id
            WHERE p.id IN ({placeholders})
        """
        for row in conn.execute(query, chunk):
            rows_by_id[row[0]] = row

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "dedup_key", "action", "post_row_id", "thread_id", "thread_url", "thread_title",
                "page_number", "post_number", "site_post_id", "author", "timestamp_display",
                "content_html_preview",
            ]
        )
        for key, ids in groups.items():
            keep_id = ids[-1] if keep_latest else ids[0]
            for row_id in ids:
                r = rows_by_id.get(row_id)
                if r is None:
                    continue
                action = "KEEP" if row_id == keep_id else "DELETE"
                content_preview = (r[9] or "")[:200].replace("\n", " ")
                writer.writerow(
                    [str(key), action, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], content_preview]
                )


def backup_before_delete(db_path: str) -> str:
    """Take a consistent snapshot of the database before deleting anything, using SQLite's
    own online backup API rather than a raw file copy (safe even mid-write)."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = f"{db_path}.before_dedupe_{ts}.bak"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def main():
    parser = argparse.ArgumentParser(description="Find and remove duplicate rows in the posts table.")
    parser.add_argument("--db", required=True, help="Path to the SQLite database file")
    parser.add_argument(
        "--apply", action="store_true", help="Actually delete duplicates (default: dry run / report only)"
    )
    parser.add_argument(
        "--keep-latest",
        action="store_true",
        help="Keep the newest (highest id) row in each duplicate group instead of the oldest (default: keep oldest)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the automatic safety backup taken before deleting (only relevant with --apply)",
    )
    parser.add_argument(
        "--report-csv",
        metavar="PATH",
        help="Write full details of every duplicate row (marked KEEP/DELETE) to this CSV file for review",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    groups = find_duplicate_groups(conn)

    if not groups:
        print(f"No duplicate posts found in {args.db}.")
        return

    total_to_remove = sum(len(ids) - 1 for ids in groups.values())
    print(f"Found {len(groups)} duplicate group(s), {total_to_remove} duplicate row(s) that would be removed.")
    for key, ids in list(groups.items())[:5]:
        print(f"  e.g. {key} -> post row ids {ids}")
    if len(groups) > 5:
        print(f"  ... and {len(groups) - 5} more group(s)")

    if args.report_csv:
        write_report_csv(conn, groups, args.keep_latest, args.report_csv)
        print(f"\nWrote full duplicate details to {args.report_csv} (KEEP/DELETE marked per row).")

    if not args.apply:
        print("\nDry run only — nothing was deleted. Re-run with --apply to actually remove duplicates.")
        conn.close()
        return

    if not args.no_backup:
        backup_path = backup_before_delete(args.db)
        print(f"\nSafety backup saved to {backup_path}")

    ids_to_delete = []
    for ids in groups.values():
        keep_id = ids[-1] if args.keep_latest else ids[0]
        ids_to_delete.extend(i for i in ids if i != keep_id)

    conn.execute("BEGIN")
    conn.executemany("DELETE FROM posts WHERE id = ?", [(i,) for i in ids_to_delete])
    conn.commit()
    print(f"Deleted {len(ids_to_delete)} duplicate row(s).")

    # Sanity check: confirm no duplicates remain
    remaining = find_duplicate_groups(conn)
    if remaining:
        print(f"WARNING: {len(remaining)} duplicate group(s) still remain — please investigate.")
    else:
        print("Confirmed: no duplicate posts remain.")

    conn.execute("VACUUM")
    conn.close()
    print("Database compacted with VACUUM. Done.")


if __name__ == "__main__":
    main()
