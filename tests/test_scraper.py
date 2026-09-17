import pytest

from open_call_agent.paths import EVENTS_PREFIX, find_csvs
from open_call_agent.ra.areas import COUNTRY_CONTINENTS, countries_in_continent, root_areas
from open_call_agent.ra.events import CSV_COLUMNS, read_rows, split_links
from open_call_agent.scraper import scrape_city

from .conftest import COUNTRY, END_DATE, START_DATE

pytestmark = pytest.mark.network


def test_switzerland_is_in_europe(ra_client):
    assert COUNTRY_CONTINENTS["CH"] == "europe"
    europe = countries_in_continent(ra_client.get_countries(), "europe")
    assert "CH" in {country["urlCode"] for country in europe}


def test_switzerland_root_area_is_all(ra_client):
    _, areas = ra_client.get_country_areas(COUNTRY)
    assert [area["name"] for area in root_areas(areas)] == ["All"]


def test_country_scrape_writes_one_csv_per_city(swiss_data_dir):
    paths = find_csvs(swiss_data_dir, EVENTS_PREFIX, COUNTRY)
    assert paths, "no Swiss event CSVs were written"

    event_ids = []
    for path in paths:
        assert path.name == f"events_{START_DATE}_{END_DATE}.csv"
        assert path.parent.parent.name == COUNTRY
        rows = read_rows(path)
        assert rows and list(rows[0].keys()) == CSV_COLUMNS
        assert {row["City"] for row in rows} == {path.parent.name}
        event_ids.extend(row["Event ID"] for row in rows)

    assert "zurich" in {path.parent.name for path in paths}
    assert len(event_ids) == len(set(event_ids)), "an event was saved in more than one city"


def test_promoter_links_are_urls_only(swiss_data_dir):
    for path in find_csvs(swiss_data_dir, EVENTS_PREFIX, COUNTRY):
        for row in read_rows(path):
            for website in split_links(row["Promoter Websites"]):
                assert website.startswith(("http://", "https://")) and "instagram.com" not in website
            for instagram in split_links(row["Promoter Instagrams"]):
                assert instagram.startswith("https://www.instagram.com/") and instagram.rstrip("/") != "https://www.instagram.com"


def test_city_scrape_matches_country_scrape(tmp_path, ra_client, swiss_data_dir):
    [city_path] = scrape_city(COUNTRY, "zurich", START_DATE, END_DATE, tmp_path, ra_client)
    [country_path] = find_csvs(swiss_data_dir, EVENTS_PREFIX, COUNTRY, "zurich")
    assert city_path == tmp_path / "scraped_data" / COUNTRY / "zurich" / f"events_{START_DATE}_{END_DATE}.csv"
    assert {row["Event ID"] for row in read_rows(city_path)} == {row["Event ID"] for row in read_rows(country_path)}


def test_unknown_city_raises(ra_client):
    with pytest.raises(ValueError):
        ra_client.get_area(COUNTRY, "nowhere")


def test_listing_limit_splits_date_range(monkeypatch, ra_client):
    from open_call_agent.ra import client as client_module

    expected = {event["id"] for event in ra_client.get_events(60, START_DATE, END_DATE)}
    # Force splitting: Switzerland has more than 5 listings in the test dates
    monkeypatch.setattr(client_module, "LISTINGS_LIMIT", 5)
    split = {event["id"] for event in ra_client.get_events(60, START_DATE, END_DATE)}
    assert split == expected
