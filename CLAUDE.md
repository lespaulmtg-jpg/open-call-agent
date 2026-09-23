# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A second CLAUDE.md in the parent directory holds general working guidelines (ask before assuming, keep changes
surgical, verify against tests). Both apply.

## Commands

```bash
conda activate open-call-agent          # environment.yml + poetry install + playwright install chromium

pytest -m "not llm"                     # everything except the models, ~20 s
pytest -m llm                           # both models end to end, ~2 min, uses the GPU
pytest tests/test_summary.py::test_festival_editions_appear_once   # one test
python -m pyflakes src/ scripts/ tests/                            # unused imports and names

open-call-agent scrape --country ch --start 2026-09-16 --end 2026-09-18 --festivals
open-call-agent check-open-calls --country ch --city basel --festivals-only --ignore-robots
open-call-dashboard --data-dir /absolute/path/to/data --open        # http://localhost:8000
```

`pyproject.toml` puts `src` on the path, so `pytest` needs no `PYTHONPATH`; running a module directly does
(`PYTHONPATH=src python -m open_call_agent.cli ...`).

Tests use **live RA data for Switzerland, 2026-09-16 to 2026-09-18** (see `tests/conftest.py`). Use the same range for
manual checks. Most tests hit ra.co or real promoter websites, so failures can be the network rather than the code.

Long jobs (`scripts/check_europe_festivals.py`, `scripts/scrape_europe.py`) cost hours and GPU time; don't start one
without asking.

## Pipeline

Each stage reads what the last one wrote, so any stage can be re-run alone:

1. **`scraper.py`** — RA GraphQL by city, country or continent → `data/scraped_data/<country>/<city>/events_*.csv`.
   `--save-raw` also writes RA's whole response per festival to `data/raw_events/`.
2. **`festivals.py`** — events over 24 hours → `festivals_*.csv` beside the events.
3. **`open_call/batch.py`** — crawls each website (`checker.py`) and classifies pages with the 3B model
   (`classifier.py`) → one CSV row and one `data/open_calls/reports/<site>.json` per website. Then `links.py` checks
   every URL and email, opening application links in a browser.
4. **`open_call/review.py`** — the 7B model re-reads sites the 3B called `open` or `needs_review` →
   `*_reviewed.csv` and `data/open_calls/reviews/<site>.json`.
5. **`summary.py`** — joins festivals with results → per-festival and per-country tables in `data/open_calls/summary/`.
6. **`src/dashboard/`** — a standard-library HTTP server plus plain JS reading those files; no framework, no build step.

## Rules that span files

- **The festival's own website leads.** RA keeps it in `promotionalLinks` (`ra/events.py`), stored as `Event Website`
  beside `Promoter Websites`, because one promoter site can cover several festivals. It is crawled first, shown in the
  dashboard, and decides the festival's status unless fewer than three of its pages could be read (`summary.py`).
- **A positive needs evidence.** `classifier.verify` downgrades a status whose quote isn't on the page verbatim, and
  closes a call whose deadline has passed. Both model passes call it — never re-implement it in one pass.
- **The application form outranks the page.** Organisers leave "applications are open" up for months, so `links.py`
  opens application links in headless Chromium and marks them `redirected (...)` or `form closed`; a call whose forms
  are all closed becomes `closed`.
- **One model on the GPU at a time**, enforced by a lock in `classifier.py`. The 7B review only starts once the 3B
  server has stopped.
- **Stopping must never lose work.** Every CSV and JSON goes through `storage.py`'s atomic writes; each site is saved
  as it finishes; SIGTERM is handled like Ctrl+C. Re-running skips sites a report or review already covers *for the
  same model file*, so changing models re-checks everything.
- **Runs record themselves.** Commands wrap their work in `runs.run(...)` and the code doing the work calls
  `runs.report(...)`, writing `data/runs/*.json` that the dashboard reads. `past_runs.py` reconstructs older runs by
  parsing log lines, so rewording a `logger.info` in the scrapers silently changes what those runs show.

## Conventions

- Lines up to ~120 characters; `:param:`/`:return:` docstrings on anything non-obvious; comments explain *why*, and
  several record a decision that cost time to find — keep them when editing nearby.
- Shared helpers have one home: `urls.py` (host and site keys), `open_call/text.py` (quote normalisation),
  `paths.py` (every data directory), `storage.py` (atomic writes). Don't reintroduce local copies.
- No new dependencies without a reason; the dashboard is deliberately framework-free.
- `DECISIONS.md` records why the non-obvious choices were made, with links into the code; add a section when you make
  another one. `README.md` is the user-facing summary.

# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
