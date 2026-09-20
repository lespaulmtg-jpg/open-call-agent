"""
Scrape RA events by city, country or continent and save them as data/scraped_data/<country>/<city>/events_<start>_<end>.csv.

With save_raw, each festival's full API response is also saved as data/raw_events/<country>/<city>/<event id>.json,
separately from the CSVs. The CSVs hold the columns the rest of the code uses; the JSON keeps everything RA returned,
so a new column can later be filled from disk instead of asking RA again.
"""
import logging
from collections import defaultdict
from datetime import date

from . import runs
from .festivals import event_duration_hours
from .paths import events_csv_path, raw_event_path
from .progress import progress
from .ra.areas import area_folder, countries_in_continent, root_areas
from .ra.client import RAClient
from .ra.events import event_to_row, write_rows
from .storage import write_json_atomic

logger = logging.getLogger(__name__)


def save_raw_events(events, rows, data_dir, country_code, city):
    """
    Save the full API response of every festival among the events, one JSON file per festival.

    :param events: The events from the RA API.
    :param rows: The matching CSV rows, used to tell festivals from shorter events.
    :param data_dir: The data directory.
    :param country_code: The RA country code.
    :param city: The city folder name.
    :return: The number of files written.
    """
    festival_ids = {row["Event ID"] for row in rows if (event_duration_hours(row) or 0) > 24}
    for event in events:
        if str(event["id"]) in festival_ids:
            write_json_atomic(raw_event_path(data_dir, country_code, city, event["id"]),
                              {"scraped_on": date.today().isoformat(), "source": "ra.co", "event": event})
    return len(festival_ids)


def write_city(events, data_dir, country_code, folder, start_date, end_date, save_raw):
    """
    Write one city's events CSV, and the raw API responses of its festivals when asked.

    :param events: The events from the RA API.
    :return: The CSV Path.
    """
    rows = [event_to_row(event, country_code, folder) for event in events]
    path = events_csv_path(data_dir, country_code, folder, start_date, end_date)
    write_rows(rows, path)
    raw = save_raw_events(events, rows, data_dir, country_code, folder) if save_raw else 0
    logger.info("%s/%s: %d events -> %s%s", country_code.lower(), folder, len(rows), path,
                f" ({raw} festivals saved as JSON)" if save_raw else "")
    runs.report(events=len(rows), countries={country_code.lower()}, cities={f"{country_code.lower()}/{folder}"})
    return path


def scrape_city(country_code, city, start_date, end_date, data_dir, client=None, save_raw=False):
    """
    Scrape one city.

    :param country_code: The RA country code, e.g. "ch".
    :param city: The city name or RA URL slug, e.g. "zurich".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :param save_raw: Also save each festival's full API response as JSON.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    area = client.get_area(country_code, city)
    folder = area_folder(area)
    owns_progress = runs.set_total(1, "city", once=True)
    events = client.get_events(area["id"], start_date, end_date, f"{country_code.lower()}/{folder}")
    path = write_city(events, data_dir, country_code, folder, start_date, end_date, save_raw)
    if owns_progress:
        runs.report(progress=1)
    return [path]


def scrape_country(country_code, start_date, end_date, data_dir, client=None, save_raw=False):
    """
    Scrape a whole country, writing one CSV per city. Top-level areas are fetched once and each event is filed
    under its venue's area, so events are never duplicated across cities.

    :param country_code: The RA country code, e.g. "ch".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :param save_raw: Also save each festival's full API response as JSON.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    _, areas = client.get_country_areas(country_code)
    areas_by_id = {area["id"]: area for area in areas}

    roots = root_areas(areas)
    owns_progress = runs.set_total(len(roots), "area", once=True)
    events_by_folder = defaultdict(dict)
    for done, root in enumerate(roots, start=1):
        for event in client.get_events(root["id"], start_date, end_date, f"{country_code.lower()} {root['name']}"):
            venue_area = ((event.get("venue") or {}).get("area") or {})
            area = areas_by_id.get(venue_area.get("id"), root)
            events_by_folder[area_folder(area)].setdefault(event["id"], event)
        if owns_progress:
            runs.report(progress=done)

    paths = []
    for folder, events in sorted(events_by_folder.items()):
        paths.append(write_city(events.values(), data_dir, country_code, folder, start_date, end_date, save_raw))
    if not paths:
        logger.info("%s: no events between %s and %s", country_code.lower(), start_date, end_date)
    return paths


def scrape_continent(continent, start_date, end_date, data_dir, client=None, save_raw=False):
    """
    Scrape every RA country in a continent. A country that fails is logged and skipped.

    :param continent: One of ra.areas.CONTINENTS, e.g. "europe".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :param save_raw: Also save each festival's full API response as JSON.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    paths = []
    countries = countries_in_continent(client.get_countries(), continent)
    runs.set_total(len(countries), "country")
    for done, country in enumerate(progress(countries, desc=f"Scraping {continent}", unit="country"), start=1):
        runs.report(progress=done)
        try:
            paths.extend(scrape_country(country["urlCode"], start_date, end_date, data_dir, client, save_raw))
        except (RuntimeError, ValueError) as error:
            logger.error("%s (%s) failed: %s", country["name"], country["urlCode"], error)
    return paths
