"""
Runs: what the scrapers record about themselves, what can be read back from the logs of older runs, and what the
dashboard shows for both. No network and no model.
"""
import logging

import pytest

from dashboard.jobs import JobRunner
from dashboard.runs import list_runs
from open_call_agent import runs
from open_call_agent.past_runs import run_from_log
from open_call_agent.paths import logs_dir, runs_dir
from open_call_agent.storage import read_json

SCRAPE_LOG = """\
2026-09-17 02:08:54,766 INFO scrape_europe: Scraping 46 countries in europe x 24 months (2025-01-01 to 2026-12-31).
2026-09-17 02:08:57,625 INFO open_call_agent.scraper: al/other: 1 events -> data/.chunks/2025-01/al/other/events.csv
2026-09-17 02:20:01,000 INFO scrape_europe: [1104/1104] United Kingdom 2026-12: 272 events in 20 cities
2026-09-17 04:11:42,730 INFO open_call_agent.festivals: other: 5 festivals -> data/xk/other/festivals.csv
2026-09-17 04:11:43,003 INFO scrape_europe: Done: 245977 events and 8815 festivals in 217 city CSVs
"""

CHECK_LOG = """\
2026-09-17 15:05:06,342 INFO check_europe_festivals: 217 festival CSVs in 44 countries.
2026-09-17 15:05:06,863 INFO open_call_agent.open_call.batch: 20 websites to check from 217 CSV files
2026-09-17 15:06:02,056 INFO open_call_agent.open_call.batch: [1/19] https://fogstudio.ch: open (crawl 6s, classify 2s)
2026-09-17 15:07:24,579 ERROR open_call_agent.open_call.batch: https://example.org failed: timeout
2026-09-17 15:07:25,579 INFO open_call_agent.open_call.batch: [2/19] https://enorm.club/: none (crawl 65s, classify 1s)
2026-09-17 15:07:30,000 WARNING check_europe_festivals: Stopped. Writing the summary from results saved so far
"""


def test_a_run_records_what_it_did_and_saves_it(tmp_path):
    with runs.run("scrape", tmp_path, "ch, 2026-09-16 to 2026-09-18", {"country": "ch"}) as run:
        runs.set_total(2, "area")
        runs.report(progress=1, events=15, cities={"zurich"}, countries={"ch"})
        runs.report(progress=2, events=15, cities={"basel", "zurich"}, countries={"ch"})
        assert read_json(run.path)["state"] == "running", "a running job is already on disk"

    saved = read_json(run.path)
    assert saved["state"] == "done" and saved["label"] == "ch, 2026-09-16 to 2026-09-18"
    assert saved["counts"] == {"events": 30, "cities": 2, "countries": 1}, "counts add up, sets are counted once"
    assert saved["progress"] == {"done": 2, "total": 2, "unit": "area"}
    assert saved["arguments"] == {"country": "ch"} and saved["pid"] and saved["errors"] == []


def test_a_stopped_or_failed_run_says_so(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with runs.run("scrape", tmp_path, "stopped", {}):
            raise KeyboardInterrupt
    with pytest.raises(ValueError):
        with runs.run("open_calls", tmp_path, "failed", {}):
            raise ValueError("no data")

    states = {read_json(path)["label"]: read_json(path) for path in runs_dir(tmp_path).glob("*.json")}
    assert states["stopped"]["state"] == "stopped"
    assert states["failed"]["state"] == "failed" and "no data" in states["failed"]["errors"][0]


def test_logged_errors_are_kept_on_the_run(tmp_path):
    with runs.run("scrape", tmp_path, "with errors", {}) as run:
        logging.getLogger("open_call_agent.scraper").error("Albania (al) failed: timeout")
        logging.getLogger("open_call_agent.scraper").info("ch/zurich: 15 events")
    assert read_json(run.path)["errors"] == ["Albania (al) failed: timeout"], "errors only, not every line"


def test_nothing_breaks_when_no_run_is_recorded():
    runs.report(events=5, progress=1)
    assert runs.set_total(10, "site") is False


def test_a_country_scrape_inside_a_continent_scrape_keeps_the_outer_count(tmp_path):
    with runs.run("scrape", tmp_path, "europe", {}) as run:
        assert runs.set_total(46, "country") is True
        assert runs.set_total(3, "area", once=True) is False, "the continent owns the count"
        runs.report(progress=5)
    assert read_json(run.path)["progress"] == {"done": 5, "total": 46, "unit": "country"}


def test_older_runs_are_read_back_from_their_logs(tmp_path):
    scrape = logs_dir(tmp_path) / "scrape_europe_20260917_020854.log"
    check = logs_dir(tmp_path) / "check_europe_festivals_20260917_150506.log"
    scrape.parent.mkdir(parents=True, exist_ok=True)
    scrape.write_text(SCRAPE_LOG)
    check.write_text(CHECK_LOG)

    scraped = run_from_log(scrape)
    assert scraped["kind"] == "scrape" and scraped["state"] == "done"
    assert scraped["seconds"] == 7369, "from its first line to its last"
    assert scraped["counts"] == {"countries": 46, "events": 245977, "festivals": 8815, "cities": 217}, \
        "the merged totals, not the monthly chunks counted again"
    assert scraped["progress"] == {"done": 1104, "total": 1104, "unit": "country-month"}

    checked = run_from_log(check)
    assert checked["kind"] == "open_calls" and checked["state"] == "stopped", "its log ends in a stop"
    assert checked["counts"] == {"websites": 20, "checked": 2, "open": 1, "none": 1}
    assert checked["progress"] == {"done": 2, "total": 19, "unit": "site"}
    assert checked["errors"] == ["open_call_agent.open_call.batch: https://example.org failed: timeout"]


def test_the_dashboard_lists_recorded_and_rebuilt_runs(tmp_path):
    with runs.run("scrape", tmp_path, "ch, today", {}):
        runs.report(events=30)
    (logs_dir(tmp_path)).mkdir(parents=True, exist_ok=True)
    (logs_dir(tmp_path) / "check_europe_festivals_20260917_150506.log").write_text(CHECK_LOG)

    listed = list_runs(tmp_path, JobRunner(tmp_path))
    assert [run["kind"] for run in listed] == ["scrape", "open_calls"], "newest first"
    assert [run["source"] for run in listed] == ["run", "log"]

    scrape = listed[0]
    assert scrape["percent"] == 100 and scrape["job_id"] is None
    assert scrape["stats"] == [{"label": "Events found", "value": 30}], "figures get names for the window"
    assert [run["kind"] for run in list_runs(tmp_path, JobRunner(tmp_path), kind="open_calls")] == ["open_calls"]
