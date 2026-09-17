"""
Identify festivals in scraped event CSVs: events lasting more than 24 hours.
"""
import logging
from datetime import datetime

from .paths import EVENTS_PREFIX, festivals_csv_path, find_csvs
from .ra.events import CSV_COLUMNS, read_rows, write_rows

logger = logging.getLogger(__name__)

MIN_FESTIVAL_HOURS = 24
FESTIVAL_COLUMNS = CSV_COLUMNS + ["Duration Hours"]


def event_duration_hours(row):
    """
    Get an event's duration from its start and end times.

    :param row: An events CSV row.
    :return: The duration in hours, or None if a time is missing or invalid.
    """
    try:
        # RA times are local ISO timestamps, e.g. "2026-09-18T23:00:00.000"
        start = datetime.fromisoformat(row["Start Time"][:19])
        end = datetime.fromisoformat(row["End Time"][:19])
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds() / 3600


def find_festivals(rows, min_hours=MIN_FESTIVAL_HOURS):
    """
    Filter events lasting more than min_hours.

    :param rows: Events CSV rows.
    :param min_hours: The minimum duration, exclusive.
    :return: The festival rows with a "Duration Hours" column added.
    """
    festivals = []
    for row in rows:
        hours = event_duration_hours(row)
        if hours is not None and hours > min_hours:
            festivals.append({**row, "Duration Hours": round(hours, 1)})
    return festivals


def identify_festivals(data_dir, country_code=None, city=None, min_hours=MIN_FESTIVAL_HOURS):
    """
    Write a festivals CSV next to every events CSV in the data directory.

    :param data_dir: The data directory.
    :param country_code: Optional RA country code to limit the search.
    :param city: Optional city folder name to limit the search.
    :param min_hours: The minimum duration, exclusive.
    :return: A list of written festivals CSV Paths.
    """
    paths = []
    for events_path in find_csvs(data_dir, EVENTS_PREFIX, country_code, city):
        festivals = find_festivals(read_rows(events_path), min_hours)
        path = festivals_csv_path(events_path)
        write_rows(festivals, path, FESTIVAL_COLUMNS)
        logger.info("%s: %d festivals -> %s", events_path.parent.name, len(festivals), path)
        paths.append(path)
    return paths
