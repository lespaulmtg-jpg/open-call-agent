"""
Rebuild runs that happened before runs were recorded, by reading the log files they left in data/logs (and
data/scraped_data/logs, where older logs were moved).

These are read back, never written: a reconstructed run carries what its log says and nothing more, so its figures can
be short of what really happened if the log was truncated or the run was killed before it could report.
"""
import logging
import re
from datetime import datetime

from .paths import logs_dir, scraped_dir

logger = logging.getLogger(__name__)

TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
# What the scrapers and checkers print, and the figure to take from each line
PATTERNS = {
    "events": re.compile(r"INFO open_call_agent\.scraper: \S+: (\d+) events ->"),
    "festivals": re.compile(r": (\d+) festivals ->"),
    "cities": re.compile(r"INFO open_call_agent\.scraper: ([a-z]{2}/[^:]+):"),
    "websites": re.compile(r"(\d+) websites to check"),
    "checked": re.compile(r"\[(\d+)/(\d+)\] \S+: (open|closed|none|needs_review) "),
    "reviewed": re.compile(r"INFO open_call_agent\.open_call\.review: \S+: \w+ -> (\w+)"),
    "countries": re.compile(r"Scraping (\d+) countries"),
    "chunks": re.compile(r"\[(\d+)/(\d+)\] .+: \d+ events in \d+ cities"),
}
# A whole-continent scrape counts its events itself, once, after merging the monthly chunks it wrote
TOTALS_RE = re.compile(r"Done: (\d+) events and (\d+) festivals in (\d+) city CSVs")
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL)\b (.*)")
STOPPED_RE = re.compile(r"Stopped\.|KeyboardInterrupt")


def log_paths(data_dir):
    """
    :param data_dir: The data directory.
    :return: The log files of past runs, newest first.
    """
    paths = list(logs_dir(data_dir).glob("*.log")) + list((scraped_dir(data_dir) / "logs").glob("*.log"))
    paths = [path for path in paths if not path.name.startswith("llama-server")]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def run_from_log(path):
    """
    Read one log file as a run.

    :param path: The log Path.
    :return: A run dict in the same shape as runs.Run.as_dict, or None if the file holds no timestamped lines. A log
             that ends in a stop message is a stopped run; the rest are taken as finished.
    """
    kind = "scrape" if "scrape" in path.stem else "open_calls"
    counts, cities, errors = {}, set(), []
    statuses = {}
    stopped = False
    first = last = None
    progress = {"done": 0, "total": 0, "unit": ""}

    with open(path, encoding="utf-8", errors="replace") as file:
        for line in file:
            stamp = TIMESTAMP_RE.match(line)
            if stamp:
                first = first or stamp.group(1)
                last = stamp.group(1)
            for name, pattern in PATTERNS.items():
                match = pattern.search(line)
                if not match:
                    continue
                if name == "cities":
                    cities.add(match.group(1))
                elif name == "checked":
                    progress = {"done": int(match.group(1)), "total": int(match.group(2)), "unit": "site"}
                    statuses[match.group(3)] = statuses.get(match.group(3), 0) + 1
                    counts["checked"] = counts.get("checked", 0) + 1
                elif name == "chunks":
                    progress = {"done": int(match.group(1)), "total": int(match.group(2)), "unit": "country-month"}
                elif name == "reviewed":
                    counts["reviewed"] = counts.get("reviewed", 0) + 1
                elif name in ("websites", "countries"):
                    counts[name] = int(match.group(1))
                else:
                    counts[name] = counts.get(name, 0) + int(match.group(1))
            totals = TOTALS_RE.search(line)
            if totals:
                # Per-chunk lines count the same events again for every month, so the merged totals win
                counts |= {"events": int(totals.group(1)), "festivals": int(totals.group(2)),
                           "cities": int(totals.group(3))}
                cities.clear()
            stopped = stopped or bool(STOPPED_RE.search(line))
            error = ERROR_RE.search(line)
            if error and len(errors) < 50:
                errors.append(error.group(2).strip())

    if not first:
        return None
    started, finished = datetime.fromisoformat(first), datetime.fromisoformat(last)
    counts |= statuses
    if cities:
        counts["cities"] = len(cities)
        counts.setdefault("countries", len({city.split("/")[0] for city in cities}))
    return {"id": f"log_{path.stem}", "kind": kind, "label": path.stem.replace("_", " "), "arguments": {},
            "pid": None, "state": "stopped" if stopped else "done", "started": started.isoformat(timespec="seconds"),
            "finished": finished.isoformat(timespec="seconds"), "seconds": round((finished - started).total_seconds()),
            "progress": progress, "counts": counts, "errors": errors, "source": "log", "log_path": str(path)}


def past_runs(data_dir):
    """
    :param data_dir: The data directory.
    :return: Runs rebuilt from the logs, newest first.
    """
    runs = []
    for path in log_paths(data_dir):
        try:
            run = run_from_log(path)
        except OSError as error:
            logger.warning("Could not read %s: %s", path, error)
            continue
        if run:
            runs.append(run)
    return runs
