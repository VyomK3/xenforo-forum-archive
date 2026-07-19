import os
import sqlite3
from mock_server import start_server
from scrape_forum import init_db, crawl, Fetcher

DB_PATH = "test_output.db"


def test_full_tree_crawl():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = init_db(DB_PATH)
    fetcher = Fetcher(delay=0.01)

    crawl(conn, fetcher, "http://127.0.0.1:8811/community/")
    conn.close()

    # Now verify contents
    conn = sqlite3.connect(DB_PATH)
    sections = conn.execute("SELECT name, parent_id, depth, top_level_name FROM sections").fetchall()
    threads = conn.execute(
        "SELECT title, poster, replies, views, top_level_section, sub_section, url FROM threads ORDER BY title"
    ).fetchall()

    print("SECTIONS:")
    for s in sections:
        print(" ", s)
    print("\nTHREADS:")
    for t in threads:
        print(" ", t)

    assert len(sections) == 2, f"expected 2 sections, got {len(sections)}"
    assert len(threads) == 3, f"expected 3 threads, got {len(threads)}"

    titles = {t[0] for t in threads}
    assert titles == {"Thread A", "Thread B", "Thread C"}

    by_title = {t[0]: t for t in threads}
    assert by_title["Thread A"][1] == "UserA"
    assert by_title["Thread A"][2] == 3   # replies
    assert by_title["Thread B"][3] == 12  # views
    assert by_title["Thread C"][4] == "Mobile Phones"  # top_level_section
    assert by_title["Thread C"][5] == "Buying Advice"  # sub_section

    crawl_log = conn.execute("SELECT page_url, status, thread_count FROM crawl_log").fetchall()
    print("\nCRAWL LOG:")
    for c in crawl_log:
        print(" ", c)

    conn.close()
    os.remove(DB_PATH)
    print("\nALL INTEGRATION CHECKS PASSED (full tree crawl)")


def test_start_at_leaf_section():
    """Regression test: starting --start-url directly at a leaf forum (no
    sub-forums) must still index its own threads. This was the reported bug."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = init_db(DB_PATH)
    fetcher = Fetcher(delay=0.01)

    # buying-advice.11 has threads but no children of its own
    crawl(conn, fetcher, "http://127.0.0.1:8811/community/buying-advice.11/")
    conn.close()

    conn = sqlite3.connect(DB_PATH)
    threads = conn.execute("SELECT title FROM threads").fetchall()
    print("\n[leaf-start test] THREADS:", threads)
    assert len(threads) == 1, f"expected 1 thread when starting at a leaf section, got {len(threads)}"
    assert threads[0][0] == "Thread C"
    conn.close()
    os.remove(DB_PATH)
    print("ALL INTEGRATION CHECKS PASSED (leaf-section start)")


if __name__ == "__main__":
    start_server(port=8811)
    test_full_tree_crawl()
    test_start_at_leaf_section()
