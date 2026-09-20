import pytest

from open_call_agent.festivals import event_duration_hours
from open_call_agent.paths import EVENTS_PREFIX, find_csvs, raw_events_dir
from open_call_agent.ra.areas import COUNTRY_CONTINENTS, countries_in_continent, root_areas
from open_call_agent.ra.events import (CSV_COLUMNS, get_event_instagram, get_event_website, read_rows, split_links)
from open_call_agent.storage import read_json
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


def test_event_website_is_picked_from_free_text_titles():
    """
    Organisers title their links themselves, so a link counts as the website when its title says "site" or its host is
    the event's own name. Tickets, social media and unrelated links are left out.
    """
    def website(title, links):
        return get_event_website({"title": title, "promotionalLinks": [{"title": t, "url": u} for t, u in links]})

    assert website("FOG Festival 2026", [("Instagram", "https://www.instagram.com/fog_festival/"),
                                         ("FOG website", "http://fogstudio.ch")]) == "http://fogstudio.ch"
    assert website("Balkan Goa Fanatics IV", [("Web site ", "https://balkangoafanatics.com"),
                                              ("Online tickets", "https://app.sixtix.com/e/x")]) == \
           "https://balkangoafanatics.com"
    assert website("Garbicz Festival 2026", [("Garbicz Festival", "https://garbiczfestival.com/")]) == \
           "https://garbiczfestival.com/", "the link's name is the festival's"
    assert website("Some Rave", [("Linktree", "https://linktr.ee/somerave")]) == "https://linktr.ee/somerave", \
        "a link hub is how some festivals publish their links"
    assert website("Sziget Festival 2025", [("Website", "https://szigetfestival.com/en/tickets")]) == \
           "https://szigetfestival.com", "a link into the festival's ticket page becomes the site itself"
    assert website("Grasgehren Open Air", [("WhatsApp", "https://chat.whatsapp.com/abc")]) is None
    assert website("Saturday: Kyle Elliott", [("Facebook", "https://fb.me/e/FjUELz9Sw")]) is None
    assert website("BREAKFLAMA", [("MÁS EVENTOS", "https://www.monsterticket.com/eventos?s=y")]) is None
    assert website("Frequencies", [("Südpol", "https://www.suedpol.org/")]) == "https://www.suedpol.org/", \
        "with nothing better, the remaining link is used"
    assert website("Klubnacht", [("Instagram", "https://www.instagram.com/berghain"),
                                 ("Billing", "https://www.berghain.berlin/de/event/79241/")]) == \
           "https://www.berghain.berlin/de/event/79241/", "social media never wins over another link"


def test_event_links_are_the_festivals_own(swiss_data_dir):
    """
    An event's promotional links on RA belong to the festival itself, unlike a promoter website that can cover several
    festivals, so they are stored separately.
    """
    rows = [row for path in find_csvs(swiss_data_dir, EVENTS_PREFIX, COUNTRY) for row in read_rows(path)]
    for row in rows:
        assert row["Event Website"] == "" or row["Event Website"].startswith(("http://", "https://"))
        assert row["Event Instagram"] == "" or row["Event Instagram"].startswith("https://www.instagram.com/")
    assert any(row["Event Website"] for row in rows), "some events link to their own website"


def test_event_instagram_is_the_festivals_own():
    def instagram(links):
        return get_event_instagram({"promotionalLinks": [{"title": t, "url": u} for t, u in links]})

    assert instagram([("Instagram", "https://www.instagram.com/fog_festival/")]) == \
           "https://www.instagram.com/fog_festival"
    assert instagram([("IG", "instagram.com/between_mountains_festival")]) == \
           "https://www.instagram.com/between_mountains_festival", "a title RA users write freely, and no scheme"
    assert instagram([("Instagram", "https://instagram.com/?hl=fr")]) is None, "no account handle"
    assert instagram([("Website", "https://fogstudio.ch")]) is None


def test_save_raw_keeps_the_full_response_for_festivals_only(tmp_path, ra_client):
    """
    With --save-raw the whole API response of each festival is kept as JSON, apart from the CSVs, so a column added
    later can be filled from disk instead of asking RA again.
    """
    scrape_city(COUNTRY, "basel", START_DATE, END_DATE, tmp_path, ra_client, save_raw=True)
    events = read_rows(find_csvs(tmp_path, EVENTS_PREFIX, COUNTRY, "basel")[0])
    festivals = [row for row in events if (event_duration_hours(row) or 0) > 24]
    saved = sorted(raw_events_dir(tmp_path).glob("*/*/*.json"))

    assert festivals, "the test dates include a festival in Basel"
    assert [path.stem for path in saved] == [row["Event ID"] for row in festivals], "festivals only, one file each"
    response = read_json(saved[0])
    assert response["source"] == "ra.co" and response["scraped_on"]
    assert response["event"]["id"] == festivals[0]["Event ID"]
    assert "promoters" in response["event"] and "artists" in response["event"], "the whole event, not our columns"


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
