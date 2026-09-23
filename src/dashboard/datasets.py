"""
What data there is to look at, and what has been done to it: the RA scrapes on disk, the open call results built from
them, and the summary tables.

Each dataset answers the questions the file names don't: what it covers, when it was written, and which steps of the
pipeline have been run over it — the models by name, with how much of the dataset they actually reached, since a
stopped check leaves a dataset that looks finished until you count its rows.
"""
from collections import Counter, defaultdict
from datetime import datetime

from open_call_agent.open_call.batch import row_websites
from open_call_agent.paths import (EVENTS_PREFIX, festivals_csv_path, raw_events_dir, reports_dir, reviews_dir,
                                   safe_filename, scraped_dir)
from open_call_agent.ra.events import read_rows
from open_call_agent.storage import read_json
from open_call_agent.urls import website_key

from .sources import list_sources, source_path


def written_at(path):
    """
    :param path: Any Path.
    :return: When it was last written, as an ISO string.
    """
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def count_rows(path):
    """
    :param path: A CSV Path with a header line.
    :return: The number of data rows, counted without parsing (event CSVs run to tens of megabytes).
    """
    with open(path, encoding="utf-8") as file:
        return max(sum(1 for _ in file) - 1, 0)


def fact(label, value):
    return {"label": label, "value": value}


def step(label, state, detail=""):
    """
    :param label: The pipeline step.
    :param state: "done", "partial" or "none".
    :param detail: What was applied, e.g. the model and when it ran.
    """
    return {"label": label, "state": state, "detail": detail}


def scrape_datasets(data_dir):
    """
    The RA scrapes on disk, one dataset per date range, since that is what a scrape covers and what its file names say.

    :param data_dir: The data directory.
    :return: A list of dataset dicts, newest first.
    """
    by_period = defaultdict(list)
    for path in sorted(scraped_dir(data_dir).glob(f"*/*/{EVENTS_PREFIX}*.csv")):
        by_period[path.stem.removeprefix(EVENTS_PREFIX)].append(path)

    datasets = []
    for period, paths in sorted(by_period.items(), reverse=True):
        festival_paths = [path for path in map(festivals_csv_path, paths) if path.exists()]
        events = sum(count_rows(path) for path in paths)
        countries = {path.parent.parent.name for path in paths}
        start, _, end = period.partition("_")

        festival_rows = [row for path in festival_paths for row in read_rows(path)]
        websites = {website_key(website) for row in festival_rows for website in row_websites(row)}
        checked = sum(1 for key in websites if (reports_dir(data_dir) / f"{safe_filename(key)}.json").exists())
        # Raw responses are filed by event, not by scrape, so only this scrape's own festivals count towards it
        saved_ids = {path.stem for path in raw_events_dir(data_dir).rglob("*.json")}
        raw = len(saved_ids & {row["Event ID"] for row in festival_rows})
        festivals = len(festival_rows)

        datasets.append({
            "id": f"scrape:{period}",
            "kind": "scrape",
            "label": f"RA scrape {start} to {end}",
            "updated": max(written_at(path) for path in paths),
            "path": str(scraped_dir(data_dir)),
            "source_id": None,
            "facts": [fact("Countries", len(countries)), fact("Cities", len(paths)), fact("Events", events),
                      fact("Festivals", festivals), fact("Websites", len(websites))],
            "steps": [
                step("Scraped from ra.co", "done",
                     f"{len(paths)} city CSVs in {len(countries)} countr{'y' if len(countries) == 1 else 'ies'}"),
                step("Festivals identified", "done" if festival_paths else "none",
                     f"{festivals:,} events last longer than 24 hours" if festival_paths
                     else "run the festivals command"),
                step("Raw API responses kept", "done" if raw else "none",
                     f"{raw:,} of this scrape's festivals have their full RA response saved" if raw
                     else "scraped without --save-raw"),
                step("Websites checked", "done" if checked >= len(websites) else "partial" if checked else "none",
                     f"{checked:,} of {len(websites):,} festival websites have a saved report"),
            ],
        })
    return datasets


def results_facts(rows, status_column):
    """
    :param rows: The rows of a results or summary CSV.
    :param status_column: Where the status lives in them.
    :return: The counts shown on the card: how many rows, and how many of each status.
    """
    statuses = Counter(row.get(status_column, "") for row in rows)
    counts = [fact(status.replace("_", " ").title(), statuses[status])
              for status in ("open", "needs_review", "closed", "none", "not_checked", "no_website")
              if statuses.get(status)]
    return [fact("Rows", len(rows))] + counts


def model_name(*paths):
    """
    :param paths: Report or review JSON Paths to try in turn; a CSV can name a path from before the data moved.
    :return: The model file that wrote the first one that can be read, or "".
    """
    return next((name for path in paths if path and (name := (read_json(path) or {}).get("model"))), "")


def results_steps(data_dir, rows):
    """
    What has been run over a results CSV: the small model, the link checks and the large model's review, each with the
    model that did it and when, so a check that was stopped half way says so rather than looking finished.

    :param data_dir: The data directory.
    :param rows: The rows of the results CSV.
    :return: A list of steps.
    """
    checked_on = sorted({row["checked_on"] for row in rows if row.get("checked_on")})
    when = f"{checked_on[0]} to {checked_on[-1]}" if len(checked_on) > 1 else (checked_on[0] if checked_on else "")
    # A site that couldn't be read records no model, so keep looking until one of them names it
    small = next((name for row in rows
                  if (name := model_name(row.get("report_path"),
                                         reports_dir(data_dir) / f"{safe_filename(website_key(row['website']))}.json"))),
                 "")

    linked = sum(1 for row in rows if row.get("website_check"))
    reviewed = [row for row in rows if row.get("large_model_status")]
    large = next((model_name(reviews_dir(data_dir) / f"{safe_filename(website_key(row['website']))}.json")
                  for row in reviewed), "")
    # Only sites the small model found worth a second look are reviewed, so that is what the review is measured against
    worth_reviewing = sum(1 for row in rows if (row.get("small_model_status") or row.get("status"))
                          in ("open", "needs_review"))

    return [
        step("Small model", "done" if rows else "none", f"{len(rows):,} websites · {small or 'unknown model'}"
             + (f" · {when}" if when else "")),
        step("Link and email checks", "done" if linked >= len(rows) and rows else "partial" if linked else "none",
             f"{linked:,} of {len(rows):,} rows checked" if linked
             else "not run: they run when a check finishes, so a stopped run has none"),
        step("Large model review", "done" if reviewed and len(reviewed) >= worth_reviewing
             else "partial" if reviewed else "none",
             f"{len(reviewed):,} of {worth_reviewing:,} open or needs-review sites" + (f" · {large}" if large else "")
             if reviewed else "not run"),
    ]


def summary_steps(rows):
    """
    :param rows: The rows of a summary CSV.
    :return: What the summary already holds, since it is built from results rather than producing any of its own.
    """
    reviewed = sum(1 for row in rows if row.get("Large Model Status"))
    linked = sum(1 for row in rows if row.get("Link Checks"))
    return [
        step("Built from festivals and results", "done", f"{len(rows):,} festivals, editions merged"),
        step("Large model review", "done" if reviewed else "none",
             f"{reviewed:,} festivals carry a reviewed status" if reviewed else "not in the results it was built from"),
        step("Link checks recorded", "done" if linked else "none",
             f"{linked:,} festivals have link check notes" if linked else "none in the results it was built from"),
    ]


def result_datasets(data_dir):
    """
    The open call results and summary tables, i.e. everything the festivals table can be built from.

    :param data_dir: The data directory.
    :return: A list of dataset dicts, newest first.
    """
    datasets = []
    for source in list_sources(data_dir):
        kind, path = source_path(data_dir, source["id"])
        rows = read_rows(path)
        summary = kind == "summary"
        datasets.append({
            "id": source["id"],
            "kind": kind,
            "label": source["label"],
            "updated": source["updated"],
            "path": str(path),
            "source_id": source["id"],
            "facts": results_facts(rows, "Open Call Status" if summary else "status"),
            "steps": summary_steps(rows) if summary else results_steps(data_dir, rows),
        })
    return datasets


def list_datasets(data_dir):
    """
    :param data_dir: The data directory.
    :return: Every dataset, results first (they are what you browse), then the scrapes they were built from.
    """
    return result_datasets(data_dir) + scrape_datasets(data_dir)


def dataset_stats(datasets):
    """
    :param datasets: The datasets from list_datasets.
    :return: The figures at the top of the view.
    """
    scrapes = [dataset for dataset in datasets if dataset["kind"] == "scrape"]
    results = [dataset for dataset in datasets if dataset["kind"] != "scrape"]
    festivals = max((next((f["value"] for f in scrape["facts"] if f["label"] == "Festivals"), 0)
                     for scrape in scrapes), default=0)
    checked = max((next((f["value"] for f in result["facts"] if f["label"] == "Rows"), 0)
                   for result in results), default=0)
    countries = max((next((f["value"] for f in scrape["facts"] if f["label"] == "Countries"), 0)
                     for scrape in scrapes), default=0)
    newest = max((dataset["updated"] for dataset in datasets), default="")
    return [
        {"label": "Datasets", "value": len(datasets), "note": f"newest {newest[:10]}" if newest else ""},
        {"label": "Festivals Scraped", "value": festivals, "note": "in the largest scrape"},
        {"label": "Websites Checked", "value": checked, "note": "in the largest check"},
        {"label": "Countries Scraped", "value": countries, "note": f"{len(results)} datasets you can browse"},
    ]
