# open_call_agent

Scrapes events from [ra.co](https://ra.co), identifies festivals, and checks promoter websites for open calls (music,
DJ, visuals, workshops, ...) with local LLMs. Design decisions and benchmarks: [DECISIONS.md](DECISIONS.md).

## Setup

```bash
conda env create -f environment.yml
conda activate open-call-agent
poetry install
playwright install chromium
```

Models (Qwen2.5-3B and 7B Instruct, Q4_K_M GGUF) download to the Hugging Face cache on first use.

## Usage

```bash
# Scrape events by city, country or continent; --festivals also identifies festivals
open-call-agent scrape --country ch --start 2026-09-16 --end 2026-09-18 --festivals
open-call-agent scrape --city ch/zurich --start 2026-09-16 --end 2026-09-18
open-call-agent festivals --country ch                 # events lasting more than 24 hours

# Check promoter websites in existing CSVs: small model on every site, then the large model reviews
open-call-agent check-open-calls --country ch --festivals-only
open-call-agent review-open-calls --input data/open_calls/open_calls_ch_festivals_<date>.csv   # review only
open-call-agent check-site -u https://fogstudio.ch --ignore-robots                            # one website

# Whole continents
python scripts/scrape_europe.py                        # all of Europe, 2025-01-01 to 2026-12-31, resumable
python scripts/check_europe_festivals.py --limit 20    # check European festivals and write summary tables
python scripts/check_europe_festivals.py --summary-only
```

Useful options: `--ignore-robots`, `--no-review`, `--no-reuse` (check sites again), `--min-page-date` (default
`2025-01-01`, `none` to disable), `--crawl-workers`, `--llm-slots`, `--review-slots`, `--data-dir`. Country and city
codes are those in ra.co URLs, e.g. `ra.co/events/ch/zurich`.

Runs can be stopped at any point with Ctrl+C or `kill <PID>`: every site finished by either model is already saved to
the CSVs and per-site JSON files, and the model server is shut down. Run the same command again to continue; sites
already checked or reviewed by the same model are skipped. Don't run two model jobs at once: only one model should use
the GPU.

## Dashboard

```bash
open-call-dashboard --open          # http://localhost:8000
```

A local page with three views: **Festivals**, a table of the reviewed festivals whose columns (festival, month, status,
submissions URL, the festival's website, the RA listing it came from, country, city and deadline) can be dragged,
sorted and filtered by text, country, city, status, your status and deadline, where a row expands into everything the scrapers found and into your own status (applied, not a
festival, ...) and notes; **RA scraper** and **Open call LLM**, which start and stop the same commands as the CLI and
show their log. The data source of the table is any results or summary CSV in `data/open_calls/`. Your statuses and
notes are stored in `data/dashboard/notes.json` and stay with a festival across new scrapes. The server is for
localhost only: it has no authentication and starts processes, so don't expose it to a network.

## Outputs

```
data/scraped_data/<country>/<city>/events_<start>_<end>.csv      scraped events
data/scraped_data/<country>/<city>/festivals_<start>_<end>.csv   events lasting more than 24 hours
data/open_calls/open_calls_<scope>_<date>.csv                    small model results, one row per website
data/open_calls/open_calls_<scope>_<date>_reviewed.csv           final results after the large model review
data/open_calls/reports/, reviews/                               per-site JSON (pages, quotes, coverage, errors)
data/open_calls/summary/                                         per-festival, per-country and Markdown summaries
data/dashboard/notes.json                                        your statuses and notes from the dashboard
data/logs/                                                        logs
```

| Column | Content |
| --- | --- |
| `status` | `open`, `closed`, `needs_review` (possible call, or site couldn't be fully checked) or `none` |
| `status_reason` | Why |
| `deadline`, `deadline_text` | `YYYY-MM-DD` (several separated by ` \| `), and the wording on the page |
| `announcement_url`, `open_call_url` | Page announcing the call, and the form or application link on it |
| `opportunity_types`, `how_to_apply`, `contact_emails`, `promoter_instagrams` | Details and contacts |
| `*_check` | Link checks: `ok`, `broken (404/DNS)`, `blocked (403)`, `timeout`, `unverifiable` (Instagram), ... |
| `issues` | Warnings and errors |
| `small_model_status`, `large_model_status`, `review_reason`, `review_quotes` | Reviewed CSV only |

The festival summary has one row per festival: editions and duplicate listings with the same country, city and name
(ignoring years) are merged, showing the next edition and listing all edition dates. Festivals without a promoter website are `no_website` and websites not yet checked are
`not_checked`.

## Tests

Tests use live RA data for Switzerland between 2026-09-16 and 2026-09-18.

```bash
pytest -m "not llm"   # scraper, festivals, dates, link checks (~20 s)
pytest -m llm         # both models end to end (~1 min, uses the GPU)
```
