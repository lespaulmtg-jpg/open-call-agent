"""
The runs shown in the scraper and Open Call LLM views: runs recorded by the scrapers themselves (data/runs/*.json),
plus older runs rebuilt from their log files, newest first.

A run started from this dashboard can also be stopped, which is why its job is matched to it by process id. A run
started from a terminal can be stopped too, by signalling its process (see jobs.stop_process), and any run the
dashboard knows how to build a command for can be started again.
"""
import re

from open_call_agent.past_runs import past_runs
from open_call_agent.paths import reports_dir, runs_dir, safe_filename
from open_call_agent.storage import read_json
from open_call_agent.urls import website_key

from .jobs import is_our_process, job_from_run, stop_process

# What each figure is called in a run's window, in the order they are shown
STAT_LABELS = {
    "events": "Events found", "festivals": "Festivals found", "countries": "Countries", "cities": "Cities",
    "websites": "Websites", "checked": "Checked now", "reused": "Already checked", "open": "Open calls",
    "needs_review": "Needs review", "closed": "Closed", "none": "No call", "reviewed": "Reviewed by the 7B model",
    "reviewed_open": "Open after review", "reviewed_needs_review": "Needs review after review",
    "reviewed_closed": "Closed after review", "reviewed_none": "No call after review",
}

# An error as a run recorded it, e.g. "2026-09-22 08:58:41,560 https://www..klubpan.com failed", or, for a run rebuilt
# from a log file, "open_call_agent.open_call.batch: https://example.org failed: timeout"
ERROR_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)?\s+")
# A logger name is a dotted module path. Requiring the dot keeps "ValueError: no data" a message and not a source.
ERROR_SOURCE_RE = re.compile(r"^(\w+(?:\.\w+)+): ")
EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Interrupt))\b")
URL_IN_ERROR_RE = re.compile(r"https?://[^\s,]+")


def parse_error(run_id, index, line):
    """
    Split a recorded error into the parts the run window shows, so an error can be pointed at and looked up rather
    than read as one long line.

    :param run_id: The run's id, which the error's own id is built from.
    :param index: The error's position in the run, counting from 0.
    :param line: The error as the run recorded it.
    :return: An error dict with id, label, time, source, type, site and message.
    """
    message, time, source = line, "", ""
    stamp = ERROR_TIME_RE.match(message)
    if stamp:
        time, message = stamp.group(1), message[stamp.end():]
    logger_name = ERROR_SOURCE_RE.match(message)
    if logger_name:
        source, message = logger_name.group(1), message[logger_name.end():]

    site = URL_IN_ERROR_RE.search(message)
    exception = EXCEPTION_RE.search(message)
    return {"id": f"{run_id}#e{index + 1}", "label": f"e{index + 1}", "time": time, "source": source,
            "type": exception.group(1) if exception else ("site failed" if site else "error"),
            "site": site.group(0) if site else "", "message": message.strip(), "line": line}


def saved_runs(data_dir):
    """
    :param data_dir: The data directory.
    :return: The runs the scrapers recorded, newest first.
    """
    runs = [read_json(path) for path in sorted(runs_dir(data_dir).glob("*.json"), reverse=True)]
    return [run for run in runs if run]


def run_state(run, job):
    """
    What state to show a run in. A run only records how it ended if it got the chance: one killed outright, e.g. by the
    machine going down, leaves a record saying "running" for ever, so a run whose process is gone is shown as stopped.

    :param run: A run dict.
    :param job: This dashboard's job for the run, or None.
    :return: A tuple of (state, note explaining a state that differs from the record).
    """
    if run["state"] != "running" or (job and job.running) or is_our_process(run.get("pid")):
        return run["state"], ""
    return "stopped", "The process is gone and never recorded how it ended, so this run was stopped by something " \
                      "outside it, e.g. the machine going down. Everything it had finished is saved."


def with_stats(run, jobs_by_pid):
    """
    Add what the dashboard shows on top of a run: its progress as a percentage, its figures with readable names, its
    errors split into parts, the job id when this dashboard started it, and whether it can be stopped or run again.

    :param run: A run dict.
    :param jobs_by_pid: A dict of process id -> Job for every job this dashboard started, finished ones included, so a
        run that has ended still shows the output its job kept.
    :return: The run, ready for the page.
    """
    job = jobs_by_pid.get(run.get("pid"))
    state, state_note = run_state(run, job)
    progress = run.get("progress") or {}
    total, done = progress.get("total") or 0, progress.get("done") or 0
    percent = round(done * 100 / total) if total else (100 if state != "running" else 0)
    stats = [{"label": label, "value": run["counts"][name]} for name, label in STAT_LABELS.items()
             if run.get("counts", {}).get(name)]
    kind, _ = job_from_run(run)
    return run | {"state": state, "state_note": state_note, "percent": percent, "stats": stats,
                  "job_id": job.id if job else None,
                  "errors": [parse_error(run["id"], index, line) for index, line in enumerate(run.get("errors") or [])],
                  "can_stop": state == "running", "can_restart": bool(kind)}


def list_runs(data_dir, runner, kind=None):
    """
    :param data_dir: The data directory.
    :param runner: The JobRunner, for matching runs this dashboard started.
    :param kind: Optional "scrape" or "open_calls".
    :return: Runs newest first, recorded ones before rebuilt ones of the same moment.
    """
    jobs_by_pid = {job.process.pid: job for job in runner.jobs.values() if job.process}
    runs = saved_runs(data_dir) + past_runs(data_dir)
    runs = [with_stats(run, jobs_by_pid) for run in runs if not kind or run["kind"] == kind]
    # Running first, then newest
    return sorted(runs, key=lambda run: (run["state"] == "running", run["started"]), reverse=True)


def is_running(data_dir, runner, kind=None):
    """
    Whether a run is still going, without reading the log files of every past run.

    :param data_dir: The data directory.
    :param runner: The JobRunner.
    :param kind: Optional "scrape" or "open_calls".
    :return: True if a recorded run of that kind is still running.
    """
    jobs_by_pid = {job.process.pid: job for job in runner.jobs.values() if job.process}
    return any(run_state(run, jobs_by_pid.get(run.get("pid")))[0] == "running"
               for run in saved_runs(data_dir) if not kind or run["kind"] == kind)


def error_report(data_dir, error):
    """
    What the saved report of the site an error is about says, so the run window can show why that site failed without
    the reader having to find the file.

    :param data_dir: The data directory.
    :param error: A parsed error dict.
    :return: A dict with the site's status and recorded errors, or None when the error names no site with a report.
    """
    if not error["site"]:
        return None
    path = reports_dir(data_dir) / f"{safe_filename(website_key(error['site']))}.json"
    report = read_json(path)
    if not report:
        return None
    return {"path": str(path), "status": report.get("status", ""), "status_reason": report.get("status_reason", ""),
            "errors": [f"{item['stage']}: {item['url']}: {item['error']}" for item in report.get("errors") or []]}


def find_run(data_dir, runner, run_id):
    """
    :return: One run by id, with each of its errors looked up in the reports.
    :raises FileNotFoundError: If there is no such run.
    """
    run = next((run for run in list_runs(data_dir, runner) if run["id"] == run_id), None)
    if not run:
        raise FileNotFoundError(f"Unknown run {run_id}")
    return run | {"errors": [error | {"report": error_report(data_dir, error)} for error in run["errors"]]}


def stop_run(data_dir, runner, run_id):
    """
    Stop a running run, whether this dashboard started it or not.

    :return: The run.
    :raises ValueError: If it isn't running any more.
    """
    run = find_run(data_dir, runner, run_id)
    if run["job_id"]:
        runner.stop(run["job_id"])
    else:
        stop_process(run.get("pid"))
    return run


def restart_run(data_dir, runner, run_id):
    """
    Start a recorded run again with the options it was started with. Sites already checked are reused, so a stopped run
    carries on rather than starting over.

    :return: The new Job.
    :raises ValueError: If the run wasn't started by a command this dashboard can run.
    """
    kind, options = job_from_run(find_run(data_dir, runner, run_id))
    if not kind:
        raise ValueError("This run can't be started from the dashboard: it was not one of the commands in its forms")
    return runner.start(kind, options)
