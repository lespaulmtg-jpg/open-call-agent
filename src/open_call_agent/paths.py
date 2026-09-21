"""
Data directory layout:

    data/scraped_data/<country>/<city>/events_<start>_<end>.csv      scraped events
    data/raw_events/<country>/<city>/<event id>.json                 RA's full API response per festival (optional)
    data/scraped_data/<country>/<city>/festivals_<start>_<end>.csv   events lasting more than 24 hours
    data/open_calls/open_calls_<scope>_<date>.csv                    small model results per website
    data/open_calls/open_calls_<scope>_<date>_reviewed.csv           final results after the large model review
    data/open_calls/reports/<site>.json                              small model report per website
    data/open_calls/reviews/<site>.json                              large model review per website
    data/runs/<started>_<kind>.json                                   what each scraper or checker run did
    data/open_calls/summary/                                         per-festival and per-country summary tables
    data/dashboard/notes.json                                        your statuses and notes from the dashboard
    data/logs/                                                        logs
"""
import os
import re
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("OPEN_CALL_AGENT_DATA_DIR", "data"))
SCRAPED_DIR = "scraped_data"
RAW_EVENTS_DIR = "raw_events"
OPEN_CALLS_DIR = "open_calls"
LOGS_DIR = "logs"
RUNS_DIR = "runs"
EVENTS_PREFIX = "events_"
FESTIVALS_PREFIX = "festivals_"


def scraped_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding scraped event and festival CSVs.
    """
    return Path(data_dir) / SCRAPED_DIR


def raw_events_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding RA's full API response per festival, kept apart from the CSVs.
    """
    return Path(data_dir) / RAW_EVENTS_DIR


def raw_event_path(data_dir, country_code, city, event_id):
    """
    :return: The Path of one festival's raw API response.
    """
    return raw_events_dir(data_dir) / country_code.lower() / city / f"{event_id}.json"


def open_calls_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding open call results, reports and reviews.
    """
    return Path(data_dir) / OPEN_CALLS_DIR


def runs_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding one JSON file per scraper or checker run.
    """
    return Path(data_dir) / RUNS_DIR


def reports_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding the small model's report per website.
    """
    return open_calls_dir(data_dir) / "reports"


def reviews_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding the large model's review per website.
    """
    return open_calls_dir(data_dir) / "reviews"


def summary_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding the per-festival and per-country summary tables.
    """
    return open_calls_dir(data_dir) / "summary"


def logs_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding logs.
    """
    return Path(data_dir) / LOGS_DIR


def events_csv_path(data_dir, country_code, city, start_date, end_date):
    """
    :return: The Path of the events CSV for a city and date range.
    """
    return scraped_dir(data_dir) / country_code.lower() / city / f"{EVENTS_PREFIX}{start_date}_{end_date}.csv"


def festivals_csv_path(events_path):
    """
    :param events_path: The Path of an events CSV.
    :return: The Path of the matching festivals CSV.
    """
    return events_path.with_name(events_path.name.replace(EVENTS_PREFIX, FESTIVALS_PREFIX, 1))


def find_csvs(data_dir, prefix, country_code=None, city=None):
    """
    Find CSVs in the data directory, optionally limited to a country or city.

    :param data_dir: The data directory.
    :param prefix: EVENTS_PREFIX or FESTIVALS_PREFIX.
    :param country_code: Optional RA country code.
    :param city: Optional city folder name (requires country_code).
    :return: A sorted list of Paths.
    """
    pattern = f"{(country_code or '*').lower()}/{city or '*'}/{prefix}*.csv"
    return sorted(scraped_dir(data_dir).glob(pattern))


def safe_filename(text):
    """
    :param text: Any text, e.g. a URL.
    :return: The text with characters that aren't safe in file names replaced.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
