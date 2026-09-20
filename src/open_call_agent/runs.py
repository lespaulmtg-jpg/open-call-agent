"""
Record what a scraper or checker run did, as data/runs/<started>_<kind>.json, so the dashboard can show past runs and
follow running ones: how far it got, what it found, and what went wrong.

A run is started by the command being run (the CLI or a script) and reported to from wherever the work happens:

    with runs.run("scrape", data_dir, "Switzerland 2026-09-16 to 2026-09-18", {"country": "ch"}):
        ...
        runs.report(events=15, cities={"zurich"})

Reporting when no run is active does nothing, so the library code can report unconditionally.
"""
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime

from .paths import runs_dir
from .storage import write_json_atomic

logger = logging.getLogger(__name__)

SAVE_EVERY_SECONDS = 2  # A running job is saved at most this often; finishing always saves
MAX_ERRORS = 50

_current = None


class Run:
    """
    One run of a command, saved as JSON while it goes.
    """

    def __init__(self, kind, data_dir, label, arguments=None):
        self.started = datetime.now()
        self.id = f"{self.started:%Y%m%d_%H%M%S}_{kind}"
        self.kind = kind
        self.label = label
        # Arguments are only shown, and some are dates or paths, so they are kept as text
        self.arguments = {name: str(value) for name, value in (arguments or {}).items()}
        self.path = runs_dir(data_dir) / f"{self.id}.json"
        self.pid = os.getpid()
        self.state = "running"
        self.finished = None
        self.progress = {"done": 0, "total": 0, "unit": ""}
        self.counts = {}
        self.sets = {}
        self.errors = []
        self._saved_at = 0

    def as_dict(self):
        duration = (self.finished or datetime.now()) - self.started
        return {"id": self.id, "kind": self.kind, "label": self.label, "arguments": self.arguments, "pid": self.pid,
                "state": self.state, "started": self.started.isoformat(timespec="seconds"),
                "finished": self.finished.isoformat(timespec="seconds") if self.finished else None,
                "seconds": round(duration.total_seconds()), "progress": self.progress,
                "counts": self.counts | {name: len(values) for name, values in self.sets.items()},
                "errors": self.errors, "source": "run"}

    def save(self, force=False):
        if force or time.monotonic() - self._saved_at >= SAVE_EVERY_SECONDS:
            write_json_atomic(self.path, self.as_dict())
            self._saved_at = time.monotonic()


def report(progress=None, total=None, unit=None, errors=None, **counts):
    """
    Add to the current run's figures. Counts are added up; sets (passed as sets) collect their members.

    :param progress: How many units are done so far.
    :param total: How many units there are in total.
    :param unit: What is being counted through, e.g. "country" or "site".
    :param errors: Error messages to record.
    :param counts: Numbers to add up, e.g. events=15, or sets to collect, e.g. cities={"zurich"}.
    """
    if _current is None:
        return
    if progress is not None:
        _current.progress["done"] = progress
    if total is not None:
        _current.progress["total"] = total
    if unit is not None:
        _current.progress["unit"] = unit
    for message in errors or []:
        if len(_current.errors) < MAX_ERRORS:
            _current.errors.append(message)
    for name, value in counts.items():
        if isinstance(value, (set, frozenset)):
            _current.sets.setdefault(name, set()).update(value)
        else:
            _current.counts[name] = _current.counts.get(name, 0) + value
    _current.save()


def set_total(total, unit, once=False):
    """
    Say how many units this run has to get through, e.g. 46 countries. A country scrape inside a continent scrape must
    not take over the count, so it asks with once=True and only counts when it gets it.

    :param total: How many units there are.
    :param unit: What is being counted, e.g. "country".
    :param once: Don't take over a count that is already set.
    :return: True if this caller owns the count and should report progress.
    """
    if _current is None:
        return False
    if once and _current.progress["total"]:
        return False
    _current.progress.update(total=total, unit=unit)
    _current.save()
    return True


class ErrorCollector(logging.Handler):
    """
    Records logged errors on the run, so its window shows what went wrong.
    """

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            report(errors=[f"{record.asctime if hasattr(record, 'asctime') else ''} {record.getMessage()}".strip()])


@contextmanager
def run(kind, data_dir, label, arguments=None):
    """
    Record a run for as long as the block runs: "done" when it returns, "stopped" on Ctrl+C, "failed" on an error.

    :param kind: "scrape" or "open_calls".
    :param data_dir: The data directory.
    :param label: What the run is, e.g. "Switzerland, 2026-09-16 to 2026-09-18".
    :param arguments: The options it was started with.
    """
    global _current

    _current = Run(kind, data_dir, label, arguments)
    handler = ErrorCollector()
    logging.getLogger().addHandler(handler)
    _current.save(force=True)
    try:
        yield _current
        _current.state = "done"
    except KeyboardInterrupt:
        _current.state = "stopped"
        raise
    except BaseException as error:
        _current.state = "failed"
        report(errors=[f"{type(error).__name__}: {error}"])
        raise
    finally:
        logging.getLogger().removeHandler(handler)
        _current.finished = datetime.now()
        _current.save(force=True)
        _current = None
