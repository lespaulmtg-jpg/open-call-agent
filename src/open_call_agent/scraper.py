"""
Scrape RA events by city, country or continent and save them as data/scraped_data/<country>/<city>/events_<start>_<end>.csv.
"""
import logging
from collections import defaultdict

from .paths import events_csv_path
from .progress import progress
from .ra.areas import area_folder, countries_in_continent, root_areas
from .ra.client import RAClient
from .ra.events import event_to_row, write_rows

logger = logging.getLogger(__name__)


def scrape_city(country_code, city, start_date, end_date, data_dir, client=None):
    """
    Scrape one city.

    :param country_code: The RA country code, e.g. "ch".
    :param city: The city name or RA URL slug, e.g. "zurich".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    area = client.get_area(country_code, city)
    folder = area_folder(area)
    events = client.get_events(area["id"], start_date, end_date, f"{country_code.lower()}/{folder}")
    path = events_csv_path(data_dir, country_code, folder, start_date, end_date)
    write_rows([event_to_row(event, country_code, folder) for event in events], path)
    logger.info("%s/%s: %d events -> %s", country_code.lower(), folder, len(events), path)
    return [path]


def scrape_country(country_code, start_date, end_date, data_dir, client=None):
    """
    Scrape a whole country, writing one CSV per city. Top-level areas are fetched once and each event is filed
    under its venue's area, so events are never duplicated across cities.

    :param country_code: The RA country code, e.g. "ch".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    _, areas = client.get_country_areas(country_code)
    areas_by_id = {area["id"]: area for area in areas}

    events_by_folder = defaultdict(dict)
    for root in root_areas(areas):
        for event in client.get_events(root["id"], start_date, end_date, f"{country_code.lower()} {root['name']}"):
            venue_area = ((event.get("venue") or {}).get("area") or {})
            area = areas_by_id.get(venue_area.get("id"), root)
            events_by_folder[area_folder(area)].setdefault(event["id"], event)

    paths = []
    for folder, events in sorted(events_by_folder.items()):
        path = events_csv_path(data_dir, country_code, folder, start_date, end_date)
        write_rows([event_to_row(event, country_code, folder) for event in events.values()], path)
        logger.info("%s/%s: %d events -> %s", country_code.lower(), folder, len(events), path)
        paths.append(path)
    if not paths:
        logger.info("%s: no events between %s and %s", country_code.lower(), start_date, end_date)
    return paths


def scrape_continent(continent, start_date, end_date, data_dir, client=None):
    """
    Scrape every RA country in a continent. A country that fails is logged and skipped.

    :param continent: One of ra.areas.CONTINENTS, e.g. "europe".
    :param start_date: The start date (inclusive), format YYYY-MM-DD.
    :param end_date: The end date (inclusive), format YYYY-MM-DD.
    :param data_dir: The data directory.
    :param client: An optional RAClient to reuse.
    :return: A list of written CSV Paths.
    """
    client = client or RAClient()
    paths = []
    for country in progress(countries_in_continent(client.get_countries(), continent), desc=f"Scraping {continent}",
                            unit="country"):
        try:
            paths.extend(scrape_country(country["urlCode"], start_date, end_date, data_dir, client))
        except (RuntimeError, ValueError) as error:
            logger.error("%s (%s) failed: %s", country["name"], country["urlCode"], error)
    return paths
