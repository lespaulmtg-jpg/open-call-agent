"""
Runs: what the scrapers record about themselves, what can be read back from the logs of older runs, and what the
dashboard shows for both. No network and no model.
"""
import logging

import pytest

from dashboard import runs as dashboard_runs
from dashboard.jobs import JobRunner, build_command, job_from_run
from dashboard.runs import find_run, is_running, list_runs, parse_error
from open_call_agent import runs
from open_call_agent.past_runs import run_from_log
from open_call_agent.paths import logs_dir, reports_dir, runs_dir
from open_call_agent.storage import read_json, write_json_atomic

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


def test_a_run_can_be_started_again_with_the_options_it_was_given():
    """
    A stopped run is carried on by running the same command again, so every option has to survive the round trip from
    the record back to a command line.
    """
    def command_for(arguments):
        kind, options = job_from_run({"arguments": arguments})
        return " ".join(build_command(kind, options, "data")).split("--data-dir data ")[1]

    assert command_for({"command": "check-open-calls", "festivals_only": "True", "no_review": "True",
                        "min_page_date": "2025-01-01", "crawl_workers": "8"}) == \
        "check-open-calls --festivals-only --no-review --min-page-date 2025-01-01 --crawl-workers 8 " \
        "--llm-slots 4 --review-slots 2", "a check of everything scraped names no area, and keeps the form's defaults"
    assert command_for({"command": "scrape", "city": "ch/zurich", "start": "2026-09-16", "end": "2026-09-18",
                        "festivals": "True"}) == "scrape --city ch/zurich --start 2026-09-16 --end 2026-09-18 --festivals"
    assert command_for({"command": "scrape", "continent": "europe", "start": "2026-01-01", "end": "2026-12-31"}) == \
        "scrape --continent europe --start 2026-01-01 --end 2026-12-31", "no --festivals: it wasn't asked for"
    assert command_for({"command": "check-open-calls", "country": "ch", "city": "basel"}).startswith(
        "check-open-calls --country ch --city basel"), "a city carries the country it is in"
    assert command_for({"command": "check-open-calls", "country": "ch"}).startswith("check-open-calls --country ch"), \
        "a country on its own is not read as a city"

    assert job_from_run({"arguments": {}}) == (None, None), "a run rebuilt from a log has nothing to start again"
    assert job_from_run({"arguments": {"command": "review-open-calls"}}) == (None, None), "no form for this command"


def test_an_error_is_split_into_parts_that_can_be_pointed_at():
    error = parse_error("20260921_221356_open_calls", 0, "2026-09-22 08:58:41,560 https://www..klubpan.com failed")
    assert error["id"] == "20260921_221356_open_calls#e1" and error["label"] == "e1"
    assert error["time"] == "2026-09-22 08:58:41" and error["type"] == "site failed"
    assert error["site"] == "https://www..klubpan.com" and error["message"] == "https://www..klubpan.com failed"

    logged = parse_error("log_check", 1, "open_call_agent.open_call.batch: https://example.org failed: timeout")
    assert logged["source"] == "open_call_agent.open_call.batch" and logged["site"] == "https://example.org"
    assert logged["label"] == "e2"

    crashed = parse_error("run", 0, "ValueError: no festival CSVs")
    assert crashed["type"] == "ValueError" and not crashed["site"]


def test_a_run_whose_process_is_gone_is_shown_as_stopped(tmp_path, monkeypatch):
    """
    A run only records how it ended if it gets the chance. One killed outright, e.g. by the machine going down, would
    otherwise sit in the dashboard saying "running" for ever.
    """
    with runs.run("open_calls", tmp_path, "killed", {"command": "check-open-calls"}) as run:
        runs.report(progress=1, total=2, unit="site")
        run.save(force=True)
        monkeypatch.setattr(dashboard_runs, "is_our_process", lambda pid: True)
        listed = list_runs(tmp_path, JobRunner(tmp_path))[0]
        assert listed["state"] == "running" and listed["percent"] == 50 and not listed["state_note"]
        assert listed["can_stop"] and listed["can_restart"]

        monkeypatch.setattr(dashboard_runs, "is_our_process", lambda pid: False)
        gone = list_runs(tmp_path, JobRunner(tmp_path))[0]
        assert gone["state"] == "stopped" and "machine going down" in gone["state_note"]
        assert not gone["can_stop"] and gone["can_restart"], "it can still be started again"
        assert is_running(tmp_path, JobRunner(tmp_path), "open_calls") is False


def test_an_error_shows_what_the_sites_own_report_recorded(tmp_path):
    write_json_atomic(reports_dir(tmp_path) / "example.org.json",
                      {"status": "needs_review", "status_reason": "Site check crashed: LocationParseError",
                       "errors": [{"stage": "crash", "url": "https://example.org", "error": "LocationParseError"}]})
    with runs.run("open_calls", tmp_path, "with a failed site", {}) as run:
        logging.getLogger("open_call_agent.open_call.batch").error("https://example.org failed")

    [error] = find_run(tmp_path, JobRunner(tmp_path), run.id)["errors"]
    assert error["site"] == "https://example.org" and error["report"]["status"] == "needs_review"
    assert error["report"]["errors"] == ["crash: https://example.org: LocationParseError"]
    assert "example.org.json" in error["report"]["path"]
