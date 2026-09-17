"""
Run the RA scraper and the open call checkers from the dashboard as separate processes, with their log kept in memory.

Options are declared in JOB_TYPES and turned into command line arguments here, so the dashboard can only start the
commands below with the options listed. Stopping a job sends SIGTERM, which the CLI handles like Ctrl+C: everything
finished so far is saved. Only one job may use the GPU at a time.
"""
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from itertools import count
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 2000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def clean_log_line(line):
    """
    Make one line of a scraper's output readable in the browser: progress bars redraw themselves with carriage
    returns and terminal escape codes, of which only the last state is worth keeping.

    :param line: A line of output.
    :return: The cleaned line, or an empty string if nothing is left.
    """
    parts = [ANSI_RE.sub("", part).strip() for part in line.rstrip("\n").split("\r")]
    return next((part for part in reversed(parts) if part), "")

# Each field becomes a command line argument: a flag on its own for checkboxes, "--flag value" otherwise.
JOB_TYPES = {
    "scrape": {
        "label": "RA scraper",
        "command": ["scrape"],
        "gpu": False,
        "fields": [
            {"name": "where", "label": "Scrape", "type": "choice", "default": "country",
             "options": [{"value": "continent", "label": "Continent", "flag": "--continent", "placeholder": "europe"},
                         {"value": "country", "label": "Country", "flag": "--country", "placeholder": "ch"},
                         {"value": "city", "label": "City", "flag": "--city", "placeholder": "ch/zurich"}]},
            {"name": "start", "label": "Start date", "type": "date", "flag": "--start", "default": "2026-01-01",
             "required": True},
            {"name": "end", "label": "End date", "type": "date", "flag": "--end", "default": "2026-12-31",
             "required": True},
            {"name": "festivals", "label": "Identify festivals as well", "type": "checkbox", "flag": "--festivals",
             "default": True},
        ],
    },
    "check-open-calls": {
        "label": "Open call check",
        "command": ["check-open-calls"],
        "gpu": True,
        "fields": [
            {"name": "where", "label": "Check", "type": "choice", "default": "country",
             "options": [{"value": "all", "label": "Everything scraped"},
                         {"value": "country", "label": "Country", "flag": "--country", "placeholder": "ch"},
                         {"value": "city", "label": "City", "flag": "--city", "placeholder": "zurich",
                          "with": {"flag": "--country", "name": "country", "placeholder": "ch"}}]},
            {"name": "festivals_only", "label": "Festivals only", "type": "checkbox", "flag": "--festivals-only",
             "default": True},
            {"name": "limit", "label": "Maximum websites (blank: all)", "type": "number", "flag": "--limit"},
            {"name": "no_review", "label": "Skip the 7B review", "type": "checkbox", "flag": "--no-review"},
            {"name": "no_reuse", "label": "Check sites again even if already checked", "type": "checkbox",
             "flag": "--no-reuse"},
            {"name": "ignore_robots", "label": "Ignore robots.txt", "type": "checkbox", "flag": "--ignore-robots"},
            {"name": "min_page_date", "label": "Ignore pages older than", "type": "text", "flag": "--min-page-date",
             "default": "2025-01-01"},
            {"name": "crawl_workers", "label": "Sites crawled at once", "type": "number", "flag": "--crawl-workers",
             "default": 8},
            {"name": "llm_slots", "label": "3B model slots", "type": "number", "flag": "--llm-slots", "default": 4},
            {"name": "review_slots", "label": "7B model slots", "type": "number", "flag": "--review-slots",
             "default": 2},
        ],
    },
    "check-europe": {
        "label": "Europe check and summary",
        "script": "scripts/check_europe_festivals.py",
        "gpu": True,
        "fields": [
            {"name": "period", "label": "Scrape period", "type": "text", "flag": "--period",
             "default": "2025-01-01_2026-12-31"},
            {"name": "limit", "label": "Maximum websites (blank: all)", "type": "number", "flag": "--limit"},
            {"name": "summary_only", "label": "Only rebuild the summary tables", "type": "checkbox",
             "flag": "--summary-only"},
            {"name": "no_review", "label": "Skip the 7B review", "type": "checkbox", "flag": "--no-review"},
            {"name": "no_reuse", "label": "Check sites again even if already checked", "type": "checkbox",
             "flag": "--no-reuse"},
            {"name": "ignore_robots", "label": "Ignore robots.txt", "type": "checkbox", "flag": "--ignore-robots"},
            {"name": "crawl_workers", "label": "Sites crawled at once", "type": "number", "flag": "--crawl-workers",
             "default": 8},
            {"name": "llm_slots", "label": "3B model slots", "type": "number", "flag": "--llm-slots", "default": 4},
            {"name": "review_slots", "label": "7B model slots", "type": "number", "flag": "--review-slots",
             "default": 2},
        ],
    },
}


def field_arguments(field, options):
    """
    Turn one declared field and the submitted options into command line arguments.

    :param field: A field from JOB_TYPES.
    :param options: The submitted options, keyed by field name.
    :return: A list of command line arguments.
    :raises ValueError: If a required value is missing or a choice is unknown.
    """
    value = options.get(field["name"])
    if field["type"] == "checkbox":
        return [field["flag"]] if value else []
    if field["type"] == "choice":
        choice = next((option for option in field["options"] if option["value"] == (value or field["default"])), None)
        if not choice:
            raise ValueError(f"Unknown {field['name']} '{value}'")
        arguments = []
        for part in ([choice["with"]] if "with" in choice else []) + ([choice] if "flag" in choice else []):
            # The value belonging to the choice has its own name, so it can't overwrite the choice itself
            text = str(options.get(part.get("name") or f"{field['name']}_value", "")).strip()
            if not text:
                raise ValueError(f"{field['label']}: {part.get('placeholder', 'a value')} is required")
            arguments += [part["flag"], text]
        return arguments

    text = str(value if value is not None else field.get("default", "")).strip()
    if not text:
        if field.get("required"):
            raise ValueError(f"{field['label']} is required")
        return []
    return [field["flag"], text]


def build_command(kind, options, data_dir):
    """
    :param kind: A key of JOB_TYPES.
    :param options: The submitted options, keyed by field name.
    :param data_dir: The data directory.
    :return: The command to run, as a list.
    :raises ValueError: If the job type or an option is invalid.
    """
    job_type = JOB_TYPES.get(kind)
    if not job_type:
        raise ValueError(f"Unknown job '{kind}'")
    # -u keeps the log flowing line by line instead of in blocks
    command = [sys.executable, "-u"]
    if "script" in job_type:
        command += [job_type["script"], "--data-dir", str(data_dir)]
    else:
        # The CLI takes --data-dir before the command name
        command += ["-m", "open_call_agent.cli", "--data-dir", str(data_dir)] + job_type["command"]
    for field in job_type["fields"]:
        command += field_arguments(field, options)
    return command


class Job:
    """
    One running or finished command, with its log.
    """

    def __init__(self, job_id, kind, command, gpu):
        self.id = job_id
        self.kind = kind
        self.command = command
        self.gpu = gpu
        self.process = None
        self.lines = deque(maxlen=MAX_LOG_LINES)
        self.started = datetime.now()
        self.finished = None
        self.returncode = None
        self.stopping = False

    @property
    def running(self):
        # A job counts as running from the moment it is registered, so a second GPU job can't slip in while this one
        # is still starting
        return self.returncode is None

    def state(self):
        """
        :return: "running", "stopping", "done", "stopped" or "failed".
        """
        if self.running:
            return "stopping" if self.stopping else "running"
        if self.returncode == 0:
            return "done"
        return "stopped" if self.returncode in (130, -signal.SIGTERM) else "failed"

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "label": JOB_TYPES[self.kind]["label"], "state": self.state(),
                "command": " ".join(self.command), "started": self.started.isoformat(timespec="seconds"),
                "finished": self.finished.isoformat(timespec="seconds") if self.finished else None,
                "returncode": self.returncode, "lines": len(self.lines), "last_line": self.lines[-1] if self.lines else ""}


class JobRunner:
    """
    Starts jobs, keeps their logs and stops them.
    """

    def __init__(self, data_dir, project_dir=None):
        self.data_dir = data_dir
        self.project_dir = Path(project_dir or Path.cwd())
        self.jobs = {}
        self.ids = count(1)
        self.lock = threading.Lock()

    def start(self, kind, options):
        """
        Start a job.

        :param kind: A key of JOB_TYPES.
        :param options: The submitted options, keyed by field name.
        :return: The new Job.
        :raises ValueError: If the options are invalid, or another job already uses the GPU.
        """
        command = build_command(kind, options, self.data_dir)
        with self.lock:
            gpu = JOB_TYPES[kind]["gpu"]
            if gpu and any(job.running and job.gpu for job in self.jobs.values()):
                raise ValueError("Another model job is running. Only one model can use the GPU at a time.")
            job = Job(next(self.ids), kind, command, gpu)
            self.jobs[job.id] = job

        logger.info("Job %d: %s", job.id, " ".join(command))
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(self.project_dir / "src"),
                                                                                os.environ.get("PYTHONPATH")])))
        try:
            job.process = subprocess.Popen(command, cwd=self.project_dir, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        except OSError as error:
            job.lines.append(f"Could not start: {error}")
            job.returncode, job.finished = -1, datetime.now()
            raise ValueError(f"Could not start the job: {error}")
        threading.Thread(target=self._read_log, args=(job,), daemon=True).start()
        return job

    def _read_log(self, job):
        for line in job.process.stdout:
            cleaned = clean_log_line(line)
            if cleaned:
                job.lines.append(cleaned)
        job.returncode = job.process.wait()
        job.finished = datetime.now()
        logger.info("Job %d %s (exit %s)", job.id, job.state(), job.returncode)

    def stop(self, job_id):
        """
        Stop a running job the safe way: SIGTERM, which the scrapers treat like Ctrl+C.

        :param job_id: A job id.
        :return: The Job.
        :raises ValueError: If there is no such job.
        """
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError(f"Unknown job {job_id}")
        if job.running and job.process:
            job.stopping = True
            # The job runs in its own process group, so the model server is signalled too
            os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
        return job

    def stop_all(self):
        """
        Stop every running job, e.g. when the dashboard shuts down.
        """
        for job in list(self.jobs.values()):
            if job.running:
                self.stop(job.id)

    def log(self, job_id, offset=0):
        """
        :param job_id: A job id.
        :param offset: Skip this many lines from the start of the kept log.
        :return: A dict with the lines from offset on, and the new offset.
        :raises ValueError: If there is no such job.
        """
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError(f"Unknown job {job_id}")
        lines = list(job.lines)[offset:]
        return {"job": job.as_dict(), "offset": offset + len(lines), "lines": lines}

    def as_list(self):
        """
        :return: Every job, newest first.
        """
        return [job.as_dict() for job in sorted(self.jobs.values(), key=lambda job: job.id, reverse=True)]

    def running_jobs(self, kinds=None):
        """
        :param kinds: Optional job types to count.
        :return: The number of running jobs.
        """
        return sum(job.running and (kinds is None or job.kind in kinds) for job in self.jobs.values())
