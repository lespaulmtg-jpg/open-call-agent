# Design decisions

Main decisions affecting speed, output quality and coverage, by module. Measurements were taken on an M1 Max (64 GB).
Links into `data/` point to local, git-ignored files; measurements marked "not saved" were run ad hoc and only their
numbers are recorded here.

## RA scraper

### Country and continent scraping ([scraper.py](src/open_call_agent/scraper.py#L24), [areas.py](src/open_call_agent/ra/areas.py#L28))

**Issue.** Scrapes are saved per city, but RA areas overlap: Switzerland's "All" area contains every Swiss city, and
large countries (US, UK, DE) nest cities under states or regions. RA has no continent field.

**Solutions tested.** Compared "All" with the five Swiss cities for 2026-09-16 to 18: "All" listed all 27 city events
plus 2 outside any city (not saved). Inspected area trees for US, UK and DE (not saved).

**Decision.** Fetch each country's top-level areas once and file every event under its venue's area, so no event is
fetched or saved twice; events outside listed cities go in `other`. Continents come from a bundled country-code map
([continents.json](src/open_call_agent/ra/continents.json)).

### Listing limit ([client.py](src/open_call_agent/ra/client.py#L140))

**Issue.** RA silently returns at most 10,000 listings per query, so large cities (Berlin 2025–26) lost events. It also
rejects pages larger than 100.

**Solutions tested.** Probed pagination: Berlin returned exactly 10,000 results, with empty pages beyond page 200 (not
saved).

**Decision.** Split the date range in half whenever a query reaches the limit, and request 100 listings per page
(halving requests). Covered by a test that forces splitting on Swiss data.

### Europe-wide scrape ([scrape_europe.py](scripts/scrape_europe.py#L36))

**Issue.** Scraping 46 countries over two years takes about two hours and hits intermittent RA server errors.

**Solutions tested.** Full run: 245,977 events, 8,815 festivals (8,811 from the Europe run plus 4 from Swiss test files
re-processed in the same step), 217 city CSVs, 6 transient HTTP 502s, all recovered by retries
([log](data/scraped_data/logs/scrape_europe_nohup.out)).

**Decision.** Scrape one country-month at a time with a completion marker, retry with backoff
([client.py](src/open_call_agent/ra/client.py#L65)), then merge months per city, de-duplicated by event ID. Interrupted
runs resume.

### Promoter links ([events.py](src/open_call_agent/ra/events.py#L17))

**Issue.** Promoter website fields often hold Facebook or Instagram pages, RA's `instagram` field is empty (links live
in `socialMediaLinks`), some Instagram links have no handle, and names mixed into URL columns made them unusable.

**Solutions tested.** Inspected FOG, Folklor and RUND rows in Swiss results (not saved).

**Decision.** Website columns hold only non-social URLs, Instagram columns only `instagram.com/<handle>` from
`socialMediaLinks`, each URL once, without names.

## Festivals

### Festival definition ([festivals.py](src/open_call_agent/festivals.py#L17))

**Issue.** RA has no festival flag.

**Solutions tested.** Swiss 2026-09-16 to 18 data: FOG Festival (74.5 h), SunIce (60 h) and two other multi-day events
qualified; single club nights did not.

**Decision.** A festival is an event lasting more than 24 hours between its start and end times.

## Crawling

### Finding pages ([checker.py](src/open_call_agent/open_call/checker.py#L391))

**Issue.** Open calls hide in footers, dropdowns or pages two clicks deep; crawling only the homepage misses them.

**Solutions tested.** Starting template: sitemap first, breadth-first crawl as fallback.

**Decision.** Combine robots.txt sitemaps, sitemap indexes and homepage links, then visit pages in priority order:
keyword URLs or link text first, then likely pages (about, contact, get involved, ...), shallow pages next
([priority](src/open_call_agent/open_call/checker.py#L334)). At most 40 pages per site.

### JavaScript sites and time budgets ([checker.py](src/open_call_agent/open_call/checker.py#L261))

**Issue.** Static fetches return empty pages for JavaScript sites, and headless rendering can be very slow.

**Solutions tested.** Benchmark crawl of 20 Zurich festival sites: mean 53 s per site, but naturklang.ch took 502 s
because renders waited for network idle ([benchmark](data/benchmarks/benchmark_20260917_100742.md)). With limits it
took 142 s (not saved).

**Decision.** Fetch statically and re-render with Chromium only when a page looks thin or blocked
([looks_thin](src/open_call_agent/open_call/checker.py#L141)). Renders wait for DOM content (15 s cap), and each site
has a 120 s crawl budget ([crawl](src/open_call_agent/open_call/checker.py#L424)). A site stopped with promising pages
unvisited becomes `needs_review`.

### robots.txt ([checker.py](src/open_call_agent/open_call/checker.py#L310))

**Issue.** fogstudio.ch, a known open call, disallows all crawlers except search engines.

**Solutions tested.** Default run: "robots.txt disallows crawling" and `needs_review`; with `--ignore-robots`: open call
found.

**Decision.** Respect robots.txt by default; `--ignore-robots` overrides. A blocked site is `needs_review`, never
`none`.

### Choosing pages to classify ([checker.py](src/open_call_agent/open_call/checker.py#L573))

**Issue.** Classifying every page is slow, keywords alone miss loosely worded calls, and generic contact words ("email
us", "get in touch") appear on nearly every site. Menus repeat "Open Call" or "Proposals" on every page.

**Solutions tested.** fogstudio.ch `/proposals` ("Send us your proposal") was missed by the first keyword list; menu
labels made every page look like a call.

**Decision.** Shortlist pages with multilingual (EN/DE/FR/IT) call keywords
([STRONG_PATTERNS](src/open_call_agent/open_call/checker.py#L38)) or hosted form links
([FORM_URL_RE](src/open_call_agent/open_call/checker.py#L73)), plus the homepage and up to 5 likely pages, 12 pages at
most. Contact wording is excluded. Lines and links repeated on most pages are removed first
([remove_boilerplate](src/open_call_agent/open_call/checker.py#L534)).

### Old pages ([dates.py](src/open_call_agent/open_call/dates.py#L171), [checker.py](src/open_call_agent/open_call/checker.py#L424))

**Issue.** Archives of past editions waste crawl budget and model time and produce stale calls.

**Solutions tested.** Date sources on real sites: fogstudio.ch has none; hiveclub.ch and kaufleuten.ch have sitemap
`lastmod`; m4music.ch has `modification_date` meta; kaufleuten.ch was published 2017 and modified 2026 (not saved).
Zurich festivals run: 244 pages skipped on 6 sites, freeing budget that surfaced plazmarec.com `/demo-submission`
([log](data/scraped_data/logs/zurich_festivals_two_pass_v3.out)).

**Decision (yours).** Don't process pages last modified or created before 2025-01-01; undated pages are processed. The
most recent known date counts (sitemap `lastmod`, page metadata, `Last-Modified` header), and identical sitemap dates
are ignored. Sitemap-dated pages aren't fetched; metadata-dated pages are still used for links.

### Not re-checking sites ([batch.py](src/open_call_agent/open_call/batch.py#L202), [review.py](src/open_call_agent/open_call/review.py#L281))

**Issue.** A Europe run takes hours, and re-running shouldn't repeat model work.

**Solutions tested.** Basel festivals run twice: the second run loaded neither model
([run 2](data/logs/basel_run2.out)).

**Decision (yours).** Skip any site already checked by the same small model file, or reviewed by the same large model
file, on any day. Sites no model classified (blocked, unreachable) are retried; `--no-reuse` forces a re-check.

## Classification

### Local models ([classifier.py](src/open_call_agent/open_call/classifier.py#L34))

**Issue.** Classification must run locally and without API costs.

**Solutions tested.** Anthropic API classifier (replaced before use); Qwen2.5-3B-Instruct Q4_K_M via llama.cpp on
Metal.

**Decision (yours).** Qwen2.5-3B-Instruct (Q4_K_M, ~2 GB) from the Hugging Face cache for every page.

### False-positive safeguards ([checker.py](src/open_call_agent/open_call/checker.py#L520))

**Issue.** A 3B model over-reads pages and invents quotes.

**Solutions tested.** Hand-made pages (open call, past call, menu label only, job ad, staff credit, newsletter) and the
Zurich benchmark; hiveclub.ch's fan-club application was misread as a call.

**Decision.** Checks in code overrule the model: a call without a quote found verbatim on the page is downgraded;
a past deadline makes a call closed; an open call without keyword or form-link evidence is only `needs_review`
([confidence](src/open_call_agent/open_call/checker.py#L534)).

### Prompt ([classifier.py](src/open_call_agent/open_call/classifier.py#L46))

**Issue.** Several real-page failures: fogstudio.ch rejected for lacking a deadline; an example JSON with
`"status": "none"` was copied by the model; memberships and staff credits read as calls; the 7B model labelled an
information event "upcoming" while its reasoning said nobody could apply, and called "proposals for 2027"
"upcoming".

**Solutions tested.** Neutral JSON placeholders; exclusion rules; reasoning before status; renaming "upcoming" to
"not_yet_open"; question-style fields ("can someone apply today?"). Each checked on fogstudio.ch, m4music.ch and
hiveclub.ch with both models (not saved). Question-style fields fixed some 7B errors but made 3B worse and were
dropped.

**Decision.** Explicit "not an open call" rules ([L50](src/open_call_agent/open_call/classifier.py#L49)), a statement
that short invitations without deadlines count ([L58](src/open_call_agent/open_call/classifier.py#L49)), neutral
placeholders, reasoning first ([L79](src/open_call_agent/open_call/classifier.py#L49)) and the `not_yet_open` label.
"Can you apply today" is decided in code from the linked form (see Review).

### Inference backend and batching ([classifier.py](src/open_call_agent/open_call/classifier.py#L351), [batch.py](src/open_call_agent/open_call/batch.py#L128))

**Issue.** Checking ~6,800 websites one page at a time would take days. The proposal was continuous batching, assuming
prompt processing parallelises well and generation doesn't.

**Solutions tested.** Per-call timing: ~2–2.5 s of each 4.5–5 s call was llama-cpp-python's JSON grammar sampling
(not saved). `llama-batched-bench`: prompt processing stays at ~1,100 tok/s regardless of batch size, while generation
scales (2.6–2.9x at 16 sequences); flash attention was ~3x slower in this build; `ubatch` 512 was fastest; the
system prompt was already cached (not saved). Seven configurations on the same 88 page chunks
([benchmark](data/benchmarks/benchmark_20260917_100742.md)): original in-process with grammar 245 s; in-process
without grammar 153 s; llama-server 1 slot 154–156 s with or without grammar; 4 slots 130 s; 8 slots 132 s. Prompt
tokens (126k) outweighed output tokens (6k) 20 to 1.

**Decision.** One `llama-server` with 4 parallel slots and continuous batching, JSON grammar always on (free there),
flash attention off ([L342](src/open_call_agent/open_call/classifier.py#L378)). 8 crawlers feed it, which roughly
matches its throughput (~6.5 s per site). The in-process backend is kept, without grammar unless output fails to parse.

### One model on the GPU ([classifier.py](src/open_call_agent/open_call/classifier.py#L213))

**Issue.** The 3B and 7B models must never share the GPU.

**Solutions tested.** End-to-end test fails if the large model starts while the small one is loaded.

**Decision (yours).** A process-wide lock refuses to load a second model; each server is stopped and waited for before
the next starts.

## Review

### Two models ([review.py](src/open_call_agent/open_call/review.py#L281))

**Issue.** The 3B model is fast but error-prone; a 7B model is ~2.2x slower (estimated) but more reliable.

**Solutions tested.** Zurich festivals: 3B flagged 3 sites `open` (kaufleuten.ch, streetparade.com, hiveclub.ch), 7B
corrected all three ([log](data/scraped_data/logs/zurich_festivals_two_pass_v3.out)). Basel festivals: 7B confirmed fogstudio.ch and
detectclassic.com ([results](data/open_calls/open_calls_ch_basel_festivals_2026-09-17_reviewed.csv)).

**Decision (yours).** 3B checks every site; afterwards, 7B (Qwen2.5-7B-Instruct Q4_K_M, 2 slots, 16k context)
re-fetches the announcement and open call pages of `open` and `needs_review` sites and decides the final status. Pages
are fetched before the model loads.

### Open versus not yet open ([review.py](src/open_call_agent/open_call/review.py#L68))

**Issue.** The 7B model repeatedly labelled fogstudio.ch's accepting form "not yet open" because the proposals were for
2027.

**Solutions tested.** Clearer definitions, label renaming and question-style prompts (see Prompt); none fixed it
reliably.

**Decision.** When a hosted form is linked, its state decides: accepting responses means `open`, a "no longer accepting
responses" message means `closed` ([FORM_CLOSED_RE](src/open_call_agent/open_call/review.py#L36)). The model still has
to describe a call with verified quotes.

### Stopping and resuming ([storage.py](src/open_call_agent/storage.py), [review.py](src/open_call_agent/open_call/review.py#L281))

**Issue.** Europe runs take hours. Stopping one lost the whole large model review (saved only at the end), a stop
mid-write could leave half-written files that broke the next run, and `kill` or a stop while a model loaded left
`llama-server` holding the GPU.

**Solutions tested.** Killed a real Zurich run: 3 finished sites kept in the CSV and reports, no temp files, and a
re-run skipped them; a stop during model startup first left the server running, then none after the fix. Tests stop
a review halfway with a fake model and check only the unfinished site is sent again
([test_stopping.py](tests/test_stopping.py)).

**Decision (yours).** Save after every site: the small model's row and report, and the large model's review JSON and
reviewed CSV (unfinished sites show "Review pending"). All CSV and JSON writes are atomic, `kill` is handled like
Ctrl+C, queued work is cancelled, and model servers are always shut down. The Europe script writes the summary from
the results saved so far when stopped.

### Dashboard ([server.py](src/dashboard/server.py#L30), [table.js](src/dashboard/static/table.js#L1))

**Issue.** Browsing the results in CSVs is awkward, and a table of 7,886 festivals with every detail is 8.8 MB of JSON.

**Solutions tested.** A web framework (FastAPI and uvicorn would add dependencies for one local page); sending all
details per row (8.8 MB, 0.3 s); drawing all rows at once (thousands of DOM rows are slow to filter and sort).

**Decision.** The standard library's HTTP server, no new dependencies, localhost only. The API sends one row per
festival with only its non-empty fields, gzipped (1 MB), and the page filters and sorts in the browser, drawing 100
rows at a time as you scroll. Jobs run as separate processes whose options come from a declared list, and stopping one
sends SIGTERM, the safe stop. Statuses and notes live in their own JSON file, keyed by festival rather than by event
ID, so they survive rescrapes and new editions.

### Runs in the dashboard ([runs.py](src/open_call_agent/runs.py#L30), [past_runs.py](src/open_call_agent/past_runs.py#L34))

**Issue.** The dashboard could only show jobs it had started itself, in memory, so its list was empty after a restart
and never held the runs started in a terminal, which is where the long ones are run.

**Solutions tested.** Reading figures out of the logs alone (brittle, and nothing to show while a run is going);
keeping runs in memory (lost on restart).

**Decision.** The commands record themselves: `runs.run(...)` writes data/runs/<started>_<kind>.json and the code doing
the work reports into it (`runs.report(events=15, cities={...})`), which does nothing when no run is active. Runs from
before this, and from terminals, are read back from their log files and marked as such, since their figures are only
as good as what was logged.

### Shared rules instead of parallel copies ([classifier.py](src/open_call_agent/open_call/classifier.py#L110), [urls.py](src/open_call_agent/urls.py#L8))

**Issue.** The two model passes had grown their own copies of the same rules: verifying quotes and deadlines (11 lines,
identical bar one word), splitting text into chunks, and picking the most positive chunk. Host matching was written
four times, the site statuses were listed in four places with two different orders, and two modules exported a
`STATUS_RANK` meaning different things.

**Solutions tested.** A review of the whole codebase (duplication, function length, dead code, naming) with the
findings ranked; the tests were green before and after every step, including both model passes end to end.

**Decision.** The rules live once where they are policed: `verify`, `split_text` and `most_positive` next to the prompt
in classifier.py, quote normalisation in text.py (so classifier and crawler can share it without an import cycle), and
`base_domain`, `matches_domain` and `website_key` in urls.py. `STATUSES` is the one site-status order, and the summary
extends it rather than restating it.

## Results

### Statuses and URL columns ([checker.py](src/open_call_agent/open_call/checker.py#L662), [batch.py](src/open_call_agent/open_call/batch.py#L30))

**Issue.** Early verdicts (`open_call`, `possible_call_needs_review`, `inconclusive`, ...) were hard to act on, and a
site that couldn't be checked must not look like "no".

**Solutions tested.** Iterated on Swiss and Zurich results.

**Decision (yours).** Four statuses: `open`, `closed`, `needs_review` (possible call, or couldn't be fully checked),
`none`, with `status_reason`. Separate `announcement_url` and `open_call_url` columns
([open_call_urls](src/open_call_agent/open_call/checker.py#L108)), with menu links excluded.

### Deadlines ([dates.py](src/open_call_agent/open_call/dates.py#L59))

**Issue.** Deadlines arrived as "16.03.26", "June 12, 2026", "mid-November 2026", "31. Oktober 2026".

**Solutions tested.** dateparser alone gave 2126 for "16.03.26" and the 30th for "June 12, 2026"; fixed and covered by
tests with real Swiss formats.

**Decision (yours).** `deadline` is always `YYYY-MM-DD`, day-first for numeric dates; "mid-" means the 15th, month-only
or "end of" the last day; the original wording is kept in `deadline_text`.

### Link and email checks ([links.py](src/open_call_agent/open_call/links.py#L114))

**Issue.** URLs extracted by the small model may be dead.

**Solutions tested.** Real and made-up Google Forms, pages and domains returned 200, 404 and DNS failure as expected;
Instagram returned the same 200 page for real and non-existent profiles; email mailbox probing was rejected as
unreliable and abusive (not saved).

**Decision (yours, scoped).** After the small model, ping every website, announcement, open call and Instagram link and
check email domains for mail servers. Only `broken` (404, 410, DNS) changes a status (`open` becomes `needs_review`);
Instagram is `unverifiable`.

### Europe summary ([summary.py](src/open_call_agent/summary.py#L112), [check_europe_festivals.py](scripts/check_europe_festivals.py#L61))

**Issue.** Results are per website, but the goal is a table of festivals. 5,399 of 8,811 European festivals have no
promoter website; 1,568 unique websites remain.

**Solutions tested.** Summary test on Swiss data (FOG Festival 2026 listed first as `open`).

**Decision.** Check all European festival websites in one batch (one model load per pass), then give each festival the
most positive result among its websites. Festivals without websites are `no_website`, unchecked ones `not_checked`.
Output: per-festival CSV, per-country counts and a Markdown overview of open calls and reviews.

### One row per festival ([summary.py](src/open_call_agent/summary.py#L57))

**Issue.** RA lists every edition and many recurring parties separately, and some events twice: the 8,811 European
festival listings hold 7,886 festivals, 500 of them with several listings (FOG Festival 2025 and 2026; Klubnacht 85
times; 52 duplicate listings with the same promoter, start and venue).

**Solutions tested.** Keys on the name alone (merges unrelated "Klubnacht" nights in Berlin and Freiburg), on the
promoter website (one club website covers dozens of different events), and on country, city and name ignoring years,
case and punctuation. Checked on the Europe data and a unit test.

**Decision.** Merge by country, city and normalised name. The row shows the next edition that hasn't ended (or the
latest), lists all edition dates and event URLs, and joins websites and Instagrams of all editions, so a website on any
edition counts. Numbered editions ("Java 41", "Java 62") stay separate.
