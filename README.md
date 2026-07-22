# xenforo-archiver

A four-stage toolchain for archiving any **XenForo 2.x** forum: crawl the forum
tree, scrape every thread's posts into SQLite, and then publish the archive
either as a **static site** (one HTML file per thread) or as a **Flask app**
served from the database on a VPS.

Throughout this guide the forum being archived is referred to as
`https://forum.site/community/` — replace that with your own target forum.
The deployed archive is referred to as `archive.example.com`.

---

## The pipeline at a glance

```
  ┌─────────────────────────────────────────────────────────────┐
  │ 1. scrape_forum_threads.py                                  │
  │    Crawls categories → sub-forums → thread listings.        │
  │    Writes: sections, threads, crawl_log                     │
  └───────────────────────────┬─────────────────────────────────┘
                              ▼   forum_index.db
  ┌─────────────────────────────────────────────────────────────┐
  │ 2. scrape_thread_to_db.py                                   │
  │    Visits each thread, saves every post + avatars/images.   │
  │    Writes: posts (+ scrape tracking cols on threads)        │
  └───────────────────────────┬─────────────────────────────────┘
                              ▼   forum_index.db + forum_assets/
              ┌───────────────┴───────────────┐
              ▼                               ▼
  ┌───────────────────────┐       ┌───────────────────────────┐
  │ 3. generate_site.py   │       │ 4. app.py + prepare_db.py │
  │    STATIC build       │  OR   │    DYNAMIC Flask server   │
  │    output/*.html      │       │    gunicorn + nginx       │
  └───────────────────────┘       └───────────────────────────┘
```

**Stages 1 and 2 are always required.** Stages 3 and 4 are two *alternative*
ways to publish the same database — you do not need both.

### Static (stage 3) vs. database-backed (stage 4)

|  | **3 — Static site** | **4 — Flask + database** |
|---|---|---|
| How pages are made | Pre-rendered once, thousands of `.html` files | Rendered per request from SQLite |
| Hosting needed | Any static host, GitHub Pages, S3, or a plain folder on disk | A VPS running Python, gunicorn and nginx |
| Works offline / `file://` | Yes — double-click `index.html` | No |
| Search | Client-side JS over `search_index.js`, downloaded by every visitor (can be 100 MB+ on a large archive) | Server-side SQLite FTS5, milliseconds, visitor downloads nothing |
| Updating content | Full rebuild of `output/` | Re-run `prepare_db.py`, restart the service |
| Updating a template | Full rebuild | Edit file, restart service |
| Running cost | Effectively zero | Cost of a small VPS |
| Best for | Portability, long-term preservation, handing someone a ZIP | Large archives, good search, live-ish sites |

They share the same URL layout and the same templates, so you can start static
and move to Flask later (or run both) without changing any links.

---

## Requirements

```bash
pip install requests beautifulsoup4 tqdm jinja2 pandas openpyxl
# add for stage 4:
pip install flask gunicorn
```

On Debian/Ubuntu with a managed Python you may need
`--break-system-packages`, or better, use a virtualenv.

## Repository layout

```
xenforo-archiver/
├── scrape_forum_threads.py     # stage 1 — index the forum tree
├── scrape_thread_to_db.py      # stage 2 — scrape posts
├── dedupe_posts.py             # maintenance — remove duplicate posts rows
├── generate_site.py            # stage 3 — static build (also a library for stage 4)
├── app.py                      # stage 4 — Flask app
├── prepare_db.py               # stage 4 — one-time DB preparation
├── forum_sections.xlsx         # Section / Sub-Section / Sub-Sub-Section / Description
├── templates/
│   ├── home_template_dark.html
│   ├── section_page_template_dark.html
│   ├── thread_template_dark.html
│   ├── search_template_dark.html
│   ├── search_server_dark.html      # stage 4 only
│   ├── _shell_css.html
│   ├── _topbar.html
│   └── _sidebar.html
├── deploy/
│   ├── archiver.service
│   ├── archiver-cache.conf
│   └── archive.example.com.conf
├── forum_index.db              # produced by stages 1–2
├── forum_assets/               # avatars + inline images
├── test_parsing.py
└── test_integration.py
```

---

# Stage 1 — Index the forum threads

`scrape_forum_threads.py` crawls the entire XenForo forum tree
(categories → sub-forums → thread listings) and stores an index of every
thread in a local SQLite database. It does **not** fetch post content — that is
stage 2.

## Run it

```bash
# Full crawl of the whole forum
python3 scrape_forum_threads.py

# Custom db path / speed
python3 scrape_forum_threads.py --db forum_index.db --delay 1.0

# If it gets interrupted (Ctrl+C, network blip, etc.), just resume:
python3 scrape_forum_threads.py --db forum_index.db --resume

# Crawl only a specific sub-forum (e.g. to test first):
python3 scrape_forum_threads.py \
  --start-url https://forum.site/community/chit-chat/ --max-pages 3
```

**Recommended first run:** try `--start-url` against one small sub-forum with
`--max-pages 2` to confirm everything works on the live site (theme tweaks can
shift a CSS class), then kick off the full crawl.

## Scale and politeness

A long-running forum can have many categories, each with hundreds of pages of
threads. A full crawl means a lot of HTTP requests.

- `--delay` (default `1.0`s) is a politeness delay between requests. Don't set
  it too low — be a good citizen of someone else's server.
- Every forum-listing page fetched is logged to a `crawl_log` table, so
  `--resume` lets you stop and pick back up without re-fetching everything or
  double-counting threads. Thread rows are **upserted by URL**, so re-running is
  always safe.
- Expect a full crawl to take tens of minutes to several hours depending on
  delay and thread count. This is a one-time historical index — no need to rush.

## What gets stored

### `sections`

| column | meaning |
|---|---|
| `id` | internal id |
| `url` | canonical forum/category URL |
| `name` | section or sub-section name |
| `parent_id` | id of parent section (NULL for top-level) |
| `top_level_name` | name of the top-level category this rolls up to |
| `depth` | 1 = top-level category, 2 = sub-forum, 3 = sub-sub-forum, etc. |
| `description` | the blurb XenForo shows under the section name |

### `threads`

| column | meaning |
|---|---|
| `thread_id` | XenForo's internal thread id (parsed from the URL) |
| `url` | full thread URL (unique key) |
| `title` | thread title |
| `prefix` | thread prefix/tag if any (e.g. "Sticky", "Solved") |
| `section_id` | FK to `sections.id` (the immediate forum it lives in) |
| `top_level_section` | e.g. "Mobile Phones & Tablets" |
| `sub_section` | the immediate sub-forum name |
| `poster` | thread starter's username |
| `post_date_unix` / `post_date_text` | when the thread was started |
| `replies` | reply count |
| `views` | view count |
| `last_post_date_unix` / `last_post_date_text` | most recent reply time |
| `last_post_author` | most recent poster |
| `is_sticky` / `is_closed` | flags |
| `scraped_at` | when this row was last (re-)scraped |

### `crawl_log`

Bookkeeping for `--resume`: every forum-listing page URL fetched, its status
(`ok` / `error`), and how many threads were found on it.

## Example queries once populated

```sql
-- Busiest sub-forums by thread count
SELECT sub_section, COUNT(*) AS threads
FROM threads GROUP BY sub_section ORDER BY threads DESC;

-- Most-replied threads overall
SELECT title, replies, url FROM threads ORDER BY replies DESC LIMIT 20;

-- All threads by a specific poster
SELECT title, top_level_section, sub_section, url
FROM threads WHERE poster = 'SomeUsername';

-- Thread activity by year
SELECT strftime('%Y', datetime(post_date_unix, 'unixepoch')) AS year,
       COUNT(*) FROM threads GROUP BY year ORDER BY year;
```

## Testing

- `test_parsing.py` — unit tests for the HTML parsing functions against
  representative XenForo markup (no network needed).
- `test_integration.py` — spins up a tiny local mock forum server and runs the
  real crawler against it end-to-end, verifying the database ends up correctly
  populated (no network needed).

```bash
python3 test_parsing.py
python3 test_integration.py
```

## Caveats

- Selectors target standard XenForo 2.x markup (`.structItem--thread`,
  `.node-list`, `.pageNav-main`, etc.). If the target forum's theme has custom
  overrides, tweak the selectors in `parse_thread_list` / `get_child_forums`.
- Very old or heavily customised threads might not parse every field (e.g. a
  theme that hides "Views") — those fields end up `NULL` rather than crashing
  the crawl.

---

# Stage 2 — Scrape thread contents into the database

`scrape_thread_to_db.py` visits threads and saves their posts into the `posts`
table, downloading avatars and inline images into an assets folder along the way.

## How thread URLs are provided

Exactly one of the following must be given:

| Source | How |
|---|---|
| Single thread | Pass the URL as a plain positional argument |
| Multiple threads (list) | `--csv threads.csv` |
| Multiple threads (from a database) | `--from-db` |

## Where data goes

Regardless of source, scraped posts are always written into the `posts` table of
the database given by `--db`. With `--from-db`, the threads table being read and
the posts table being written are in the **same** database file.

If a `threads` row for a URL doesn't exist yet, the script creates one, using
whichever columns actually exist in that database's `threads` table — so it
adapts to a bare fresh database or to a rich one produced by stage 1. If a
`threads` row **already** exists (the normal case with `--from-db`), the script
leaves all existing columns alone and only touches two tracking columns it adds
itself:

- `last_scraped_at` — timestamp of the most recent scrape attempt
- `last_scrape_status` — `ok`, `error`, or `stale` (the last set by
  `--check-updates`; see *Re-scraping and fetching new posts* below)

## Re-scraping and fetching new posts

Re-running the scraper over threads it has already seen is **incremental and
duplicate-safe**, and there is a dedicated mode for pulling in new replies that
have appeared since the archive was made.

### Duplicate-safe by design

Posts are keyed by their forum post id. The `posts` table carries a unique index
on `(thread_id, site_post_id)`, and every insert is an `INSERT OR IGNORE`, so a
post already stored is never saved twice. The script creates this index
automatically on first run. (If an *older* database already contains duplicates,
the index can't be built until they're removed — see the `dedupe_posts.py`
maintenance section below.)

Because of that, **re-scraping a thread only appends its genuinely new posts** —
the posts already stored are left untouched:

- **It resumes from the last stored page.** New posts land at the end of a
  thread, so a thread previously scraped in full is re-fetched from its last
  stored page (which may have gained posts) plus any newer pages — not from page
  1 — and images for posts already held are not re-downloaded. A thread whose
  last attempt didn't finish cleanly (`last_scrape_status` is `error`/absent)
  falls back to a full page-1 pass so any gaps are filled.
- **Edits and deletions are not picked up by default.** Since existing posts are
  skipped, a post edited or deleted on the forum after archiving keeps its stored
  copy. Use `--rescrape-all` to force a clean rebuild (wipe each thread's posts
  and re-insert from scratch), which reflects upstream edits and deletions.

### Fetching new posts on already-archived threads (`--check-updates`)

Once a forum is archived, threads keep getting replies. To pull in just the new
posts without re-scraping everything:

1. **Refresh the index (stage 1).** Re-run `scrape_forum_threads.py` against the
   same database. Thread rows are upserted by URL, so this updates each thread's
   `replies` and `last_post_date_*` to the forum's current values — cheaply, from
   listing pages (dozens of threads per request), not one request per thread.
2. **Scrape the grown threads (stage 2 with `--check-updates`).** This compares
   each already-`ok` thread's refreshed `replies` count against how many posts
   are actually stored for it, marks the ones that gained posts as `stale`, and
   re-scrapes exactly those (incrementally, so only the new posts are fetched).

```bash
# 1. refresh reply counts / last-post dates from the live forum
python3 scrape_forum_threads.py --db forum_index.db --resume

# 2. preview which threads have new posts, without scraping
python3 scrape_thread_to_db.py --from-db --db forum_index.db --check-updates --check-only

# 3. mark grown threads and scrape their new posts
python3 scrape_thread_to_db.py --from-db --db forum_index.db --check-updates
```

Step 1 is essential: until the index is refreshed, the stored `replies` value is
the snapshot from the original crawl and won't reveal new posts. `--check-updates`
requires the `replies` column, which a stage-1 database has.

## All parameters

### Source (pick one)

| Parameter | Description |
|---|---|
| `thread_url` (positional) | URL of a single forum thread (any page of it). |
| `--csv CSV_PATH` | CSV file listing one thread URL per line (no header needed). |
| `--from-db` | Read thread URLs from the `threads` table (`url` column) of `--db`. Posts are saved back into that same database. |

### Common options

| Parameter | Default | Description |
|---|---|---|
| `--db DB` | `forum_data.db` | SQLite database to read from (with `--from-db`) and write posts into. |
| `--assets-dir DIR` | `forum_assets` | Where avatars and inline images are saved. Downloads are deduplicated across threads. |
| `--delay SECONDS` | `1.0` | Seconds between page requests (politeness delay). |

### Skipping already-scraped threads

| Parameter | Description |
|---|---|
| `--skip-existing` | For single-URL / `--csv` mode: skip a thread whose `last_scrape_status` is already `ok`. (Off by default in these modes.) |
| `--rescrape-all` | Force a **full rebuild** of each scraped thread: wipe its stored posts and re-insert from scratch, so upstream edits and deletions are reflected (the default appends only new posts — see *Re-scraping and fetching new posts* above). With `--from-db` it **also** re-scrapes threads already marked `ok` (normally skipped). |

A thread whose last attempt *failed* (`last_scrape_status = error`), or that was
never scraped at all, is never skipped — only a prior `ok` counts as "already
scraped."

### Detecting new posts (only valid with `--from-db`)

| Parameter | Description |
|---|---|
| `--check-updates` | Before scraping, mark already-`ok` threads whose forum `replies` count now exceeds their stored post count as `stale`, then scrape those. Only threads that **gained** posts are chased; upstream deletions are ignored. Refresh the index (stage 1) first so `replies` is current. Requires a `replies` column. |
| `--check-only` | With `--check-updates`: do the marking, then stop **without** scraping (review first, then re-run with a plain `--from-db`). |
| `--include-closed` | With `--check-updates`: also check closed/locked threads. Excluded by default, since a locked thread shouldn't gain posts — and this avoids accidentally re-scraping tens of thousands of them. |

### Filtering (only valid with `--from-db`)

| Parameter | Description |
|---|---|
| `--poster NAME` | Only scrape threads whose `poster` column matches `NAME` (case-insensitive, exact match). Repeat to match multiple posters. |
| `--thread-id ID` | Only scrape thread(s) whose `thread_id` equals `ID` (the forum's own id from the URL — not the database's internal row `id`). Repeat for multiple ids. |

If both are given, a thread must match at least one of each: `--poster` values
are OR'd, `--thread-id` values are OR'd, and the two groups are AND'd.

### Database backups (protection against corruption)

| Parameter | Default | Description |
|---|---|---|
| `--backup-every N` | `200` | After every `N` **successfully** scraped threads, snapshot `--db`. `0` disables periodic checkpoints (a final backup still happens unless `--no-backup`). |
| `--no-backup` | off | Disable all backups for this run — periodic and final. |

A backup is always taken at the very end of a run (success or failure) and, as a
safety net, right before exit if the script crashes partway through.

Backups are saved **in the same directory as `--db`**, named:

```
forum_index_backup_20260713_153211_873492.db
```

(`<db_stem>_backup_<YYYYMMDD>_<HHMMSS>_<microseconds>.db` — the microsecond
suffix keeps filenames unique even if two fire in the same second.)

Backups use SQLite's own online backup API (`sqlite3.Connection.backup`), not a
plain file copy. This snapshots the database in a structurally consistent state
even if captured mid-write, which is what makes it a genuine defence against the
sort of corruption a raw file copy could itself introduce.

### External run log

| Parameter | Default | Description |
|---|---|---|
| `--log-file PATH` | `<db_stem>_scrape_log.txt` next to `--db` | Plain-text log. Every run appends (never overwrites) — a running history across all runs. |

Each line is timestamped and records run start (source mode, `--db`, backup
setting), every backup taken (checkpoint number / end-of-run / pre-crash, plus
path), and run end (duration, succeeded / failed / skipped counts).

```
[2026-07-13 15:30:02] RUN START | from-db (poster=['someuser']) | db=forum_index.db | backups=every 200 threads (+ final)
[2026-07-13 15:41:57] BACKUP | checkpoint at 200 threads | /data/forum_index_backup_20260713_154157_112233.db
[2026-07-13 15:53:40] BACKUP | end of run | /data/forum_index_backup_20260713_155340_998877.db
[2026-07-13 15:53:40] RUN END | duration=23m 38s | succeeded=350 failed=2 skipped=40 | backups_taken=2 | db=forum_index.db
```

### Live console visibility

Console output is prefixed with a wall-clock timestamp:

```
[15:41:12] [147/500] https://forum.site/community/threads/some-thread.123456/
[15:41:57]   Backup checkpoint: 200 threads scraped successfully (147/500 processed overall) -> /data/forum_index_backup_20260713_154157_112233.db
```

The progress bar carries a running postfix so you always know how things are
going without waiting for the log file:

```
Threads:  29%|██▉ | 147/500 [12:34<28:11, next_bkup=in 53 ok=140 fail=2 skip=5]
```

- `next_bkup` — successful scrapes remaining until the next `--backup-every`
  checkpoint, capped at the number of threads left in the run (so a short batch
  shows an honest `in 5` rather than `in 200`); `off` if backups are disabled
- `ok` / `fail` / `skip` — running counts of successful, failed, skipped threads

## Examples

```bash
# Single thread into a fresh/default database
python3 scrape_thread_to_db.py \
  "https://forum.site/community/threads/some-thread-title.137002/"

# Single thread into a specific database and assets folder
python3 scrape_thread_to_db.py \
  "https://forum.site/community/threads/some-thread-title.137002/" \
  --db forum_data.db --assets-dir forum_assets

# A batch listed in a CSV, skipping ones already scraped successfully
python3 scrape_thread_to_db.py --csv threads.csv --db forum_data.db --skip-existing

# Every thread indexed in stage 1 (already-ok threads skipped by default)
python3 scrape_thread_to_db.py --from-db --db forum_index.db --assets-dir forum_assets

# Fetch new posts on already-archived threads that have since grown
# (refresh the index with stage 1 first — see "Fetching new posts" above)
python3 scrape_thread_to_db.py --from-db --db forum_index.db --check-updates

# Force a full rebuild (wipe + re-insert) to capture upstream edits/deletions
python3 scrape_thread_to_db.py --from-db --db forum_index.db --rescrape-all

# Only threads started by specific posters
python3 scrape_thread_to_db.py --from-db --db forum_index.db --poster johndoe --poster janedoe

# Only specific threads by forum thread id
python3 scrape_thread_to_db.py --from-db --db forum_index.db --thread-id 137002 --thread-id 140120

# Poster filter with a slower, extra-polite delay
python3 scrape_thread_to_db.py --from-db --db forum_index.db --poster johndoe --delay 2.5

# Back up every 50 successful threads instead of 200
python3 scrape_thread_to_db.py --from-db --db forum_index.db --backup-every 50

# No backups at all (not recommended, but available)
python3 scrape_thread_to_db.py --from-db --db forum_index.db --no-backup

# Custom log file location
python3 scrape_thread_to_db.py --from-db --db forum_index.db --log-file /var/log/forum_scrape.log
```

## Notes

- `--poster` and `--thread-id` are rejected with an error without `--from-db`,
  since there is no threads row to filter on until after the fetch happens.
- Two live progress bars are shown: overall thread progress, and page progress
  within the thread currently being scraped.
- Only **successful** scrapes count toward the `--backup-every` checkpoint.
- Backup files accumulate — the script never deletes old ones, so periodically
  clean out `*_backup_*.db` files you no longer need.

---

# Maintenance — remove duplicate posts (`dedupe_posts.py`)

The stage-2 scraper prevents duplicate posts from being created in the first
place (unique index on `(thread_id, site_post_id)` + `INSERT OR IGNORE`).
`dedupe_posts.py` is for cleaning a database that *already* contains
duplicates — e.g. one built by an older version of the scraper that
wiped-and-reinserted, or a thread that got scraped twice under two slightly
different URLs. It's also what to run if stage 2 warns it couldn't create the
unique index because duplicates already exist.

## What counts as a duplicate

Rows sharing the same `(thread_id, site_post_id)` — the forum's own post id, so a
shared value means the exact same real post stored more than once. Rows with a
missing/blank `site_post_id` fall back to grouping by
`(thread_id, page_number, post_number)`. Within each group one row is kept — the
oldest by internal `id` by default, or the newest with `--keep-latest` — and the
rest are deleted.

## Run it

```bash
# Dry run (default) — reports what it found, deletes nothing
python3 dedupe_posts.py --db forum_index.db

# Write every duplicate row (marked KEEP/DELETE) to a CSV for manual review first
python3 dedupe_posts.py --db forum_index.db --report-csv duplicates.csv

# Actually remove them (takes an automatic safety backup first)
python3 dedupe_posts.py --db forum_index.db --apply

# Keep the newest copy of each duplicate instead of the oldest
python3 dedupe_posts.py --db forum_index.db --apply --keep-latest
```

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `--db DB` | *(required)* | SQLite database to clean. |
| `--apply` | off (dry run) | Actually delete duplicates. Without it, the script only reports what it would remove. |
| `--keep-latest` | off | Keep the newest (highest `id`) row in each group instead of the oldest. |
| `--report-csv PATH` | — | Write full details of every duplicate row, marked `KEEP`/`DELETE`, to a CSV for review. |
| `--no-backup` | off | Skip the automatic pre-delete backup (only relevant with `--apply`; not recommended). |

Safe by default: it's a **dry run unless you pass `--apply`**, and `--apply` takes
a consistent SQLite-backup snapshot (`<db>.before_dedupe_<timestamp>.bak`) before
deleting anything, unless you add `--no-backup`. After deleting, it re-checks that
no duplicates remain and compacts the file with `VACUUM`.

Once the database is clean, the next `scrape_thread_to_db.py` run can create the
`(thread_id, site_post_id)` unique index that keeps it that way.

---

# Stage 3 — Static site generation

`generate_site.py` builds a static, read-only replica of the forum from
`forum_index.db` + `forum_sections.xlsx`. Output is a folder of plain HTML with
one file per thread, servable from anything — or from no server at all.

## Setup

```bash
pip install jinja2 beautifulsoup4 pandas openpyxl
```

## Expected folder layout

```
xenforo-archiver/
  generate_site.py
  forum_sections.xlsx     <- Section / Sub-Section / Sub-Sub-Section / Description
  templates/
    home_template_dark.html
    section_page_template_dark.html
    search_template_dark.html
    thread_template_dark.html
  forum_index.db          <- your database (place here, or pass --db)
  forum_assets/           <- optional avatar images; basenames must match
                             posts.avatar_local_path. If omitted, avatars fall
                             back to the letter placeholder.
```

## Run

```bash
python3 generate_site.py \
  --db forum_index.db \
  --structure forum_sections.xlsx \
  --templates templates \
  --assets forum_assets \
  --output output
```

All arguments default to the names above, so a bare `python3 generate_site.py`
works if everything sits next to the script. Re-run any time the database or
spreadsheet changes — `output/` is fully regenerated each time and is safe to
delete and rebuild.

## Site structure

**Home page (`index.html`)** — a list of all sections, nothing else. Sections
(the spreadsheet's **Section** column) are plain divider labels, not links,
purely to segregate the page. Under each, every **Sub-Section** is a link with
its total thread count, straight to its own page. No threads and no
Sub-Sub-Sections appear on the home page. It also carries the search box.

**Section pages (`sections/<slug>.html`)** — one page per Sub-Section *and* per
Sub-Sub-Section. Each page:

- Shows the category description (spreadsheet **Description** column) and a link
  back to the original forum URL, if the database has a matching category.
- If it has Sub-Sub-Sections, shows a "Sub-sections" block linking to each.
- If it has its own matching threads — or no children at all — lists its threads,
  **paginated 20 per page**, sorted by most recent activity (pinned first).
- A pure container Sub-Section with no threads of its own skips the redundant
  "no threads" box and shows just its children.
- A Sub-Section with **both** its own threads and children shows both.
- **Pagination** — page 1 is `sections/<slug>.html`; page 2+ is
  `sections/<slug>-page-2.html` and so on. Numbered links with Prev/Next use an
  ellipsis window (`1 … 4 5 6 … 12`) so it stays usable with hundreds of threads.

**Thread pages (`threads/<slug>.<id>.html`)** — rendered with
`thread_template_dark.html`. Filenames reproduce the original site's own slug,
decoded from percent-encoding, e.g.
`need-career-advice-for-engineering-student.210412.html`.

Every Section / Sub-Section / Sub-Sub-Section in the spreadsheet gets a page even
with zero archived threads, so the site reflects the forum's full original
structure, not just what has been scraped so far.

## Search (client-side)

The home page search box covers thread titles, full post content (OP + every
reply), and posters — with a checkbox to widen poster matching from "just the
thread starter" to "anyone who posted in the thread." Searching goes to
`search.html`, which shows matching threads with a highlighted snippet,
paginated 20 per page.

This runs **entirely in the browser** — there is no backend. The search data is
written to `search_index.js` and loaded via a plain `<script src>` tag rather
than `fetch()`-ed as JSON, specifically so it still works when the site is opened
directly from disk (`file://`) rather than over HTTP — browsers commonly block
`fetch()`/XHR of local files, but a `<script src>` load is unaffected.

**Index size** — with full-text indexing on, `search_index.js` scales with total
post content. A small test produced tens of KB; a synthetic ~20 MB database
produced ~14.5 MB; a 200 MB+ database will be substantially larger. The script
prints the exact size at the end of every run (`Search index: ...`). If it turns
out impractically large — slow to load, especially on a first visit before
browser caching — shrink it without any code changes:

```bash
python3 generate_site.py --search-content-limit 2000   # cap indexed text per thread
```

This only shortens how much of each thread is *searchable*; titles, posters, and
the thread pages themselves are unaffected. `0` (default) means unlimited.

## Performance

The script bulk-loads all posts in a single query and groups them in memory
rather than querying per thread. The old per-thread approach could take hours or
never finish on a large database, since an unindexed `posts.thread_id` turns it
into an O(threads × posts) full-scan pattern. Progress is printed every 500
threads.

If your `.db` file or `--output` folder lives inside a cloud-sync folder
(Nextcloud, OneDrive, Dropbox…), the script prints a note — writing thousands of
small files there is slow regardless of query optimisation, because the sync
client watches, hashes and uploads each one. If a run drags, generate to a local
non-synced folder and move `output/` into place afterwards (with sync paused).

## Notes on data handling

- Thread "started by" uses the first post's author — more reliable than
  `threads.poster`, which the listing scraper sometimes fills with a date.
- Leftover `<script>` blocks captured inside post bodies (lightbox i18n JSON) are
  stripped from `content_html` before rendering; everything else — text, images,
  quotes, smilies — is left exactly as scraped.
- **Unicode filenames** — thread filenames are decoded from the source URL's
  percent-encoding, so a Hindi/Arabic/Chinese title becomes readable characters
  rather than `%E0%A4%B9%E0%A4%BF...` literal text (which can be 3× longer and
  blow past Windows' 255-character filename limit). Over-long decoded titles are
  safely truncated; the numeric thread id is always kept in full, so uniqueness
  is never affected.
- The footer's "Forum snapshot as of" date is the most recent
  `threads.last_scraped_at` (falling back to `scraped_at`) in the database at
  build time.

---

# Stage 4 — Serving from the database (Flask on a VPS)

This replaces the static build with a small Flask app that renders every page
**on request** from `forum_index.db`, using the same templates unchanged. Nginx
sits in front, caches everything (a read-only archive is cacheable for as long
as you like), and terminates HTTPS.

## How it fits together

```
visitor ──> nginx (HTTPS, cache, serves avatars directly)
              └──> gunicorn on 127.0.0.1:8000 (2 workers)
                     └──> app.py (Flask)
                            ├── tiny startup model (section hierarchy,
                            │   counts, sidebar — a few MB, never grows)
                            └── forum_index.db (read-only)
                                  ├── every listing / thread / search page
                                  │   answered by an indexed query
                                  └── thread_fts (FTS5) for full search
```

Key design decisions:

- **URLs are identical to the static site** — `/`, `/sections/<slug>.html`,
  `/sections/<slug>-page-N.html`, `/threads/<slug>.<id>.html`, `/search.html`,
  `/full_search.html`. Because the hierarchy matches, every relative link baked
  into the templates (`../threads/...`, `{{ rel }}index.html`) works untouched,
  and old bookmarks to a static build keep working.
- **`generate_site.py` becomes a library, not just a build script.** `app.py`
  imports its helpers (`thread_filename`, `clean_content_html`,
  `load_structure`, pagination, site constants), so the served site is
  guaranteed to look and behave exactly like the static one. Edit a template →
  refresh the page. No builds.
- **The original templates are used as-is.** The only new template is
  `search_server_dark.html`, a server-side version of the search page reusing the
  original CSS verbatim so it looks identical. The old JS search templates and
  `search_index.js` are no longer needed: search is SQLite FTS5 on the server and
  visitors download nothing.
- **`prepare_db.py` runs once per database** (and again whenever you update it).
  It adds two indexes, a `thread_starters` table, a `site_meta` table, and builds
  the `thread_fts` FTS5 index inside the same `.db` file.

## Files to upload

```
/home/<user>/www/archiver/
├── app.py
├── prepare_db.py
├── generate_site.py            (unmodified, imported as a library)
├── forum_index.db
├── forum_sections.xlsx         (read at startup)
├── forum_assets/
└── templates/
    ├── home_template_dark.html
    ├── section_page_template_dark.html
    ├── thread_template_dark.html
    ├── search_server_dark.html
    ├── _shell_css.html
    ├── _topbar.html
    └── _sidebar.html
```

The static-only search templates can stay in the folder harmlessly; nothing
loads them. Note that `home_template_dark.html` and `_sidebar.html` carry small
edits — the hero search form gained the "full thread posts" mode, and Full
Thread Search links now point at `search.html?mode=full`.

## Step 1 — DNS

In your DNS panel, add an **A record**:

```
Type: A    Name: archive    Value: <your VPS's public IP>    TTL: default
```

(Add an AAAA record too if the VPS has IPv6.) Wait a few minutes, then confirm:
`dig +short archive.example.com` should print the VPS IP. Do this first so the
record has propagated by the time you reach the HTTPS step.

## Step 2 — Server preparation (as root, on the VPS)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip rsync nginx
sudo apt install -y certbot python3-certbot-nginx

# App lives in your own home directory — day-to-day changes need no sudo.
mkdir -p /home/<user>/www/archiver

# nginx (user www-data) must be able to TRAVERSE the path to read avatars:
chmod o+x /home/<user> /home/<user>/www
```

Check SQLite supports FTS5 (Debian 11+ does out of the box):

```bash
python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE t USING fts5(x)\"); print('FTS5 OK')"
```

## Step 3 — Upload the files

From your local machine:

```bash
rsync -avz --progress app.py prepare_db.py generate_site.py \
    forum_index.db forum_sections.xlsx \
    <user>@YOUR_VPS_IP:/home/<user>/www/archiver/

rsync -avz --progress templates/     <user>@YOUR_VPS_IP:/home/<user>/www/archiver/templates/
rsync -avz --progress forum_assets/  <user>@YOUR_VPS_IP:/home/<user>/www/archiver/forum_assets/
```

`rsync -z` compresses in transit, and re-uploads of an updated database only
transfer changed blocks.

## Step 4 — Python environment (on the VPS)

```bash
cd /home/<user>/www/archiver
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install flask gunicorn jinja2 pandas openpyxl beautifulsoup4
```

`pandas`/`openpyxl` are needed because `generate_site.py` reads the xlsx;
`beautifulsoup4` is optional but keeps `clean_content_html` on its accurate path.

## Step 5 — Prepare the database (one-time)

```bash
cd /home/<user>/www/archiver
./venv/bin/python prepare_db.py --db forum_index.db
```

This streams once over all posts and builds, inside the `.db` itself: an index on
`posts(thread_id, page_number, post_number)` (making thread pages millisecond
lookups), an index on `threads(section_id)`, the `thread_starters` and
`site_meta` tables (fast startup), and the `thread_fts` FTS5 index. Takes
seconds to a few minutes. Re-run it any time you replace the database.

## Step 6 — Test run

```bash
cd /home/<user>/www/archiver
./venv/bin/gunicorn -w 2 --preload -b 127.0.0.1:8000 app:app
```

From a second SSH session, `curl -I http://127.0.0.1:8000/` should return `200`.
Try a thread URL and `curl "http://127.0.0.1:8000/full_search.html?q=processor"`.
Ctrl-C to stop.

If startup fails with "database has not been prepared", step 5 didn't run against
the same DB file the app points at.

## Step 7 — Run it as a service

Copy `deploy/archiver.service` to `/etc/systemd/system/archiver.service`, then:

```bash
systemctl daemon-reload
systemctl enable --now archiver
systemctl status archiver      # should be active (running)
journalctl -u archiver -f      # live logs if anything looks off
```

The unit uses `--preload` so workers share Python library imports copy-on-write
(saving ~50 MB per extra worker). On a small VPS, 2 workers is plenty for a
read-only archive behind an nginx cache; drop to `-w 1` if RAM is tight, or raise
it if you have headroom.

## Step 8 — Nginx

1. Copy `deploy/archiver-cache.conf` to `/etc/nginx/conf.d/archiver-cache.conf`
   (defines the cache zone — it must live at the `http` level, which `conf.d`
   files do).
2. Copy `deploy/archive.example.com.conf` to
   `/etc/nginx/sites-available/archive.example.com`.
3. Enable and reload:

```bash
mkdir -p /var/cache/nginx/archiver && chown www-data:www-data /var/cache/nginx/archiver
ln -s /etc/nginx/sites-available/archive.example.com /etc/nginx/sites-enabled/
nginx -t && sudo systemctl reload nginx
```

`http://archive.example.com` now serves the archive. The `X-Cache-Status`
response header shows `MISS` on first load of a page and `HIT` after — with a 24h
cache on a read-only archive, the Python app barely works once pages are warm.
Avatars under `/forum_assets/` are served by nginx straight from disk and never
touch Python.

## Step 9 — HTTPS

```bash
certbot --nginx -d archive.example.com
```

Certbot edits the nginx config to add the certificate and the HTTP→HTTPS
redirect, and installs auto-renewal. `https://archive.example.com` is live.

## Updating the archive later

```bash
rsync -avz forum_index.db <user>@YOUR_VPS_IP:/home/<user>/www/archiver/forum_index.db.new
ssh <user>@YOUR_VPS_IP
cd /home/<user>/www/archiver
./venv/bin/python prepare_db.py --db forum_index.db.new
mv forum_index.db.new forum_index.db
sudo systemctl restart archiver            # rebuilds the in-memory model (~seconds)
sudo rm -rf /var/cache/nginx/archiver/*    # drop cached pages from the old snapshot
sudo systemctl reload nginx
```

Changing a **template** is simpler still: edit the file, restart the service,
wipe the cache. No rebuild, ever.

## Moving to another domain later

Nothing in the app knows its hostname — all URLs are relative. Add the A record
for the new domain, duplicate the nginx server file with the new `server_name`
(or add the name to the existing `server_name` line), run
`certbot --nginx -d newdomain.com`, and optionally add a
`return 301 https://newdomain.com$request_uri;` server block on the old name so
old links redirect.

## What differs from the static site

- **Search is better and lighter.** The static build ships a full-text index
  (potentially 100 MB+) to every visitor's browser. Here, one unified
  `search.html` with four modes — thread titles, full post content (highlighted
  snippets, BM25 relevance ranking), started-by, and poster-contributed — is
  answered server-side in milliseconds, and visitors download only the results.
- **A search behaviour nuance:** "poster contributed" and full search use FTS5
  word matching (with prefix matching), which is smarter than the old substring
  scan for normal queries, but matching *inside* the middle of a word no longer
  hits. In practice an upgrade, but worth knowing.
- **The app opens the DB read-only** (`mode=ro`), so it can never corrupt the
  archive. For a small extra speed-up — if you promise the file never changes
  while the service runs — add `&immutable=1` to the connection URI in `app.py`.
  A restart then becomes *required* after swapping the DB, which the update
  procedure above does anyway.
- **RAM footprint stays flat.** The app holds no thread or post data in memory;
  every page is an indexed SQL query at request time. Resident memory (~50–90 MB
  per worker, dominated by Python libraries) doesn't grow with archive size, so
  it runs on a small VPS. Keep `--preload`.
- **404s:** thread/section URLs that never existed return a small themed 404 with
  a link back to the index.

---

# Adapting this to your own XenForo forum

1. Point stage 1 at your forum's root (`--start-url https://forum.site/community/`).
2. Run a small `--max-pages 2` test crawl first and inspect the database before
   committing to a full crawl.
3. If the theme is customised, adjust the selectors in `parse_thread_list` and
   `get_child_forums` in `scrape_forum_threads.py`, and the post selectors in
   `scrape_thread_to_db.py`. `test_parsing.py` is the fastest place to iterate.
4. Rebuild `forum_sections.xlsx` for your forum's hierarchy: columns are
   **Section**, **Sub-Section**, **Sub-Sub-Section**, **Description**. This
   spreadsheet drives the site's navigation, so sections listed here get a page
   even if nothing has been scraped into them yet.
5. Retheme by editing `templates/` — both the static generator and the Flask app
   read the same files.

## Please archive responsibly

Respect the target forum's terms of use and `robots.txt`, keep `--delay` at a
sane value, and prefer resuming an interrupted crawl over restarting it. Most
forums are community-run and volunteer-moderated; the whole pipeline is designed
to be a slow, one-time historical capture rather than a hammering.
