"""
Data directory layout:

    data/scraped_data/<country>/<city>/events_<start>_<end>.csv      scraped events
    data/scraped_data/<country>/<city>/festivals_<start>_<end>.csv   events lasting more than 24 hours
    data/open_calls/open_calls_<scope>_<date>.csv                    small model results per website
    data/open_calls/open_calls_<scope>_<date>_reviewed.csv           final results after the large model review
    data/open_calls/reports/<site>.json                              small model report per website
    data/open_calls/reviews/<site>.json                              large model review per website
    data/logs/                                                        logs
"""
import os
import re
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("OPEN_CALL_AGENT_DATA_DIR", "data"))
SCRAPED_DIR = "scraped_data"
OPEN_CALLS_DIR = "open_calls"
LOGS_DIR = "logs"
EVENTS_PREFIX = "events_"
FESTIVALS_PREFIX = "festivals_"


def scraped_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding scraped event and festival CSVs.
    """
    return Path(data_dir) / SCRAPED_DIR


def open_calls_dir(data_dir):
    """
    :param data_dir: The data directory.
    :return: The Path holding open call results, reports and reviews.
    """
    return Path(data_dir) / OPEN_CALLS_DIR


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
