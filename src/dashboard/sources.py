"""
Data sources for the festivals table: summary CSVs written by the Europe script, and open call results CSVs joined
with the scraped festivals on the fly. Rows are cached until the underlying files change.
"""
import logging
from datetime import datetime
from pathlib import Path

from open_call_agent.paths import FESTIVALS_PREFIX, open_calls_dir, scraped_dir
from open_call_agent.ra.events import read_rows
from open_call_agent.summary import FESTIVAL_SUMMARY_COLUMNS, build_festival_rows, festival_key

logger = logging.getLogger(__name__)

ROW_COLUMNS = ["id", "festival", "month", "start_date", "end_date", "status", "deadline", "open_call_url",
               "festival_url", "website", "source", "source_url", "country", "country_code", "city", "promoters"]


def summary_paths(data_dir):
    """
    :return: Festival summary CSV Paths, newest first.
    """
    directory = open_calls_dir(data_dir) / "summary"
    paths = [path for path in directory.glob("*.csv") if not path.stem.endswith("_by_country")]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def results_paths(data_dir):
    """
    :return: Open call results CSV Paths, reviewed ones first, newest first.
    """
    paths = sorted(open_calls_dir(data_dir).glob("open_calls_*.csv"), key=lambda path: path.stat().st_mtime,
                   reverse=True)
    return sorted(paths, key=lambda path: not path.stem.endswith("_reviewed"))


def festival_paths(data_dir):
    """
    :return: Every scraped festivals CSV Path.
    """
    return sorted(scraped_dir(data_dir).glob(f"*/*/{FESTIVALS_PREFIX}*.csv"))


def source_label(path, kind):
    """
    :return: A readable name for a data source, e.g. "europe festivals 2025-01-01 2026-12-31 (reviewed)".
    """
    name = path.stem.replace("open_calls_", "").replace("_", " ")
    if kind == "results":
        name = name.replace(" reviewed", "") + (" (reviewed)" if path.stem.endswith("_reviewed") else " (small model)")
    return name


def list_sources(data_dir):
    """
    Find the data sources the table can be built from.

    :param data_dir: The data directory.
    :return: A list of dicts with id, label, kind and updated, newest first within each kind.
    """
    sources = []
    for kind, paths in (("summary", summary_paths(data_dir)), ("results", results_paths(data_dir))):
        for path in paths:
            sources.append({"id": f"{kind}:{path.name}", "label": source_label(path, kind), "kind": kind,
                            "updated": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
    return sources


def source_path(data_dir, source_id):
    """
    :param source_id: An id from list_sources, e.g. "results:open_calls_ch_festivals_2026-09-17_reviewed.csv".
    :return: A tuple of (kind, Path).
    :raises FileNotFoundError: If the source doesn't exist.
    """
    kind, _, name = (source_id or "").partition(":")
    directory = open_calls_dir(data_dir) / "summary" if kind == "summary" else open_calls_dir(data_dir)
    path = directory / Path(name).name
    if kind not in ("summary", "results") or not path.is_file():
        raise FileNotFoundError(f"Unknown data source '{source_id}'")
    return kind, path


def default_source(data_dir):
    """
    :return: The id of the most recently written source, so a fresh check is shown rather than an older summary, or
             None if there is no data yet.
    """
    sources = list_sources(data_dir)
    return max(sources, key=lambda source: source["updated"])["id"] if sources else None


def row_id(summary_row):
    """
    :param summary_row: A festival summary row.
    :return: A stable id for the festival, the same across rebuilds of the table, used to store notes against.
    """
    key = festival_key({"Event name": summary_row["Festival"], "Country": summary_row["Country Code"],
                        "City": summary_row["City"], "Event ID": summary_row["Event ID"]})
    return "|".join(key)


def first_link(value):
    """
    :param value: A column holding one or more URLs separated by ", ".
    :return: The first URL, or an empty string.
    """
    return (value or "").split(", ")[0]


def working_link(value, checks):
    """
    Pick the first URL that its link check didn't find wanting, so the table doesn't send you to a page that is broken
    or that redirects visitors elsewhere, e.g. a volunteer portal whose event page is gone.

    :param value: A column holding one or more URLs separated by ", ".
    :param checks: The matching *_check column, results separated by " | ".
    :return: The first working URL, the first URL if none of them work, or an empty string.
    """
    urls = [url for url in (value or "").split(", ") if url]
    results = (checks or "").split(" | ")
    working = [url for index, url in enumerate(urls)
               if index >= len(results) or not results[index].startswith(("broken", "redirected"))]
    return (working or urls or [""])[0]


def to_row(summary_row):
    """
    :param summary_row: A festival summary row.
    :return: A table row: the columns the table shows, plus every summary column for the expanded view.
    """
    start = summary_row["Start Date"]
    month = datetime.fromisoformat(start).strftime("%b %Y") if start else ""
    return {
        "id": row_id(summary_row),
        "festival": summary_row["Festival"],
        "month": month,
        "start_date": start,
        "end_date": summary_row["End Date"],
        "status": summary_row["Open Call Status"],
        "deadline": summary_row["Deadline"],
        # A call's application link, or the page announcing it when the application link no longer works
        "open_call_url": working_link(summary_row["Open Call URL"], summary_row.get("Open Call URL Check")) or
                         working_link(summary_row["Announcement URL"], summary_row.get("Announcement URL Check")),
        # The festival's own website, with the RA listing it was scraped from as the source
        "festival_url": first_link(summary_row["Website"]),
        "website": summary_row["Website"],
        "source": "RA",
        "source_url": summary_row["Event URL"],
        "country": summary_row["Country"] or summary_row["Country Code"].upper(),
        "country_code": summary_row["Country Code"],
        "city": summary_row["City"],
        "promoters": summary_row["Promoters"],
        # Only what the expanded row shows, so the table of thousands of festivals stays a small download
        "details": {column: summary_row[column] for column in FESTIVAL_SUMMARY_COLUMNS if summary_row.get(column)},
    }


_cache = {}


def load_rows(data_dir, source_id):
    """
    Load the festivals table for a data source, cached until its files change.

    :param data_dir: The data directory.
    :param source_id: An id from list_sources.
    :return: A list of table rows.
    """
    kind, path = source_path(data_dir, source_id)
    festivals = festival_paths(data_dir) if kind == "results" else []
    stamp = (path.stat().st_mtime, tuple((str(csv), csv.stat().st_mtime) for csv in festivals))
    cached = _cache.get(source_id)
    if cached and cached[0] == stamp:
        return cached[1]

    if kind == "summary":
        summary_rows = read_rows(path)
    else:
        logger.info("Building the festivals table from %s and %d festival CSVs", path.name, len(festivals))
        summary_rows = build_festival_rows(festivals, path)
    rows = [to_row(row) for row in summary_rows]
    _cache[source_id] = (stamp, rows)
    return rows
