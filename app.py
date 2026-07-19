#!/usr/bin/env python3
"""
app.py — Serve the xenforo-archiver forum replica directly from forum_index.db,
using the same Jinja templates and helper code as generate_site.py.

LOW-MEMORY DESIGN (v2): nothing thread- or post-related is held in memory.
Every listing page, thread page, and search is answered by an indexed SQL
query at request time. The only things built at startup are the tiny bits
every page shares: the section hierarchy from the xlsx, per-section thread
counts, sidebar stats, and the top-5 threads box. Resident memory stays at
a few tens of MB regardless of how many threads/posts the archive holds —
this is the version to run on a small VPS.

URL layout mirrors the static build exactly, so every relative link baked
into the templates keeps working unchanged:

    /                             home (also /index.html)
    /sections/<slug>.html         section listing, page 1
    /sections/<slug>-page-N.html  section listing, page N
    /threads/<slug>.<id>.html     one thread
    /search.html?q=...&mode=...   title / started-by / contributed search
    /full_search.html?q=...       FTS5 full-text search over post content
    /forum_assets/<file>          avatars (nginx serves these in production)

Run prepare_db.py against the database ONCE before starting this app —
it creates the indexes and FTS tables everything below depends on.

Configuration via environment variables (all optional):
    FORUM_DB         path to forum_index.db      (default: forum_index.db)
    FORUM_STRUCTURE  path to the sections xlsx   (default: forum_sections.xlsx)
    FORUM_TEMPLATES  templates folder            (default: templates)
    FORUM_ASSETS     avatars folder              (default: forum_assets)

Development:  python3 app.py            (http://127.0.0.1:5000)
Production:   gunicorn -w 2 -b 127.0.0.1:8000 app:app
"""

import math
import os
import re
import sqlite3
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from flask import (Flask, abort, g, redirect, request,
                   send_from_directory)
from jinja2 import Environment, FileSystemLoader

# All the logic that defines what the site looks like stays in
# generate_site.py — this file only changes WHEN pages get rendered
# (on request instead of at build time).
from generate_site import (
    DEFAULT_SECTION_ICON, PAGE_SIZE, SECTION_ICONS, SITE_DEVELOPER_NOTE,
    SITE_FOOTER_NOTE, SITE_SUBTITLE, SITE_TITLE, build_pagination,
    clean_content_html, fmt_int, format_snapshot_date, load_structure,
    page_filename, slugify, thread_filename,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("FORUM_DB", str(BASE_DIR / "forum_index.db"))
XLSX_PATH = os.environ.get("FORUM_STRUCTURE",
                           str(BASE_DIR / "forum_sections.xlsx"))
TEMPLATES_DIR = os.environ.get("FORUM_TEMPLATES", str(BASE_DIR / "templates"))
ASSETS_DIR = os.environ.get("FORUM_ASSETS", str(BASE_DIR / "forum_assets"))

SEARCH_PAGE_SIZE = 20

app = Flask(__name__)

# Same environment settings as generate_site.py (autoescape OFF because
# thread post content is stored as HTML) — templates escape user input
# explicitly with |e where needed.
env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=False)
home_tpl = env.get_template("home_template_dark.html")
thread_tpl = env.get_template("thread_template_dark.html")
section_tpl = env.get_template("section_page_template_dark.html")
search_tpl = env.get_template("search_server_dark.html")


# --------------------------------------------------------------------------
# Per-request read-only DB connection
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        # mode=ro guarantees the app can never modify the archive.
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# The SQL every thread-row consumer shares: thread columns plus the
# starter's name from the prepare_db-built thread_starters table.
THREAD_SELECT = ("SELECT t.*, ts.author AS _starter FROM threads t "
                 "LEFT JOIN thread_starters ts ON ts.thread_id = t.id ")
LISTING_ORDER = ("ORDER BY t.is_sticky DESC, t.last_post_date_unix DESC ")
# (SQLite sorts NULLs last in DESC order, which matches the static build's
#  `or 0` treatment of missing sticky flags / dates.)


def started_by(row):
    return row["_starter"] or row["poster"] or "Unknown"


# --------------------------------------------------------------------------
# Site model — the SMALL shared stuff only, built once at startup:
# structure hierarchy, per-section counts, sidebar, breadcrumbs.
# No thread lists, no filename maps, no post content.
# --------------------------------------------------------------------------

class SiteModel:
    def __init__(self):
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT * FROM sections")
        db_sections_by_name = {row["name"].strip(): dict(row)
                               for row in cur.fetchall()}

        try:
            cur.execute("SELECT value FROM site_meta WHERE key='total_posts'")
        except sqlite3.OperationalError:
            raise SystemExit(
                "The database has not been prepared for serving yet.\n"
                "Run:  python3 prepare_db.py --db " + DB_PATH)
        row = cur.fetchone()
        total_posts = int(row["value"]) if row else 0

        # Per-section thread counts: one indexed GROUP BY, tiny result.
        cur.execute("SELECT section_id, COUNT(*) AS c FROM threads "
                    "GROUP BY section_id")
        self.counts_by_section_id = {r["section_id"]: r["c"]
                                     for r in cur.fetchall()}
        total_threads = sum(self.counts_by_section_id.values())

        # Snapshot date — whichever timestamp columns this schema has.
        latest = None
        for col in ("last_scraped_at", "scraped_at"):
            try:
                cur.execute(f"SELECT MAX({col}) FROM threads")
                val = cur.fetchone()[0]
                if val and (latest is None or val > latest):
                    latest = val
            except sqlite3.OperationalError:
                pass
        self.snapshot_date = format_snapshot_date(latest)

        # Top-5 most viewed threads for the sidebar. One scan, once, at
        # startup — never per request.
        cur.execute(THREAD_SELECT +
                    "ORDER BY CAST(t.views AS INTEGER) DESC LIMIT 5")
        self.sidebar_top_threads = []
        for t in cur.fetchall():
            self.sidebar_top_threads.append({
                "title": t["title"],
                "href": f"threads/{thread_filename(dict(t))}",
                "replies": fmt_int(t["replies"]),
                "views": fmt_int(t["views"]),
                "section": (t["sub_section"] or "").strip(),
            })
        conn.close()

        def fmt_compact(n):
            n = n or 0
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        def count_for(name):
            db_sec = db_sections_by_name.get(name.strip())
            return self.counts_by_section_id.get(db_sec["id"], 0) if db_sec else 0

        # ---- Structure hierarchy (same walk as generate_site's build) ----
        raw_structure = load_structure(XLSX_PATH)

        sidebar_nav = []
        total_subsections = 0
        for mega in raw_structure:
            mega_count = 0
            for sub in mega["subsections"]:
                total_subsections += 1
                mega_count += count_for(sub["name"])
                for subsub_name, _d in sub["subsubsections"]:
                    mega_count += count_for(subsub_name)
            mega["anchor"] = "sec-" + slugify(mega["name"], "section")
            sidebar_nav.append({
                "name": mega["name"],
                "href": f"index.html#{mega['anchor']}",
                "count": fmt_compact(mega_count),
            })

        self.sidebar_ctx = {
            "stats": {
                "sections": fmt_int(len(raw_structure)),
                "subsections": fmt_int(total_subsections),
                "threads": fmt_int(total_threads),
                "posts": fmt_int(total_posts),
            },
            "nav": sidebar_nav,
            "top_threads": self.sidebar_top_threads,
        }
        self.total_threads = total_threads
        self.total_posts = total_posts
        self.total_subsections = total_subsections

        # listings[slug] -> the per-section metadata section_tpl needs.
        # Thread rows themselves are fetched per request by section_id.
        self.listings = {}
        self.breadcrumb_parts_by_section_id = {}
        self.section_href_by_id = {}
        used_slugs = set()

        def make_slug(name, fallback):
            slug = slugify(name, fallback)
            base = slug
            i = 2
            while slug in used_slugs:
                slug = f"{base}-{i}"
                i += 1
            used_slugs.add(slug)
            return slug

        mega_sections_home = []
        for mega in raw_structure:
            subsections_home = []
            for sub in mega["subsections"]:
                sub_slug = make_slug(sub["name"], f"section-{len(used_slugs) + 1}")
                sub_db = db_sections_by_name.get(sub["name"].strip())
                own_count = count_for(sub["name"])
                has_subsubs = bool(sub["subsubsections"])
                show_own = (sub_db is not None) or not has_subsubs

                children_ctx, children_home = [], []
                subsub_total = 0
                for subsub_name, subsub_desc in sub["subsubsections"]:
                    ss_slug = make_slug(subsub_name,
                                        f"section-{len(used_slugs) + 1}")
                    ss_count = count_for(subsub_name)
                    subsub_total += ss_count
                    ss_db = db_sections_by_name.get(subsub_name.strip())
                    if ss_db:
                        self.breadcrumb_parts_by_section_id[ss_db["id"]] = \
                            [mega["name"], sub["name"], subsub_name]
                        self.section_href_by_id[ss_db["id"]] = \
                            f"../sections/{page_filename(ss_slug, 1)}"
                    children_ctx.append({
                        "name": subsub_name,
                        "href": page_filename(ss_slug, 1),
                        "thread_count": ss_count,
                    })
                    children_home.append({
                        "name": subsub_name,
                        "href": f"sections/{page_filename(ss_slug, 1)}",
                    })
                    self.listings[ss_slug] = dict(
                        slug=ss_slug, name=subsub_name,
                        mega_name=mega["name"], description=subsub_desc,
                        section_db_id=ss_db["id"] if ss_db else None,
                        own_count=ss_count, show_own=True,
                        total_count=ss_count, children=None,
                        parent={"name": sub["name"],
                                "href": page_filename(sub_slug, 1)},
                        source_url=ss_db["url"] if ss_db else None)

                total_count = (own_count if show_own else 0) + subsub_total
                if sub_db:
                    self.breadcrumb_parts_by_section_id[sub_db["id"]] = \
                        [mega["name"], sub["name"]]
                    self.section_href_by_id[sub_db["id"]] = \
                        f"../sections/{page_filename(sub_slug, 1)}"
                self.listings[sub_slug] = dict(
                    slug=sub_slug, name=sub["name"], mega_name=mega["name"],
                    description=sub["description"],
                    section_db_id=sub_db["id"] if sub_db else None,
                    own_count=own_count, show_own=show_own,
                    total_count=total_count,
                    children=children_ctx if children_ctx else None,
                    parent=None,
                    source_url=sub_db["url"] if sub_db else None)

                subsections_home.append({
                    "name": sub["name"],
                    "href": f"sections/{page_filename(sub_slug, 1)}",
                    "description": sub["description"],
                    "total_count": total_count,
                    "children": children_home,
                })
            mega_sections_home.append({
                "name": mega["name"],
                "anchor": mega["anchor"],
                "subsections": subsections_home,
                "icon": SECTION_ICONS.get(mega["name"], DEFAULT_SECTION_ICON),
            })
        self.mega_sections_home = mega_sections_home

        # Which avatar files actually exist, listed once.
        self.assets_files = set()
        if Path(ASSETS_DIR).exists():
            self.assets_files = {p.name for p in Path(ASSETS_DIR).iterdir()
                                 if p.is_file()}


SITE = SiteModel()  # tiny; each gunicorn worker builds its own in seconds


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
@app.route("/index.html")
def home():
    return home_tpl.render(
        site_title=SITE_TITLE,
        site_subtitle=SITE_SUBTITLE,
        footer_note=SITE_FOOTER_NOTE,
        developer_note=SITE_DEVELOPER_NOTE,
        mega_sections=SITE.mega_sections_home,
        total_subsections=SITE.total_subsections,
        total_threads=SITE.total_threads,
        total_posts=SITE.total_posts,
        snapshot_date=SITE.snapshot_date,
    )


def _listing_row(t):
    return {
        "title": t["title"],
        "href": f"../threads/{thread_filename(dict(t))}",
        "started_by": started_by(t),
        "post_date_text": t["post_date_text"] or "",
        "replies": fmt_int(t["replies"]),
        "views": fmt_int(t["views"]),
        "last_post_date_text": t["last_post_date_text"] or "",
        "is_sticky": bool(t["is_sticky"]),
        "is_closed": bool(t["is_closed"]),
    }


_PAGE_RE = re.compile(r"^(.*)-page-(\d+)$")


@app.route("/sections/<path:fname>")
def section_page(fname):
    if not fname.endswith(".html"):
        abort(404)
    stem = fname[:-5]
    m = _PAGE_RE.match(stem)
    slug, page_num = (m.group(1), int(m.group(2))) if m else (stem, 1)
    listing = SITE.listings.get(slug)
    if listing is None or page_num < 1:
        abort(404)

    show_own = listing["show_own"]
    own_count = listing["own_count"] if show_own else 0
    total_pages = max(1, math.ceil(own_count / PAGE_SIZE)) if show_own else 1
    if page_num > total_pages:
        abort(404)

    page_threads = []
    if show_own and listing["section_db_id"] is not None:
        cur = get_db().execute(
            THREAD_SELECT + "WHERE t.section_id = ? " + LISTING_ORDER +
            "LIMIT ? OFFSET ?",
            (listing["section_db_id"], PAGE_SIZE, (page_num - 1) * PAGE_SIZE))
        page_threads = [_listing_row(t) for t in cur]

    page_items, prev_href, next_href = build_pagination(slug, page_num,
                                                        total_pages)
    return section_tpl.render(
        site_title=SITE_TITLE,
        home_href="../index.html",
        rel="../",
        snapshot_date=SITE.snapshot_date,
        sidebar=SITE.sidebar_ctx,
        show_search_panel=True,
        slug=slug,
        mega_name=listing["mega_name"],
        name=listing["name"],
        title=listing["name"],
        description=listing["description"],
        total_count=listing["total_count"],
        source_url=listing["source_url"],
        children=listing["children"],
        parent=listing["parent"],
        show_own=show_own,
        threads=page_threads,
        current_page=page_num,
        total_pages=total_pages,
        page_items=page_items,
        prev_href=prev_href,
        next_href=next_href,
    )


# /threads/<slug>.<thread_id>.html — possibly with a "-2" dedup suffix the
# static build appended on (extremely rare) filename collisions. The slug
# itself may contain dots ("windows-8.1-tips..."), so anchor on the LAST
# numeric ".<digits>" before ".html".
_THREAD_FNAME_RE = re.compile(r"^(?P<slug>.+)\.(?P<tid>\d+)(?:-(?P<dup>\d+))?\.html$")


def _find_thread(fname):
    """Resolve a request filename to its threads row, verifying that the
    row really regenerates this exact filename (so guessed/malformed URLs
    404 instead of serving the wrong thread)."""
    m = _THREAD_FNAME_RE.match(fname)
    if not m:
        return None
    tid, dup = m.group("tid"), m.group("dup")
    db = get_db()
    rows = db.execute(THREAD_SELECT + "WHERE t.thread_id = ? "
                      "ORDER BY t.id", (tid,)).fetchall()
    if not rows:
        # thread_filename falls back to str(threads.id) when the scraped
        # thread_id column is empty — mirror that fallback here.
        rows = db.execute(THREAD_SELECT + "WHERE t.id = ? "
                          "ORDER BY t.id", (int(tid),)).fetchall()

    base_fname = fname if dup is None else f"{m.group('slug')}.{tid}.html"
    matches = [r for r in rows if thread_filename(dict(r)) == base_fname]
    if not matches:
        return None
    if dup is None:
        return matches[0]
    idx = int(dup) - 1  # "-2" suffix means the 2nd thread with this name
    return matches[idx] if 0 <= idx < len(matches) else None


@app.route("/threads/<path:fname>")
def thread_page(fname):
    th = _find_thread(fname)
    if th is None:
        abort(404)

    cur = get_db().execute(
        "SELECT * FROM posts WHERE thread_id = ? "
        "ORDER BY page_number, post_number", (th["id"],))
    posts = []
    for p in cur:
        avatar = p["avatar_local_path"]
        if avatar:
            basename = Path(avatar).name
            avatar = (f"../forum_assets/{basename}"
                      if basename in SITE.assets_files else None)
        posts.append({
            "author": p["author"] or "Unknown",
            "author_profile_url": p["author_profile_url"],
            "avatar_local_path": avatar,
            "timestamp_display": p["timestamp_display"] or "",
            "content_html": clean_content_html(p["content_html"]),
            "post_number": p["post_number"],
        })

    thread_ctx = {
        "title": th["title"],
        "url": th["url"],
        "breadcrumb_parts":
            SITE.breadcrumb_parts_by_section_id.get(th["section_id"]),
        "breadcrumb_href": SITE.section_href_by_id.get(th["section_id"]),
    }
    return thread_tpl.render(
        site_title=SITE_TITLE,
        home_href="../index.html",
        rel="../",
        snapshot_date=SITE.snapshot_date,
        sidebar=SITE.sidebar_ctx,
        show_search_panel=True,
        thread=thread_ctx,
        posts=posts,
        post_count=len(posts),
    )


# --------------------------------------------------------------------------
# Search — every mode is a DB query; nothing scanned in Python.
# --------------------------------------------------------------------------

def _fts_query(user_q):
    """Turn free text into a safe FTS5 MATCH expression: every term quoted
    (so FTS operators in user input can't break the query), AND-combined,
    with prefix matching on each term so partial words still hit."""
    terms = re.findall(r"\w+", user_q, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in terms[:12])


def _search_result(t, matched_posters=None, snippet=None):
    return {
        "title": t["title"],
        "href": f"threads/{thread_filename(dict(t))}",
        "section": (t["sub_section"] or "").strip(),
        "started_by": started_by(t),
        "post_date_text": t["post_date_text"] or "",
        "replies": fmt_int(t["replies"]),
        "views": fmt_int(t["views"]),
        "last_post_date_text": t["last_post_date_text"] or "",
        "is_sticky": bool(t["is_sticky"]),
        "is_closed": bool(t["is_closed"]),
        "matched_posters": matched_posters,
        "snippet": snippet,
    }


def _paginate_qs(base, q, mode, current, total):
    def href(p):
        params = {"q": q, "page": p}
        if mode:
            params["mode"] = mode
        return f"{base}?{urlencode(params)}"
    pages = sorted({1, total, max(1, current - 1), current,
                    min(total, current + 1)})
    items, prev_p = [], None
    for p in pages:
        if prev_p is not None and p - prev_p > 1:
            items.append(None)
        items.append({"num": p, "href": href(p)})
        prev_p = p
    return items, href(max(1, current - 1)), href(min(total, current + 1))


def _like_pattern(q):
    """%q% with LIKE wildcards in the user's input escaped."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.route("/search.html")
def search():
    q = (request.args.get("q") or "").strip()
    mode = request.args.get("mode", "title")
    if mode not in ("title", "full", "started", "contributed"):
        mode = "title"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    results, total_results, total_pages = [], 0, 1
    page_items = prev_href = next_href = None
    if q:
        db = get_db()
        limit_offset = " LIMIT ? OFFSET ?"

        if mode == "title":
            where = "WHERE t.title LIKE ? ESCAPE '\\' "
            params = (_like_pattern(q),)
            total_results = db.execute(
                "SELECT COUNT(*) FROM threads t " + where,
                params).fetchone()[0]
            total_pages = max(1, math.ceil(total_results / SEARCH_PAGE_SIZE))
            page = min(page, total_pages)
            cur = db.execute(
                THREAD_SELECT + where + LISTING_ORDER + limit_offset,
                params + (SEARCH_PAGE_SIZE, (page - 1) * SEARCH_PAGE_SIZE))
            results = [_search_result(t) for t in cur]

        elif mode == "started":
            where = ("WHERE t.id IN (SELECT thread_id FROM thread_starters "
                     "WHERE author LIKE ? ESCAPE '\\') ")
            params = (_like_pattern(q),)
            total_results = db.execute(
                "SELECT COUNT(*) FROM threads t " + where,
                params).fetchone()[0]
            total_pages = max(1, math.ceil(total_results / SEARCH_PAGE_SIZE))
            page = min(page, total_pages)
            cur = db.execute(
                THREAD_SELECT + where + LISTING_ORDER + limit_offset,
                params + (SEARCH_PAGE_SIZE, (page - 1) * SEARCH_PAGE_SIZE))
            results = [_search_result(t) for t in cur]

        elif mode == "full":  # full-text over titles + post content (FTS5)
            match_expr = _fts_query(q)
            if match_expr:
                total_results = db.execute(
                    "SELECT COUNT(*) FROM thread_fts WHERE thread_fts MATCH ?",
                    (match_expr,)).fetchone()[0]
                total_pages = max(1, math.ceil(total_results / SEARCH_PAGE_SIZE))
                page = min(page, total_pages)
                # Unlikely delimiters so the raw snippet text can be
                # HTML-escaped first, THEN get real <mark> tags.
                cur = db.execute(
                    "SELECT t.*, ts.author AS _starter, "
                    "snippet(thread_fts, 2, '\u0001', '\u0002', ' … ', 40) "
                    "AS _snip "
                    "FROM thread_fts "
                    "JOIN threads t ON t.id = thread_fts.rowid "
                    "LEFT JOIN thread_starters ts ON ts.thread_id = t.id "
                    "WHERE thread_fts MATCH ? "
                    "ORDER BY bm25(thread_fts, 10.0, 2.0, 1.0) "
                    "LIMIT ? OFFSET ?",
                    (match_expr, SEARCH_PAGE_SIZE,
                     (page - 1) * SEARCH_PAGE_SIZE))
                for row in cur:
                    snip = escape(row["_snip"] or "")
                    snip = (snip.replace("\u0001", "<mark>")
                                .replace("\u0002", "</mark>"))
                    results.append(_search_result(row, snippet=snip))

        else:  # contributed — per-thread poster lists live in FTS
            match_expr = _fts_query(q)
            if match_expr:
                fts_where = "WHERE thread_fts MATCH ?"
                fts_param = (f"posters: ({match_expr})",)
                total_results = db.execute(
                    "SELECT COUNT(*) FROM thread_fts " + fts_where,
                    fts_param).fetchone()[0]
                total_pages = max(1, math.ceil(total_results / SEARCH_PAGE_SIZE))
                page = min(page, total_pages)
                cur = db.execute(
                    "SELECT t.*, ts.author AS _starter, f.posters AS _posters "
                    "FROM thread_fts f "
                    "JOIN threads t ON t.id = f.rowid "
                    "LEFT JOIN thread_starters ts ON ts.thread_id = t.id " +
                    fts_where + limit_offset,
                    fts_param + (SEARCH_PAGE_SIZE,
                                 (page - 1) * SEARCH_PAGE_SIZE))
                ql = q.lower()
                for t in cur:
                    names = sorted({n for n in (t["_posters"] or "").split()
                                    if ql in n.lower()})
                    results.append(_search_result(
                        t, matched_posters=names[:5] or None))

        page_items, prev_href, next_href = _paginate_qs(
            "search.html", q, mode, page, total_pages)

    return search_tpl.render(
        site_title=SITE_TITLE,
        rel="",
        snapshot_date=SITE.snapshot_date,
        sidebar=SITE.sidebar_ctx,
        show_search_panel=False,
        q=q, mode=mode,
        results=results,
        total_results=total_results,
        current_page=page, total_pages=total_pages,
        page_items=page_items, prev_href=prev_href, next_href=next_href,
    )


# The old separate full-search page is merged into /search.html as
# mode=full — redirect old bookmarks and cached links there.
@app.route("/full_search.html")
def full_search_redirect():
    q = (request.args.get("q") or "").strip()
    params = {"mode": "full"}
    if q:
        params["q"] = q
    return redirect(f"/search.html?{urlencode(params)}", code=301)


# Dev fallback — in production nginx serves /forum_assets/ directly.
@app.route("/forum_assets/<path:fname>")
def assets(fname):
    return send_from_directory(ASSETS_DIR, fname)


@app.errorhandler(404)
def not_found(e):
    return ("<html><body style='background:#0D1414;color:#E7EEEC;"
            "font-family:sans-serif;text-align:center;padding-top:80px'>"
            "<h1>404</h1><p>This page isn't in the archive. "
            "<a style='color:#5FE8DF' href='/'>Back to the index</a>"
            "</p></body></html>"), 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)
