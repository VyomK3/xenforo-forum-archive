#!/usr/bin/env python3
"""
generate_site.py — Build a static, read-only replica of a forum from
forum_index.db (threads / posts) plus forum_sections.xlsx
(Section / Sub-Section / Sub-Sub-Section / Description), using Jinja2
templates.

Usage:
    python3 generate_site.py \
        --db forum_index.db \
        --structure forum_sections.xlsx \
        --templates templates \
        --assets forum_assets \
        --output output

The xlsx is the source of truth for site structure and copy:
    Section           top-level segregation label on the home page (not
                      clickable, just groups the sub-sections under it)
    Sub-Section       the actual archived category — gets its own paginated
                      page (sections/<slug>.html, page 2+ as
                      sections/<slug>-page-2.html, ...) listing threads
                      whose thread.section_id matches a DB `sections` row
                      of this name, 20 threads per page
    Sub-Sub-Section   nested under its Sub-Section; gets its own paginated
                      page the same way. The parent Sub-Section's page links
                      to each of its Sub-Sub-Section pages.
    Description       shown at the top of the matching page

Output layout:
    output/
        index.html                  <- home page: just Section > Sub-Section
                                        links (no threads), with counts, plus
                                        a search box
        search.html                 <- search results page (client-side,
                                        title/content/poster, paginated)
        search_index.js             <- full-text search data, embedded as a
                                        <script src> (not fetched as JSON) so
                                        search works even opened via file://
        sections/<slug>.html        <- page 1 of a Sub-Section or
                                        Sub-Sub-Section's thread listing
        sections/<slug>-page-N.html <- subsequent pages, 20 threads each
        threads/<slug>.<id>.html    <- one page per thread, rendered with
                                        thread_template_dark.html
        forum_assets/...            <- copied avatar images actually
                                        referenced by archived posts

Re-run any time the database or spreadsheet is updated — the whole output
folder is regenerated from scratch (safe to delete and rebuild).
"""

import argparse
import json
import math
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from html import unescape as unescape_html
from itertools import groupby
from pathlib import Path
from urllib.parse import urlparse, unquote

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    sys.exit("Missing dependency. Install with:\n  pip install jinja2 beautifulsoup4 pandas openpyxl --break-system-packages")

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except ImportError:
    HAVE_BS4 = False

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency. Install with:\n  pip install pandas openpyxl --break-system-packages")


SITE_TITLE = "XENFORO-Archive"
SITE_SUBTITLE = "An attempt to have a read-only archive of xenforo based forum."
SITE_FOOTER_NOTE = (
    "No copyright infringement intended. This is a non-profit and "
    "non-monetized website, hosted purely for archival purposes by "
    "forum member who spent more than a decade of their life on it."
)
SITE_DEVELOPER_NOTE = "Site developed and maintained by Username."
PAGE_SIZE = 20

# Maps each top-level Section (xlsx "Section" column) to one of the icon
# <symbol> ids defined in home_template_dark.html's inline sprite. Any
# Section name not listed here (e.g. if the spreadsheet grows a new
# top-level category) falls back to DEFAULT_SECTION_ICON, so the build
# never breaks on an unmapped name.
SECTION_ICONS = {
    "News": "news",
    "Mobile Monsters": "mobile",
    "Hardware": "cpu",
    "Portables, Peripherals and Electronics": "camera",
    "Gaming": "gamepad",
    "Software": "code",
    "Interact": "megaphone",
    "Internet and Networking": "globe",
    "Community": "users",
    "Market": "cart",
    "Education and Career Guide": "cap",
    "Bandwidth Wastage": "coffee",
}
DEFAULT_SECTION_ICON = "folder"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def slugify(text, fallback):
    """Turn a title/name into a short, filesystem- and URL-safe slug."""
    if not text:
        return fallback
    text = str(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if not text:
        return fallback
    return text[:60].rstrip("-")


MAX_FILENAME_COMPONENT = 150  # safe headroom under Windows' 255-char NTFS
                               # per-component limit; also gives margin for
                               # cloud-sync clients (Nextcloud, etc.) that
                               # have been known to choke on very long
                               # unicode filenames before the OS limit hits


def _safe_filename(base_slug, unique_suffix):
    """
    <base_slug>.<unique_suffix>.html, length-capped for filesystem safety.
    The unique_suffix (thread/section id) is always kept in full, since
    that's what guarantees uniqueness — only the readable slug part is ever
    trimmed, and only when it doesn't fit.
    """
    tail = f".{unique_suffix}.html"
    budget = max(MAX_FILENAME_COMPONENT - len(tail), 10)
    if len(base_slug) > budget:
        base_slug = base_slug[:budget].rstrip("-") or "item"
    return f"{base_slug}{tail}"


def thread_filename(thread):
    """
    Reproduce the original site's own slug from its URL, e.g.:
    https://.../threads/need-career-advice-for-engineering-student.210412/
      -> need-career-advice-for-engineering-student.210412.html

    URL path segments are percent-encoded for non-ASCII characters (Hindi,
    Arabic, Chinese, ...), which are decoded here first — otherwise a title
    like "हिन्दी में टाइपिंग" (19 characters) turns into ~170 characters of
    literal "%E0%A4%B9%E0%A4%BF..." text, which blows past Windows' 255-char
    filename limit on plenty of real titles. The decoded slug is then capped
    to a safe length as a backstop (see _safe_filename); the numeric thread
    ID is always preserved in full for uniqueness, only the readable part is
    ever trimmed. Falls back to a generated slug if the URL is missing or
    unexpected.
    """
    thread_id = thread.get("thread_id") or str(thread["id"])
    url = thread.get("url")
    base_slug = None

    if url:
        path = urlparse(url).path.rstrip("/")
        last_segment = unquote(path.rsplit("/", 1)[-1]) if path else ""
        suffix = f".{thread_id}"
        if last_segment.endswith(suffix):
            base_slug = last_segment[: -len(suffix)]
        else:
            m = re.match(r"^(.+)\.(\d+)$", last_segment)
            if m:
                base_slug = m.group(1)

    if not base_slug:
        base_slug = slugify(thread.get("title"), f"thread-{thread_id}")

    return _safe_filename(base_slug, thread_id)


def page_filename(slug, page_num):
    return f"{slug}.html" if page_num == 1 else f"{slug}-page-{page_num}.html"


def build_pagination(slug, current, total):
    """Windowed page-number list with ellipsis gaps, e.g. 1 … 4 5 6 … 12."""
    pages = sorted({1, total, max(1, current - 1), current, min(total, current + 1)})
    items = []
    prev_p = None
    for p in pages:
        if prev_p is not None and p - prev_p > 1:
            items.append(None)
        items.append({"num": p, "href": page_filename(slug, p)})
        prev_p = p
    prev_href = page_filename(slug, max(1, current - 1))
    next_href = page_filename(slug, min(total, current + 1))
    return items, prev_href, next_href


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def html_to_text(html):
    """
    Fast plain-text extraction for the search index. Deliberately uses regex
    rather than BeautifulSoup: this runs on every post (not conditionally,
    like clean_content_html above), so on a database with hundreds of
    thousands of posts a full parse-tree build per post would meaningfully
    undo the earlier N+1-query performance fix. Regex tag-stripping plus
    entity-unescaping is more than accurate enough for search text.
    """
    if not html:
        return ""
    text = _SCRIPT_BLOCK_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = unescape_html(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


_IFRAME_TAG_RE = re.compile(r"<iframe\b[^>]*>", re.IGNORECASE)
_YOUTUBE_BBCODE_RE = re.compile(r"\[youtube\]\s*([a-zA-Z0-9_-]{6,20})\s*\[/youtube\]", re.IGNORECASE)


def _convert_youtube_bbcode(html):
    """
    A subset of older posts store the video as raw, never-rendered
    [youtube]VIDEO_ID[/youtube] BBCode instead of the <iframe>-based embed
    newer posts got server-rendered into (a bbMediaWrapper div). Left as-is
    this just prints as literal bracket text with no video at all — worse
    than error 153, there isn't even a link to click through. Convert it
    into the same markup the rendered posts use, iframe included.
    """
    def _replace(m):
        video_id = m.group(1)
        return (
            '<div class="bbMediaWrapper" data-media-key="{0}" data-media-site-id="youtube">'
            '<div class="bbMediaWrapper-inner">'
            '<iframe allowfullscreen="true" frameborder="0" height="315" '
            'src="https://www.youtube.com/embed/{0}"></iframe>'
            "</div></div>"
        ).format(video_id)
    return _YOUTUBE_BBCODE_RE.sub(_replace, html)


def _add_youtube_referrer_policy(html):
    """
    Since late 2025 YouTube strictly enforces referrer/origin checks on
    embedded players. Iframes scraped from the original forum (pre-dating
    that change) have no referrerpolicy attribute, so they now fail with
    "Error 153: Video player configuration error" — the video still opens
    fine via the "open in a new tab" link because that's a normal
    top-level navigation with a full referrer, only the in-page iframe is
    affected. The fix is simply to add
    referrerpolicy="strict-origin-when-cross-origin" to every YouTube
    iframe tag that doesn't already declare one.
    """
    def _inject(m):
        tag = m.group(0)
        tag_lower = tag.lower()
        if "youtube.com/embed" not in tag_lower and "youtube-nocookie.com/embed" not in tag_lower:
            return tag
        if "referrerpolicy=" in tag_lower:
            return tag
        return re.sub(
            r"^<iframe\b",
            '<iframe referrerpolicy="strict-origin-when-cross-origin"',
            tag,
            count=1,
            flags=re.IGNORECASE,
        )
    return _IFRAME_TAG_RE.sub(_inject, html)


def clean_content_html(html):
    """
    Strip the leftover <script> blocks the scraper captured inside post
    bodies (lightbox i18n JSON, etc), and patch YouTube iframe embeds so
    they don't hit the referrer-policy playback error (see
    _add_youtube_referrer_policy above). Everything else (images, quotes,
    formatting, smilies) is left exactly as scraped.

    Most posts don't contain a <script> tag or a YouTube iframe at all, so
    cheap substring checks first avoid building a full parse tree (the
    expensive part) for the overwhelming majority of posts. This matters a
    lot at scale — on a database with hundreds of thousands of posts,
    always-parsing turns into a real chunk of total runtime for no benefit
    on posts with nothing to strip or patch.
    """
    if not html:
        return ""
    lower = html.lower()
    has_script = "<script" in lower
    has_youtube_bbcode = "[youtube]" in lower
    if has_youtube_bbcode:
        html = _convert_youtube_bbcode(html)
        lower = html.lower()
    has_youtube_iframe = "<iframe" in lower and (
        "youtube.com/embed" in lower or "youtube-nocookie.com/embed" in lower
    )

    if has_youtube_iframe:
        html = _add_youtube_referrer_policy(html)

    if not has_script:
        return html

    if HAVE_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script"):
            tag.decompose()
        return str(soup)
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def fmt_int(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def clean_text(val):
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    text = str(val).strip()
    return text if text else None


def format_snapshot_date(raw):
    """Best-effort human-readable formatting; falls back to the raw string."""
    if not raw:
        return None
    candidate = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(candidate, fmt)
            return dt.strftime("%B %d, %Y")
        except ValueError:
            continue
    return candidate[:10]  # e.g. "2026-07-11" as a last resort


# --------------------------------------------------------------------------
# Structure loading (xlsx is the source of truth for site layout + copy)
# --------------------------------------------------------------------------

def load_structure(xlsx_path):
    """
    Returns an ordered list of:
      {"name": <Section>, "subsections": [
          {"name": <Sub-Section>, "description": str|None,
           "subsubsections": [{"name": <Sub-Sub-Section>, "description": str|None}, ...]},
          ...
      ]}
    preserving first-appearance order from the spreadsheet (which mirrors
    the original forum's own ordering).
    """
    df = pd.read_excel(xlsx_path)
    required = {"Section", "Sub-Section", "Sub-Sub-Section", "Description"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"'{xlsx_path}' is missing expected column(s): {', '.join(sorted(missing))}")

    mega_order = []
    mega_map = {}  # mega_name -> {sub_name: {"description": ..., "subsubs": [(name, desc), ...]}}

    for _, row in df.iterrows():
        mega_name = clean_text(row["Section"])
        sub_name = clean_text(row["Sub-Section"])
        subsub_name = clean_text(row["Sub-Sub-Section"])
        desc = clean_text(row["Description"])

        if not mega_name or not sub_name:
            continue  # skip malformed rows rather than crash the whole build

        if mega_name not in mega_map:
            mega_map[mega_name] = {}
            mega_order.append(mega_name)

        subs = mega_map[mega_name]
        if sub_name not in subs:
            subs[sub_name] = {"description": None, "subsubs": []}

        if subsub_name is None:
            subs[sub_name]["description"] = desc
        else:
            subs[sub_name]["subsubs"].append((subsub_name, desc))

    mega_sections = []
    for mega_name in mega_order:
        subsections = []
        for sub_name, data in mega_map[mega_name].items():
            subsections.append({
                "name": sub_name,
                "description": data["description"],
                "subsubsections": data["subsubs"],
            })
        mega_sections.append({"name": mega_name, "subsections": subsections})

    return mega_sections


# --------------------------------------------------------------------------
# Main build
# --------------------------------------------------------------------------

def build(db_path, xlsx_path, templates_dir, assets_dir, output_dir, light_content_limit=0, full_content_limit=0, search_only=False):
    t_start = time.time()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Read-only performance pragmas — none of these write to the db file,
    # they just let SQLite use more RAM for caching/sorting instead of disk,
    # which matters a lot on a 200MB+ database.
    cur.execute("PRAGMA cache_size = -131072")   # ~128MB page cache
    cur.execute("PRAGMA temp_store = MEMORY")
    cur.execute("PRAGMA mmap_size = 536870912")  # 512MB memory-mapped I/O

    output_dir = Path(output_dir)
    threads_out = output_dir / "threads"
    sections_out = output_dir / "sections"
    assets_out = output_dir / "forum_assets"
    dirs_needed = (output_dir,) if search_only else (output_dir, threads_out, sections_out)
    for d in dirs_needed:
        d.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
    home_tpl = env.get_template("home_template_dark.html")
    thread_tpl = env.get_template("thread_template_dark.html")
    section_tpl = env.get_template("section_page_template_dark.html")
    search_tpl = env.get_template("search_template_dark.html")
    full_search_tpl = env.get_template("full_search_template_dark.html")

    # ---- Load DB sections, keyed by exact name (as scraped) ----------------
    cur.execute("SELECT * FROM sections")
    db_sections_by_name = {row["name"].strip(): dict(row) for row in cur.fetchall()}

    # ---- Load threads + assign output filenames -----------------------------
    cur.execute("SELECT * FROM threads ORDER BY id")
    all_threads = [dict(r) for r in cur.fetchall()]

    used_thread_filenames = set()
    referenced_avatars = set()
    threads_by_section_id = {}
    latest_scrape_ts = None

    for th in all_threads:
        filename = thread_filename(th)
        base_filename = filename
        i = 2
        while filename in used_thread_filenames:
            stem = base_filename[:-5]  # strip ".html"
            filename = f"{stem}-{i}.html"
            i += 1
        used_thread_filenames.add(filename)
        th["filename"] = filename
        th["href"] = f"../threads/{filename}"  # pages that use this live under output/sections/

        for key in ("last_scraped_at", "scraped_at"):
            val = th.get(key)
            if val and (latest_scrape_ts is None or val > latest_scrape_ts):
                latest_scrape_ts = val

        th["replies_fmt"] = fmt_int(th.get("replies"))
        th["views_fmt"] = fmt_int(th.get("views"))

        threads_by_section_id.setdefault(th["section_id"], []).append(th)

    # ---- Bulk-load every post ONCE, grouped by thread in memory --------------
    # This is the single biggest performance fix: the previous version ran two
    # separate queries per thread (one for the first post's author, one for
    # the full post list). On an unindexed `posts` table that's an O(threads
    # x posts) full-table-scan pattern — with thousands of threads and
    # hundreds of thousands of posts that's billions of row comparisons,
    # which is what was causing multi-hour runs that never finished. A single
    # query ordered by thread_id, grouped in Python, is one sequential pass
    # over the posts table no matter how large the database is.
    print("Loading all posts in a single pass (this replaces thousands of "
          "per-thread queries — may take a moment on a large database)...")
    t_posts = time.time()
    cur.execute("SELECT * FROM posts ORDER BY thread_id, page_number, post_number")
    posts_by_thread = {
        thread_id: [dict(r) for r in group]
        for thread_id, group in groupby(cur, key=lambda r: r["thread_id"])
    }
    total_post_rows = sum(len(v) for v in posts_by_thread.values())
    print(f"Loaded {total_post_rows:,} posts across {len(posts_by_thread):,} threads "
          f"in {time.time() - t_posts:.1f}s.")

    # Pre-list the assets folder ONCE instead of doing a filesystem exists()
    # check per avatar per post — matters a lot when the folder is on a
    # network/cloud-synced drive, where each stat() call can be slow.
    assets_files = set()
    if assets_dir:
        assets_files = {p.name for p in Path(assets_dir).iterdir() if p.is_file()}

    for th in all_threads:
        raw_posts = posts_by_thread.get(th["id"], [])
        th["started_by"] = (
            (raw_posts[0].get("author") if raw_posts and raw_posts[0].get("author") else None)
            or th.get("poster")
            or "Unknown"
        )

    # ---- Site-wide totals & sidebar data (needed by every rendered page) -----
    total_threads = len(all_threads)
    cur.execute("SELECT COUNT(*) AS c FROM posts")
    total_posts = cur.fetchone()["c"]
    snapshot_date = format_snapshot_date(latest_scrape_ts)

    def fmt_compact(n):
        """1234 -> '1.2k', 950 -> '950' — for tight sidebar counts."""
        n = n or 0
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    # Most-viewed threads across the whole archive. Uses the thread table's
    # own sub_section column for the label (not breadcrumb_parts_by_section_id,
    # which doesn't exist yet at this point in the build).
    def _views_int(t):
        try:
            return int(t.get("views") or 0)
        except (TypeError, ValueError):
            return 0

    sidebar_top_threads = [{
        "title": t["title"],
        "href": f"threads/{t['filename']}",
        "replies": t["replies_fmt"],
        "views": t["views_fmt"],
        "section": (t.get("sub_section") or "").strip(),
    } for t in sorted(all_threads, key=_views_int, reverse=True)[:5]]

    def threads_for(name):
        """Sorted, template-ready thread list for a DB section matched by name."""
        db_sec = db_sections_by_name.get(name.strip())
        if not db_sec:
            return [], 0
        raw = threads_by_section_id.get(db_sec["id"], [])
        raw_sorted = sorted(
            raw,
            key=lambda t: (t.get("is_sticky") or 0, t.get("last_post_date_unix") or 0),
            reverse=True,
        )
        ready = [{
            "title": t["title"],
            "href": t["href"],
            "started_by": t["started_by"],
            "post_date_text": t.get("post_date_text") or "",
            "replies": t["replies_fmt"],
            "views": t["views_fmt"],
            "last_post_date_text": t.get("last_post_date_text") or "",
            "is_sticky": bool(t.get("is_sticky")),
            "is_closed": bool(t.get("is_closed")),
        } for t in raw_sorted]
        return ready, len(ready)

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

    def write_listing_pages(slug, name, mega_name, description, threads, show_own,
                             total_count, children, parent, source_url):
        if search_only:
            return
        total_pages = max(1, math.ceil(len(threads) / PAGE_SIZE)) if show_own else 1
        for page_num in range(1, total_pages + 1):
            page_threads = threads[(page_num - 1) * PAGE_SIZE: page_num * PAGE_SIZE] if show_own else []
            page_items, prev_href, next_href = build_pagination(slug, page_num, total_pages)
            html = section_tpl.render(
                site_title=SITE_TITLE,
                home_href="../index.html",
                rel="../",
                snapshot_date=snapshot_date,
                sidebar=sidebar_ctx,
                show_search_panel=True,
                slug=slug,
                mega_name=mega_name,
                name=name,
                title=name,
                description=description,
                total_count=total_count,
                source_url=source_url,
                children=children,
                parent=parent,
                show_own=show_own,
                threads=page_threads,
                current_page=page_num,
                total_pages=total_pages,
                page_items=page_items,
                prev_href=prev_href,
                next_href=next_href,
            )
            (sections_out / page_filename(slug, page_num)).write_text(html, encoding="utf-8")

    # ---- Build the Section > Sub-Section > Sub-Sub-Section hierarchy --------
    # This has to run BEFORE thread pages are rendered below (even though
    # conceptually it's "about" section listing pages, not threads) because
    # thread pages need breadcrumb_by_section_id / section_href_by_id too, to
    # show a real "Xenforo Archive / News / Technology News" breadcrumb instead of
    # a bare "Xenforo Archive / Thread". It only depends on all_threads (already
    # loaded above) and db_sections_by_name — not on post content — so this
    # ordering was always safe, just previously written the other way round.
    raw_structure = load_structure(xlsx_path)

    # Light pre-pass over the structure to build the sidebar's "Jump to
    # section" nav. This must exist BEFORE the main hierarchy loop below,
    # because section listing pages (which carry the sidebar) are rendered
    # inside that loop. Counting is a cheap dict-lookup per name — no
    # sorting, no template prep — so doing it twice costs nothing.
    def _count_for(name):
        db_sec = db_sections_by_name.get(name.strip())
        return len(threads_by_section_id.get(db_sec["id"], [])) if db_sec else 0

    sidebar_nav = []
    total_subsections = 0
    for mega in raw_structure:
        mega_count = 0
        for sub in mega["subsections"]:
            total_subsections += 1
            mega_count += _count_for(sub["name"])
            for subsub_name, _desc in sub["subsubsections"]:
                mega_count += _count_for(subsub_name)
        mega["anchor"] = "sec-" + slugify(mega["name"], "section")
        sidebar_nav.append({
            "name": mega["name"],
            "href": f"index.html#{mega['anchor']}",
            "count": fmt_compact(mega_count),
        })

    sidebar_ctx = {
        "stats": {
            "sections": fmt_int(len(raw_structure)),
            "subsections": fmt_int(total_subsections),
            "threads": fmt_int(total_threads),
            "posts": fmt_int(total_posts),
        },
        "nav": sidebar_nav,
        "top_threads": sidebar_top_threads,
    }

    mega_sections_home = []
    breadcrumb_by_section_id = {}
    breadcrumb_parts_by_section_id = {}
    section_href_by_id = {}

    for mega in raw_structure:
        subsections_home = []
        for sub in mega["subsections"]:
            sub_slug = make_slug(sub["name"], f"section-{len(used_slugs) + 1}")

            own_threads, own_count = threads_for(sub["name"])
            has_own_db_match = sub["name"].strip() in db_sections_by_name
            has_subsubs = bool(sub["subsubsections"])
            # Show this sub-section's own thread listing whenever it has a
            # matching DB category, OR it has no children at all (something
            # must be rendered either way). A pure container sub-section
            # (has children, no DB match of its own — e.g. "Online Shopping")
            # renders only its children links, no redundant empty state.
            show_own = has_own_db_match or not has_subsubs

            children_ctx = []
            children_home = []
            subsub_total = 0
            for subsub_name, subsub_desc in sub["subsubsections"]:
                ss_slug = make_slug(subsub_name, f"section-{len(used_slugs) + 1}")
                ss_threads, ss_count = threads_for(subsub_name)
                subsub_total += ss_count
                ss_db = db_sections_by_name.get(subsub_name.strip())
                ss_source_url = ss_db["url"] if ss_db else None
                if ss_db:
                    breadcrumb_by_section_id[ss_db["id"]] = f"{mega['name']} / {sub['name']} / {subsub_name}"
                    breadcrumb_parts_by_section_id[ss_db["id"]] = [mega["name"], sub["name"], subsub_name]
                    # threads/ and sections/ are sibling folders under the
                    # output root, so a thread page needs to go up one level
                    # before it can reach into sections/.
                    section_href_by_id[ss_db["id"]] = f"../sections/{page_filename(ss_slug, 1)}"

                children_ctx.append({
                    "name": subsub_name,
                    "href": page_filename(ss_slug, 1),
                    "thread_count": ss_count,
                })
                # Same sub-sub-section, but with a home-page-relative href
                # (home.html lives at the output root, not inside sections/).
                children_home.append({
                    "name": subsub_name,
                    "href": f"sections/{page_filename(ss_slug, 1)}",
                })

                write_listing_pages(
                    slug=ss_slug,
                    name=subsub_name,
                    mega_name=mega["name"],
                    description=subsub_desc,
                    threads=ss_threads,
                    show_own=True,
                    total_count=ss_count,
                    children=None,
                    parent={"name": sub["name"], "href": page_filename(sub_slug, 1)},
                    source_url=ss_source_url,
                )

            total_count = (own_count if show_own else 0) + subsub_total
            sub_db = db_sections_by_name.get(sub["name"].strip())
            sub_source_url = sub_db["url"] if sub_db else None
            if sub_db:
                breadcrumb_by_section_id[sub_db["id"]] = f"{mega['name']} / {sub['name']}"
                breadcrumb_parts_by_section_id[sub_db["id"]] = [mega["name"], sub["name"]]
                section_href_by_id[sub_db["id"]] = f"../sections/{page_filename(sub_slug, 1)}"

            write_listing_pages(
                slug=sub_slug,
                name=sub["name"],
                mega_name=mega["name"],
                description=sub["description"],
                threads=own_threads,
                show_own=show_own,
                total_count=total_count,
                children=children_ctx if children_ctx else None,
                parent=None,
                source_url=sub_source_url,
            )

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

    # ---- Render one page per thread ------------------------------------------
    total_threads_to_render = len(all_threads)
    t_render = time.time()
    for i, th in enumerate(all_threads, 1):
        raw_posts = posts_by_thread.get(th["id"], [])

        posts = []
        search_text_parts = []
        search_posters = []
        seen_posters = set()
        for p in raw_posts:
            author = p.get("author") or "Unknown"

            if not search_only:
                avatar = p.get("avatar_local_path")
                if avatar:
                    basename = Path(avatar).name
                    if assets_dir and basename in assets_files:
                        referenced_avatars.add(basename)
                        avatar = f"../forum_assets/{basename}"
                    else:
                        avatar = None
                posts.append({
                    "author": author,
                    "author_profile_url": p.get("author_profile_url"),
                    "avatar_local_path": avatar,
                    "timestamp_display": p.get("timestamp_display") or "",
                    "content_html": clean_content_html(p.get("content_html")),
                    "post_number": p.get("post_number"),
                })

            # Collected alongside thread-page rendering (not a second pass
            # over posts) for the search index built after this loop.
            text_piece = html_to_text(p.get("content_html"))
            if text_piece:
                search_text_parts.append(text_piece)
            if author not in seen_posters:
                seen_posters.add(author)
                search_posters.append(author)

        th["_search_content"] = " ".join(search_text_parts)
        th["_search_posters"] = search_posters

        if search_only:
            if i % 5000 == 0 or i == total_threads_to_render:
                print(f"Indexed {i:,}/{total_threads_to_render:,} threads "
                      f"({time.time() - t_render:.1f}s elapsed)...")
            continue

        thread_ctx = {
            "title": th["title"],
            "url": th["url"],
            "breadcrumb_parts": breadcrumb_parts_by_section_id.get(th["section_id"]),
            "breadcrumb_href": section_href_by_id.get(th["section_id"]),
        }
        html = thread_tpl.render(
            site_title=SITE_TITLE,
            home_href="../index.html",
            rel="../",
            snapshot_date=snapshot_date,
            sidebar=sidebar_ctx,
            show_search_panel=True,
            thread=thread_ctx,
            posts=posts,
            post_count=len(posts),
        )
        (threads_out / th["filename"]).write_text(html, encoding="utf-8")

        if i % 500 == 0 or i == total_threads_to_render:
            print(f"Rendered {i:,}/{total_threads_to_render:,} thread pages "
                  f"({time.time() - t_render:.1f}s elapsed)...")

    # ---- Render home page -------------------------------------------------------
    # (total_threads / total_posts / snapshot_date were computed earlier,
    # before rendering began, since the sidebar on every page needs them.)
    html = home_tpl.render(
        site_title=SITE_TITLE,
        site_subtitle=SITE_SUBTITLE,
        footer_note=SITE_FOOTER_NOTE,
        developer_note=SITE_DEVELOPER_NOTE,
        mega_sections=mega_sections_home,
        total_subsections=total_subsections,
        total_threads=total_threads,
        total_posts=total_posts,
        snapshot_date=snapshot_date,
    )
    if not search_only:
        (output_dir / "index.html").write_text(html, encoding="utf-8")

    # ---- Build & write the two search indexes ---------------------------------
    # Embedded as <script src> (not fetched as JSON) so search works even when
    # the site is opened directly from disk (file://) — browsers block
    # fetch()/XHR of local files in many configurations, but a plain
    # <script src="..."> load is unaffected by that restriction.
    #
    # Two separate indexes, because the regular search page's three modes
    # ("Search in thread titles only", "Started by poster", "Poster
    # contributed") never match against post content at all — only title and
    # poster names. Shipping full post text to every visitor just for those
    # three modes was the single biggest contributor to index size. Full-text
    # keyword search now lives on its own "Full Thread Search" page, backed
    # by a second, separate index that's the only one to carry post content.
    #
    # Both indexes are STREAMED straight to disk, one record at a time —
    # never assembled as a Python list, never passed through json.dumps() on
    # the whole collection, never held as one giant string. At six-figure
    # thread counts with full post content, building the entire index as a
    # single string (or worse, several of them back to back: a list of
    # dicts, then its json.dumps() string, then an f-string copy of that,
    # then write_text()'s own internal encode of the whole thing to bytes)
    # is exactly what runs a multi-hundred-MB index up into the multi-GB
    # range in peak memory and is the direct cause of a MemoryError here.
    # Writing record-by-record keeps peak memory for this step down to
    # roughly one thread's content at a time, regardless of how many
    # threads there are in total.
    def iter_search_records(content_limit, omit_if_zero):
        for th in all_threads:
            record = {
                "t": th["title"],
                "h": f"threads/{th['filename']}",
                "s": breadcrumb_by_section_id.get(th["section_id"], ""),
                "o": th["started_by"],
                "p": th.get("_search_posters") or [],
                "r": th["replies_fmt"],
                "v": th["views_fmt"],
                "pd": th.get("post_date_text") or "",
                "ld": th.get("last_post_date_text") or "",
            }
            if th.get("is_sticky"):
                record["st"] = 1
            if th.get("is_closed"):
                record["cl"] = 1
            if content_limit or not omit_if_zero:
                content = th.get("_search_content") or ""
                record["c"] = content[:content_limit] if content_limit else content
            yield record

    def write_search_index_js(path, var_name, records):
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"var {var_name}=[")
            first = True
            for record in records:
                if not first:
                    f.write(",")
                first = False
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            f.write("];")
        return path.stat().st_size

    print("Building search indexes...")
    t_search = time.time()

    light_index_bytes = write_search_index_js(
        output_dir / "search_index.js", "SEARCH_INDEX",
        iter_search_records(light_content_limit, omit_if_zero=True),
    )
    full_index_bytes = write_search_index_js(
        output_dir / "search_index_full.js", "SEARCH_INDEX_FULL",
        iter_search_records(full_content_limit, omit_if_zero=False),
    )

    print(f"Light search index (titles/posters): {len(all_threads):,} threads, "
          f"{fmt_bytes(light_index_bytes)}")
    print(f"Full search index (title+content):   {len(all_threads):,} threads, "
          f"{fmt_bytes(full_index_bytes)} ({time.time() - t_search:.1f}s total).")

    if not search_only:
        search_html = search_tpl.render(
            site_title=SITE_TITLE,
            site_subtitle=SITE_SUBTITLE,
            rel="",
            snapshot_date=snapshot_date,
            sidebar=sidebar_ctx,
            show_search_panel=False,
        )
        (output_dir / "search.html").write_text(search_html, encoding="utf-8")

        full_search_html = full_search_tpl.render(
            site_title=SITE_TITLE,
            site_subtitle=SITE_SUBTITLE,
            rel="",
            snapshot_date=snapshot_date,
            sidebar=sidebar_ctx,
            show_search_panel=False,
        )
        (output_dir / "full_search.html").write_text(full_search_html, encoding="utf-8")

    # ---- Copy only the avatar images actually used -----------------------------
    copied = 0
    if not search_only and assets_dir and referenced_avatars:
        assets_out.mkdir(parents=True, exist_ok=True)
        assets_src = Path(assets_dir)
        for name in referenced_avatars:
            src = assets_src / name
            if src.exists():
                shutil.copy2(src, assets_out / name)
                copied += 1

    conn.close()

    if search_only:
        print("Search-only build: no HTML pages were (re)written, only search_index.js.")
    print(f"Top-level sections: {len(mega_sections_home)}")
    print(f"Sub-sections:       {total_subsections}")
    print(f"Threads:            {total_threads}")
    print(f"Posts:              {total_posts}")
    print(f"Avatars copied:     {copied}")
    print(f"Search indexes:      {fmt_bytes(light_index_bytes)} light, {fmt_bytes(full_index_bytes)} full")
    print(f"Snapshot date:      {snapshot_date}")
    print(f"Total build time:   {time.time() - t_start:.1f}s")
    print(f"Output written to:  {output_dir.resolve()}")


SYNC_FOLDER_HINTS = ("nextcloud", "onedrive", "dropbox", "icloud", "google drive", "syncthing")


def warn_if_sync_folder(path, label):
    resolved = str(Path(path).resolve()).lower()
    for hint in SYNC_FOLDER_HINTS:
        if hint in resolved:
            print(
                f"Note: {label} path appears to be inside a cloud-sync folder "
                f"({hint}). Writing/reading thousands of small files there can be "
                f"very slow, since the sync client tries to watch/hash/upload each "
                f"one as it changes. If this run is slow, try pointing --output at "
                f"a local, non-synced folder and moving the finished 'output' "
                f"folder into place afterward (pausing sync while you do so)."
            )
            return


def main():
    parser = argparse.ArgumentParser(description="Build a static XENFORO-Archive replica from forum_index.db + forum_sections.xlsx")
    parser.add_argument("--db", default="forum_index.db", help="Path to forum_index.db")
    parser.add_argument("--structure", default="forum_sections.xlsx", help="Path to the Section/Sub-Section/Sub-Sub-Section/Description spreadsheet")
    parser.add_argument("--templates", default="templates", help="Folder containing home_template_dark.html, section_page_template_dark.html, thread_template_dark.html")
    parser.add_argument("--assets", default="forum_assets", help="Folder containing avatar images referenced by avatar_local_path (optional)")
    parser.add_argument("--output", default="output", help="Output folder for the generated site")
    parser.add_argument("--light-content-limit", type=int, default=0,
                         help="Max characters of post text to include in the LIGHT search "
                              "index (search_index.js), used by the regular search page's "
                              "3 modes: 'Search in thread titles only', 'Started by poster', "
                              "and 'Poster contributed'. None of those actually match against "
                              "post content, so the default (0) omits content from this index "
                              "entirely — the single biggest lever for keeping it small. Only "
                              "raise this if you want a content preview snippet shown there.")
    parser.add_argument("--full-content-limit", type=int, default=0,
                         help="Max characters of post text to include in the FULL search "
                              "index (search_index_full.js), used only by the 'Full Thread "
                              "Search' page for real keyword-in-content search. 0 = unlimited/"
                              "full text (the default). If this file is too large in practice, "
                              "try e.g. 2000-5000 to cap it — titles and posters are never "
                              "truncated in either index.")
    parser.add_argument("--search-only", action="store_true",
                         help="Only rebuild search_index.js and search_index_full.js — skips "
                              "writing thread pages, section listing pages, and the home page "
                              "entirely. Use this to quickly retune the two --*-content-limit "
                              "flags on a large database without waiting through a full site "
                              "rebuild each time. Requires that you've already done at least "
                              "one full build into --output, since it doesn't touch the other "
                              "pages already there.")
    args = parser.parse_args()

    if not Path(args.db).exists():
        sys.exit(f"Database not found: {args.db}")
    if not Path(args.structure).exists():
        sys.exit(f"Structure spreadsheet not found: {args.structure}")
    if not Path(args.templates).exists():
        sys.exit(f"Templates folder not found: {args.templates}")

    assets_dir = args.assets if Path(args.assets).exists() else None
    if assets_dir is None:
        print(f"Note: assets folder '{args.assets}' not found — avatars will fall back to letter placeholders.")

    warn_if_sync_folder(args.db, "database")
    warn_if_sync_folder(args.output, "output")

    build(args.db, args.structure, args.templates, assets_dir, args.output, args.light_content_limit, args.full_content_limit, args.search_only)


if __name__ == "__main__":
    main()
