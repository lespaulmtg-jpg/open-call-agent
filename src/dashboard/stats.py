"""
The summary statistics at the top of each dashboard view.
"""
from datetime import date

from open_call_agent.urls import website_key
from open_call_agent.paths import EVENTS_PREFIX, FESTIVALS_PREFIX, reports_dir, reviews_dir, safe_filename, scraped_dir
from open_call_agent.ra.events import read_rows, split_links
from open_call_agent.storage import read_json

from .notes import COMPLETED_STATUSES, EXCLUDED_STATUSES
from .sources import load_rows


def statistic(label, value, note=""):
    return {"label": label, "value": value, "note": note}


def table_stats(data_dir, source_id, notes):
    """
    :param data_dir: The data directory.
    :param source_id: The data source shown in the table.
    :param notes: The notes, from read_notes.
    :return: A list of statistics: completed applications, possible applications and websites needing review.
    """
    rows = load_rows(data_dir, source_id) if source_id else []
    statuses = {row["id"]: (notes.get(row["id"]) or {}).get("status", "") for row in rows}
    completed = sum(status in COMPLETED_STATUSES for status in statuses.values())
    applied = sum(status == "applied" for status in statuses.values())

    today = date.today().isoformat()
    possible = [row for row in rows if row["status"] == "open" and statuses[row["id"]] not in
                COMPLETED_STATUSES | EXCLUDED_STATUSES]
    upcoming = sum(not row["deadline"] or row["deadline"] >= today for row in possible)
    needs_review = sum(row["status"] == "needs_review" for row in rows)

    return [
        statistic("Total Completed Applications", completed, f"{applied} awaiting an answer" if applied else ""),
        statistic("Possible Applications", len(possible), f"{upcoming} with a deadline ahead"),
        statistic("Needs Review Websites", needs_review, "open call unclear"),
        statistic("Festivals", len(rows), "in this data source"),
    ]


def count_csv_rows(paths):
    """
    :param paths: CSV Paths with a header line.
    :return: The total number of data rows.
    """
    total = 0
    for path in paths:
        with open(path, encoding="utf-8") as file:
            total += max(sum(1 for _ in file) - 1, 0)
    return total


def scrape_stats(data_dir, runner):
    """
    :param data_dir: The data directory.
    :param runner: The JobRunner.
    :return: A list of statistics: events and festivals scraped, cities covered and running scrapers.
    """
    events = sorted(scraped_dir(data_dir).glob(f"*/*/{EVENTS_PREFIX}*.csv"))
    festivals = sorted(scraped_dir(data_dir).glob(f"*/*/{FESTIVALS_PREFIX}*.csv"))
    countries = {path.parent.parent.name for path in events}
    return [
        statistic("Events Found", count_csv_rows(events), f"{len(events)} CSV files"),
        statistic("Festivals Found", count_csv_rows(festivals), "events lasting more than 24 hours"),
        statistic("Scrapers Running", runner.running_jobs(["scrape"]), "started from this dashboard"),
        statistic("Cities Covered", len({path.parent.name for path in events}), f"{len(countries)} countries"),
    ]


def festival_websites(data_dir):
    """
    :param data_dir: The data directory.
    :return: The keys of every promoter website of a scraped festival, as the checkers count them.
    """
    websites = set()
    for path in scraped_dir(data_dir).glob(f"*/*/{FESTIVALS_PREFIX}*.csv"):
        for row in read_rows(path):
            websites.update(website_key(website) for website in split_links(row["Promoter Websites"]))
    return websites


def llm_stats(data_dir, runner):
    """
    :param data_dir: The data directory.
    :param runner: The JobRunner.
    :return: A list of statistics: websites checked by each model, open calls found and websites still to check.
    """
    reports = sorted(reports_dir(data_dir).glob("*.json"))
    reviews = sorted(reviews_dir(data_dir).glob("*.json"))
    open_calls = sum((read_json(path) or {}).get("review", {}).get("status") == "open" for path in reviews)
    still_open = sum((read_json(path) or {}).get("status") == "open" for path in reports)

    websites = festival_websites(data_dir)
    checked = {path.stem for path in reports}
    unchecked = len({key for key in websites if safe_filename(key) not in checked})

    return [
        statistic("Websites Checked", len(reports), "by the 3B model"),
        statistic("Open Calls Found", open_calls, f"confirmed by the 7B model of {still_open} the 3B model found"),
        statistic("Websites Left To Check", unchecked, f"of {len(websites)} festival websites"),
        statistic("Model Jobs Running", runner.running_jobs(["check-open-calls", "check-europe"]),
                  "only one model uses the GPU"),
    ]
