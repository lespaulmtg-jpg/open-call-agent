"""
The backfill script adds the event website and Instagram columns to CSVs scraped before those columns existed, reading
a saved raw response where there is one and asking RA only for the rest. No network: RA is replaced by a fake client.
"""
import sys
from pathlib import Path

import pytest

from open_call_agent.festivals import FESTIVAL_COLUMNS
from open_call_agent.paths import raw_event_path
from open_call_agent.ra.events import CSV_COLUMNS, read_rows, write_rows
from open_call_agent.storage import write_json_atomic

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from backfill_event_links import backfill, needs_backfill, saved_links  # noqa: E402

EVENTS = [
    {"Event ID": "1", "Event name": "FOG Festival 2026", "Start Time": "2026-09-17T20:00:00.000",
     "End Time": "2026-09-20T22:30:00.000", "Country": "ch", "City": "basel", "Venue": "Kaserne",
     "Promoters": "FOG Festival", "Promoter Websites": "https://fogstudio.ch", "Promoter Instagrams": "",
     "Event URL": "https://ra.co/events/1", "Number of guests attending": "5", "Duration Hours": "74.5"},
    {"Event ID": "2", "Event name": "Between Mountains 2026", "Start Time": "2026-09-04T18:00:00.000",
     "End Time": "2026-09-06T17:00:00.000", "Country": "ch", "City": "basel", "Venue": "Holdenweid",
     "Promoters": "Detect Sounds", "Promoter Websites": "https://www.detectclassic.com", "Promoter Instagrams": "",
     "Event URL": "https://ra.co/events/2", "Number of guests attending": "13", "Duration Hours": "47.0"},
]

RA_LINKS = {"2": [{"title": "Website", "url": "http://betweenmountains.ch/"},
                  {"title": "Instagram", "url": "https://www.instagram.com/between_mountains_festival/"},
                  {"title": "Ticket Shop", "url": "https://detectclassic.com/en/betweenmountains/shop"}]}


class FakeRA:
    """
    Answers the aliased event query from RA_LINKS and counts how many events were asked for.
    """

    def __init__(self):
        self.asked = []

    def query(self, query, variables=None):
        answer = {}
        for index, part in enumerate(query.split('event(id: "')[1:]):
            event_id = part.split('"')[0]
            self.asked.append(event_id)
            answer[f"event{index}"] = {"id": event_id, "title": f"Event {event_id}",
                                       "promotionalLinks": RA_LINKS.get(event_id)}
        return answer


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "scraped_data" / "ch" / "basel" / "festivals_2025-01-01_2026-12-31.csv"
    write_rows(EVENTS, path, [column for column in FESTIVAL_COLUMNS if column not in ("Event Website",
                                                                                     "Event Instagram")])
    return tmp_path


def test_backfill_adds_the_columns(data_dir):
    [path] = list((data_dir / "scraped_data").glob("*/*/*.csv"))
    assert needs_backfill(path)

    client = FakeRA()
    assert backfill(data_dir, client=client) == 1
    rows = {row["Event ID"]: row for row in read_rows(path)}
    assert rows["2"]["Event Website"] == "http://betweenmountains.ch/"
    assert rows["2"]["Event Instagram"] == "https://www.instagram.com/between_mountains_festival", "normalised"
    assert rows["1"]["Event Website"] == "", "an event without promotional links keeps the column empty"
    assert rows["2"]["Promoter Websites"] == "https://www.detectclassic.com", "the promoter's website is kept"
    assert rows["2"]["Duration Hours"] == "47.0", "a festival CSV keeps its own columns"
    assert set(client.asked) == {"1", "2"}

    # A second run skips the file, and --force does it again
    assert not needs_backfill(path)
    assert backfill(data_dir, client=FakeRA()) == 0
    assert backfill(data_dir, client=FakeRA(), force=True) == 1


def test_saved_response_is_used_instead_of_asking_ra(data_dir):
    write_json_atomic(raw_event_path(data_dir, "ch", "basel", "1"),
                      {"scraped_on": "2026-09-18", "source": "ra.co",
                       "event": {"id": "1", "title": "FOG Festival 2026",
                                 "promotionalLinks": [{"title": "FOG website", "url": "http://fogstudio.ch"}]}})

    assert saved_links(data_dir, "ch", "basel", ["1", "2"]) == {
        "1": {"Event Website": "http://fogstudio.ch", "Event Instagram": ""}}

    client = FakeRA()
    backfill(data_dir, client=client)
    rows = {row["Event ID"]: row for row in read_rows(list((data_dir / "scraped_data").glob("*/*/*.csv"))[0])}
    assert rows["1"]["Event Website"] == "http://fogstudio.ch"
    assert client.asked == ["2"], "only the event without a saved response is asked for"
