# dashboard/

A local web UI: browse the festivals that have been checked, and run the scrapers. Start it with
`open-call-dashboard --data-dir <path> --open` and it serves http://localhost:8000.

It reads the pipeline's files and shells out to its CLI. It never imports work in-process, so a job keeps running when
the browser is closed, and a run started in a terminal shows up here too.

| Module | Role |
|---|---|
| [server.py](server.py) | `ThreadingHTTPServer`, the static files and the whole JSON API. |
| [sources.py](sources.py) | Which CSVs the table can be built from, and the row the page receives. |
| [stats.py](stats.py) | The four figures at the top of each view. |
| [datasets.py](datasets.py) | What is on disk and which pipeline steps have been run over it. |
| [runs.py](runs.py) | Recorded and rebuilt runs, with progress and readable figure names. |
| [jobs.py](jobs.py) | The forms' options, the command they build, and the subprocess. |
| [notes.py](notes.py) | The user's own status and note per festival. |
| [static/](static/) | The page itself — see its AGENTS.md. |

## The API

`GET /api/config` (sources, statuses, job types, resolved data dir) · `/api/festivals?source=` · `/api/datasets` ·
`/api/checking` · `/api/stats/scrape` · `/api/stats/llm` · `/api/runs?kind=` · `/api/runs/detail?id=` · `/api/jobs` ·
`/api/jobs/log?id=&offset=`. `POST /api/notes` · `/api/jobs/start` · `/api/jobs/stop` · `/api/runs/stop` ·
`/api/runs/restart`. `respond()` maps `FileNotFoundError` to 404 and `ValueError` to 400, so handlers just raise.

`/api/runs/detail` differs from the listing: it looks each error's site up in `reports/` and attaches what that site's
own report recorded. Keep the listing free of file reads — the page asks for it every two seconds.

Responses over 10 KB are gzipped — the festivals table is a few megabytes of text. Static files are served with
`Cache-Control: no-cache`: without it a browser served stale `api.js` and a feature looked missing when it wasn't.

**No authentication, and none is planned.** It binds 127.0.0.1 and must not be exposed.

## Things that took a while to get right

- **A job may only run what `JOB_TYPES` declares.** `build_command` turns declared fields into arguments and rejects
  everything else; the browser never supplies a command. A choice's free-text value is named `<field>_value` (or the
  `with` part's own name) so it can't collide with the choice itself — sending `--city city` was a real bug.
- **Jobs run in their own process group** (`start_new_session=True`), so stopping one signals the model server too.
  Stop is SIGTERM, which the CLI handles like Ctrl+C: everything finished is kept. `PYTHONPATH` is set explicitly
  because the dashboard may be started from a different environment than the one the CLI needs.
- **`Job.running` means `returncode is None`**, true from the moment the job is registered. Anything laxer let a second
  GPU job slip in during `Popen`. Jobs are matched to runs by process id whether or not they are still running, so a
  finished run still shows the output its job kept.
- **A run whose process is gone is shown as stopped** (`run_state`), because a run only records how it ended if it gets
  the chance; one killed outright would otherwise say "running" for ever. `is_our_process` checks the command as well
  as the id, since a process id can be handed to something else later — and that is also what makes it safe to signal.
- **Starting a run again goes back through the forms' options** (`job_from_run` → `run_options` → `build_command`), so
  nothing runs that a form couldn't have started. `field_dest` maps a flag to the name the CLI records it under, and
  the most specific choice is matched first, or `--country ch --city basel` would come back as a country run.
- **Progress bars in the log** redraw with `\r` and ANSI codes; `clean_log_line` keeps only the last state.
- **`default_source` is the most recently written source**, not the first found, or a fresh check is hidden behind an
  older summary.
- **`working_link`** picks the first URL whose link check isn't `broken` or `redirected`, so the table doesn't send the
  user to a dead form when a working announcement page exists.
- **A dataset's steps are measured, not assumed** (`datasets.py`): the link checks only run when a check finishes
  and the review only covers what the small model flagged, so a stopped run leaves results that look complete until
  you count the rows that carry a `*_check` column or a `large_model_status`. That is what the Datasets view shows.
- **`festival_websites` in stats.py uses `row_websites`**, the checker's own rule. Counting only `Promoter Websites`
  made "Websites Left To Check" disagree with what a run reported as its total.
- **`load_rows` caches on the mtimes** of the source CSV and every festivals CSV, since building the table from raw
  results joins thousands of rows. "Reload" in the page just re-requests; the mtime check is what makes it cheap.
- **Note ids come from `summary.festival_key`** via `row_id`. Change that key and every saved note loses its festival.
- A run is matched to the job that started it **by process id**, which is how the window knows it can offer "Stop".

## Tests

`test_dashboard.py` (table, notes, the command each form builds, every endpoint), `test_datasets.py` and
`test_runs.py`. No model, no
network beyond the Swiss fixture. Changes to the page itself were verified with Playwright by hand — there is no
browser test suite.
