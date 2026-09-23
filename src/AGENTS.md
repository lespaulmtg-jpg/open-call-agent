# src/

Two packages, both put on the path by `pythonpath = ["src"]` in `pyproject.toml` and installed as the commands
`open-call-agent` (`open_call_agent.cli:main`) and `open-call-dashboard` (`dashboard.server:main`).

- **`open_call_agent/`** — the pipeline: scrape ra.co, find festivals, check their websites for open calls with two
  local models, summarise. Knows nothing about the dashboard.
- **`dashboard/`** — a local web UI that reads what the pipeline wrote and starts it as subprocesses. Imports
  `open_call_agent` freely; the reverse never happens. If you find yourself importing `dashboard` from the pipeline,
  the logic belongs in `open_call_agent` instead.

The two only meet through the data directory (see the layout docstring at the top of
[paths.py](open_call_agent/paths.py)) and through the CLI that [jobs.py](dashboard/jobs.py) shells out to. Nothing is
passed in memory, which is why the dashboard can show a run that a terminal started, and why stopping the dashboard
doesn't lose a scrape.

## Working here

- `pytest -m "not llm"` covers everything but the models and takes ~20 s. Run it after every change; several bugs in
  this tree were found only because a test exercised the real CSVs.
- Tests use live RA data for Switzerland, 2026-09-16 to 2026-09-18 (`tests/conftest.py`), so a failure can be the
  network rather than the code.
- `python -m pyflakes src/` catches the imports and names a refactor leaves behind.
- Running a module directly needs `PYTHONPATH=src`; `pytest` does not.

## Conventions in both packages

- Lines up to ~120 characters. `:param:`/`:return:` docstrings on anything non-obvious. Comments explain *why*, and
  several record a decision that cost time to find — keep them when editing nearby.
- A helper has one home: `open_call_agent/urls.py` (host and site keys), `open_call_agent/open_call/text.py` (quote
  normalisation), `open_call_agent/paths.py` (every data directory), `open_call_agent/storage.py` (atomic writes).
  Local copies of these have caused real bugs; don't reintroduce them.
- No new dependencies without a reason. The dashboard is deliberately framework-free and its server is the standard
  library.
