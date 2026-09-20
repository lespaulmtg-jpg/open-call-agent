from datetime import date

import pytest

from open_call_agent.festivals import FESTIVAL_COLUMNS
from open_call_agent.open_call.batch import RESULT_COLUMNS
from open_call_agent.open_call.review import check_and_review_open_calls
from open_call_agent.paths import FESTIVALS_PREFIX, find_csvs
from open_call_agent.ra.events import read_rows, write_rows
from open_call_agent.summary import STATUS_RANK, build_country_rows, build_festival_rows, festival_key, write_summary

from .conftest import COUNTRY


@pytest.mark.network
def test_festivals_without_results_are_listed(swiss_data_dir):
    rows = build_festival_rows(find_csvs(swiss_data_dir, FESTIVALS_PREFIX, COUNTRY), None, {"ch": "Switzerland"})
    fog = next(row for row in rows if row["Festival"] == "FOG Festival 2026")
    assert fog["Open Call Status"] == "not_checked" and fog["Country"] == "Switzerland"
    assert {row["Open Call Status"] for row in rows} <= {"not_checked", "no_website"}
    assert len({row["Event ID"] for row in rows}) == len(rows)


@pytest.mark.network
@pytest.mark.llm
def test_swiss_festival_summary(swiss_data_dir, tmp_path):
    festival_paths = find_csvs(swiss_data_dir, FESTIVALS_PREFIX, COUNTRY)
    _, reviewed_path = check_and_review_open_calls(swiss_data_dir, country_code=COUNTRY, festivals_only=True,
                                                   ignore_robots=True)
    festival_path, country_path, markdown_path = write_summary(festival_paths, reviewed_path, tmp_path, "summary",
                                                               {"ch": "Switzerland"})
    festivals = read_rows(festival_path)
    listings = [row for path in festival_paths for row in read_rows(path)]
    assert len(festivals) == len({festival_key(row) for row in listings}), "one row per festival"

    fog = next(row for row in festivals if row["Festival"] == "FOG Festival 2026")
    assert fog["Open Call Status"] == "open"
    assert fog["Announcement URL"].rstrip("/").endswith("/proposals")
    assert "docs.google.com/forms" in fog["Open Call URL"]
    assert festivals[0]["Open Call Status"] == "open", "open calls should be listed first"
    assert all(row["Open Call Status"] == "no_website" for row in festivals if not row["Website"])
    assert [STATUS_RANK.index(row["Open Call Status"]) for row in festivals] == \
        sorted(STATUS_RANK.index(row["Open Call Status"]) for row in festivals)

    [switzerland, total] = read_rows(country_path)
    assert switzerland["Country"] == "Switzerland" and total["Country"] == "Total"
    assert int(total["Festivals"]) == len(festivals) and int(total["Open"]) >= 1
    assert "FOG Festival 2026" in markdown_path.read_text()


def festival(event_id, name, start, end, city="zurich", website="", instagram="", event_website=""):
    return {"Event ID": event_id, "Event name": name, "Start Time": f"{start}T12:00:00.000",
            "End Time": f"{end}T23:00:00.000", "Country": "ch", "City": city, "Venue": "Venue", "Promoters": "FOG",
            "Event Website": event_website, "Event Instagram": "",
            "Promoter Websites": website, "Promoter Instagrams": instagram, "Number of guests attending": "10",
            "Event URL": f"https://ra.co/events/{event_id}", "Duration Hours": "35"}


def test_a_link_check_that_overrides_the_models_is_shown(tmp_path):
    from open_call_agent.summary import status_reason

    assert status_reason({"status_reason": "Open call on 4 page(s); link check: the application form says it is closed",
                          "review_reason": "The page says applications are open."}) == \
           "Link check: the application form says it is closed | The page says applications are open."
    assert status_reason({"status_reason": "Open call on 1 page(s)", "review_reason": "Quotes verified."}) == \
           "Quotes verified."
    assert status_reason({"status_reason": "No call found"}) == "No call found"


def test_festival_editions_appear_once(tmp_path):
    festivals = [
        festival("1", "FOG Festival 2025", "2025-09-19", "2025-09-21", instagram="https://www.instagram.com/fog"),
        festival("2", "FOG festival 2026", "2026-09-18", "2026-09-20", website="https://fogstudio.ch"),
        festival("2", "FOG festival 2026", "2026-09-18", "2026-09-20", website="https://fogstudio.ch"),
        festival("3", "FOG Festival", "2026-09-18", "2026-09-20", city="basel"),
    ]
    zurich, basel = tmp_path / "zurich.csv", tmp_path / "basel.csv"
    write_rows(festivals[:2], zurich, FESTIVAL_COLUMNS)
    write_rows(festivals[2:], basel, FESTIVAL_COLUMNS)
    results = tmp_path / "results.csv"
    write_rows([{column: "" for column in RESULT_COLUMNS} | {"website": "https://fogstudio.ch", "status": "open"}],
               results, RESULT_COLUMNS)

    rows = build_festival_rows([zurich, basel], results, today=date(2026, 1, 1))
    assert len(rows) == 2, "editions and duplicate listings in one city are one festival; other cities stay separate"
    fog = next(row for row in rows if row["City"] == "zurich")
    assert fog["Editions"] == 2 and fog["Event ID"] == "2" and fog["Start Date"] == "2026-09-18"
    assert fog["Edition Dates"] == "2025-09-19 to 2025-09-21 | 2026-09-18 to 2026-09-20"
    assert fog["Open Call Status"] == "open", "a website listed on any edition counts"
    assert fog["Instagram"] == "https://www.instagram.com/fog"

    after_all = build_festival_rows([zurich], results, today=date(2027, 1, 1))
    assert after_all[0]["Event ID"] == "2", "the latest edition is shown once all are over"


def test_festival_website_decides_the_result(tmp_path):
    """
    A promoter website can cover several festivals (detectclassic.com covers Between Mountains and the German Detect
    Classic Festival), so the festival's own website leads and decides its status.
    """
    path = tmp_path / "basel.csv"
    write_rows([festival("1", "Between Mountains 2026", "2026-09-04", "2026-09-06", city="basel",
                         website="https://www.detectclassic.com", event_website="http://betweenmountains.ch/")],
               path, FESTIVAL_COLUMNS)
    results = tmp_path / "results.csv"

    def write_results(rows):
        write_rows([{column: "" for column in RESULT_COLUMNS} | row for row in rows], results, RESULT_COLUMNS)

    write_results([{"website": "https://www.detectclassic.com", "status": "open", "deadline": "2026-11-15"}])
    [row] = build_festival_rows([path], results)
    assert row["Website"] == "http://betweenmountains.ch/, https://www.detectclassic.com"
    assert row["Open Call Status"] == "open", "the promoter's result stands in until the festival's site is checked"

    write_results([{"website": "https://www.detectclassic.com", "status": "open", "deadline": "2026-11-15"},
                   {"website": "http://betweenmountains.ch/", "status": "none", "pages_visited": "12"}])
    [row] = build_festival_rows([path], results)
    assert row["Open Call Status"] == "none", "the festival's own website decides once it has been checked"

    # betweenmountains.ch is a one-page holding site: "no call found" there says nothing about the festival
    write_results([{"website": "https://www.detectclassic.com", "status": "open", "deadline": "2026-11-15"},
                   {"website": "http://betweenmountains.ch/", "status": "none", "pages_visited": "1"}])
    [row] = build_festival_rows([path], results)
    assert row["Open Call Status"] == "open", "a call found elsewhere outranks a crawl that read almost nothing"
