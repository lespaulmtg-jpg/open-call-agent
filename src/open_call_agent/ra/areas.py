"""
Map RA countries to continents and pick the areas to scrape for a country.
"""
import json
from pathlib import Path

CONTINENTS = ["africa", "asia", "europe", "north_america", "oceania", "south_america"]

# RA country code -> continent. RA uses "UK" rather than the ISO code "GB", and "00" (Streamland) is online-only.
with open(Path(__file__).with_name("continents.json"), encoding="utf-8") as file:
    COUNTRY_CONTINENTS = json.load(file)


def countries_in_continent(countries, continent):
    """
    Filter RA countries to one continent.

    :param countries: RA country dicts with urlCode.
    :param continent: One of CONTINENTS.
    :return: A list of country dicts.
    :raises ValueError: If the continent is unknown.
    """
    if continent not in CONTINENTS:
        raise ValueError(f"Unknown continent '{continent}', expected one of: {', '.join(CONTINENTS)}")
    return [country for country in countries if COUNTRY_CONTINENTS.get(country["urlCode"].upper()) == continent]


def root_areas(areas):
    """
    Get a country's top-level areas. Their event listings include events in their child areas, so scraping the
    roots covers the whole country: e.g. Switzerland's "All" area, or each US state.

    :param areas: The country's area dicts with id and parentId.
    :return: The areas whose parent is not another area in the country.
    """
    area_ids = {area["id"] for area in areas}
    return [area for area in areas if area["parentId"] not in area_ids]


def area_folder(area):
    """
    Get the folder name for an area. Country-wide "All" areas hold events outside the listed cities.

    :param area: An area dict with name and urlName.
    :return: The folder name, e.g. "zurich" or "other".
    """
    return "other" if area["name"].lower() == "all" else area["urlName"]
