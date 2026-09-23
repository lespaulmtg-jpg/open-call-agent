# open_call_agent/

The pipeline. Every stage reads what the previous one wrote to disk, so any stage can be re-run on its own and a
stopped run always leaves usable files behind.

```
cli.py  ──►  scraper.py ──► festivals.py ──► open_call/batch.py ──► open_call/review.py ──► summary.py
             (ra/)          events >24h      3B model + links        7B model               festival tables
```

| Module | What it owns |
|---|---|
| [cli.py](cli.py) | Every command and option. Sub-commands import their work lazily. |
| [scraper.py](scraper.py) | City, country and continent scrapes; optional raw JSON per festival. |
| [festivals.py](festivals.py) | An event lasting more than 24 hours is a festival. Nothing else defines one. |
| [summary.py](summary.py) | Merging editions into one festival row and choosing its status. |
| [runs.py](runs.py) / [past_runs.py](past_runs.py) | What a run did — recorded live, or read back from old logs. |
| [paths.py](paths.py) | Every path in the data directory. Its docstring is the layout reference. |
| [storage.py](storage.py) | Atomic writes and SIGTERM handling. |
| [urls.py](urls.py), [progress.py](progress.py) | Site keys and host matching; tqdm bars that survive logging. |
| [ra/](ra/), [open_call/](open_call/) | The ra.co API layer and the open call checker — see their own AGENTS.md. |

## Rules that bite if you don't know them

- **The festival's own website leads.** `ra/events.py` reads it from RA's `promotionalLinks` into `Event Website`,
  separate from `Promoter Websites`, because one promoter site can cover several festivals. `summary.choose_result`
  lets it decide the status unless fewer than `MIN_PAGES_FOR_NONE` (3) of its pages could be read — a one-page holding
  site returning "no call found" must not outrank a real call on a promoter site.
- **Stopping must never lose work.** Everything goes through `storage.write_text_atomic` / `write_json_atomic`; the
  CLI installs `stop_on_sigterm()` so `kill` behaves like Ctrl+C; each site is saved as it finishes.
  `test_stopping.py` guards this.
- **Runs record themselves.** The command wraps its work in `runs.run(...)`; the code doing the work calls
  `runs.report(...)` unconditionally (reporting with no active run is a no-op). Counts add up, sets are counted once,
  and `set_total(..., once=True)` lets a country scrape inside a continent scrape leave the outer progress alone.
- **`past_runs.py` parses log wording.** Its `PATTERNS` match lines like `%s/%s: %d events -> %s` in `scraper.py` and
  `[%d/%d] %s: %s` in `open_call/batch.py`. Rewording a `logger.info` silently changes what older runs show in the
  dashboard, and nothing fails. Change the regex in the same commit, and check `tests/test_runs.py`.
- **`cli.py` imports its work inside each `run_*` function** so `--help` doesn't load llama-cpp, playwright or
  huggingface_hub. Keep new heavy imports inside the function.
- **Festival identity is `summary.festival_key`**: country, city and the name with years, case and punctuation
  stripped. It decides which rows merge into one festival and, via `dashboard/sources.row_id`, which festival a note
  belongs to — so changing it orphans the user's saved notes.

## Tests

`test_scraper.py`, `test_festivals.py`, `test_summary.py`, `test_runs.py`, `test_stopping.py`. The scrape fixture is
session-scoped: Switzerland, 2026-09-16 to 2026-09-18.
