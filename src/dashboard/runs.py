"""
The runs shown in the scraper and Open Call LLM views: runs recorded by the scrapers themselves (data/runs/*.json),
plus older runs rebuilt from their log files, newest first.

A run started from this dashboard can also be stopped, which is why its job is matched to it by process id.
"""
from open_call_agent.past_runs import past_runs
from open_call_agent.paths import runs_dir
from open_call_agent.storage import read_json

# What each figure is called in a run's window, in the order they are shown
STAT_LABELS = {
    "events": "Events found", "festivals": "Festivals found", "countries": "Countries", "cities": "Cities",
    "websites": "Websites", "checked": "Checked now", "reused": "Already checked", "open": "Open calls",
    "needs_review": "Needs review", "closed": "Closed", "none": "No call", "reviewed": "Reviewed by the 7B model",
    "reviewed_open": "Open after review", "reviewed_needs_review": "Needs review after review",
    "reviewed_closed": "Closed after review", "reviewed_none": "No call after review",
}


def saved_runs(data_dir):
    """
    :param data_dir: The data directory.
    :return: The runs the scrapers recorded, newest first.
    """
    runs = [read_json(path) for path in sorted(runs_dir(data_dir).glob("*.json"), reverse=True)]
    return [run for run in runs if run]


def with_stats(run, running_jobs):
    """
    Add what the dashboard shows on top of a run: its progress as a percentage, its figures with readable names, and
    the job id when this dashboard started it, so it can be stopped.

    :param run: A run dict.
    :param running_jobs: A dict of process id -> job id for jobs this dashboard started.
    :return: The run, ready for the page.
    """
    progress = run.get("progress") or {}
    total, done = progress.get("total") or 0, progress.get("done") or 0
    percent = round(done * 100 / total) if total else (100 if run["state"] != "running" else 0)
    stats = [{"label": label, "value": run["counts"][name]} for name, label in STAT_LABELS.items()
             if run.get("counts", {}).get(name)]
    return run | {"percent": percent, "stats": stats, "job_id": running_jobs.get(run.get("pid"))}


def list_runs(data_dir, runner, kind=None):
    """
    :param data_dir: The data directory.
    :param runner: The JobRunner, for matching runs this dashboard started.
    :param kind: Optional "scrape" or "open_calls".
    :return: Runs newest first, recorded ones before rebuilt ones of the same moment.
    """
    running_jobs = {job.process.pid: job.id for job in runner.jobs.values() if job.running and job.process}
    runs = saved_runs(data_dir) + past_runs(data_dir)
    runs = [with_stats(run, running_jobs) for run in runs if not kind or run["kind"] == kind]
    # Running first, then newest
    return sorted(runs, key=lambda run: (run["state"] == "running", run["started"]), reverse=True)


def find_run(data_dir, runner, run_id):
    """
    :return: One run by id.
    :raises FileNotFoundError: If there is no such run.
    """
    run = next((run for run in list_runs(data_dir, runner) if run["id"] == run_id), None)
    if not run:
        raise FileNotFoundError(f"Unknown run {run_id}")
    return run
