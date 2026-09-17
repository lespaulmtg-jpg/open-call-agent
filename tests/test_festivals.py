import pytest

from open_call_agent.festivals import event_duration_hours
from open_call_agent.paths import EVENTS_PREFIX, FESTIVALS_PREFIX, festivals_csv_path, find_csvs
from open_call_agent.ra.events import read_rows

from .conftest import COUNTRY

pytestmark = pytest.mark.network


def test_festivals_are_exactly_the_events_longer_than_24_hours(swiss_data_dir):
    events_paths = find_csvs(swiss_data_dir, EVENTS_PREFIX, COUNTRY)
    for events_path in events_paths:
        festivals_path = festivals_csv_path(events_path)
        assert festivals_path.exists()
        expected = {row["Event ID"] for row in read_rows(events_path) if event_duration_hours(row) > 24}
        festivals = read_rows(festivals_path)
        assert {row["Event ID"] for row in festivals} == expected
        assert all(float(row["Duration Hours"]) > 24 for row in festivals)


def test_fog_festival_is_identified(swiss_data_dir):
    festivals = [row for path in find_csvs(swiss_data_dir, FESTIVALS_PREFIX, COUNTRY) for row in read_rows(path)]
    assert any(row["Event name"] == "FOG Festival 2026" for row in festivals)


def test_duration_handles_missing_times():
    assert event_duration_hours({"Start Time": "", "End Time": "2026-09-18T23:00:00.000"}) is None
    assert event_duration_hours({"Start Time": "2026-09-18T23:00:00.000", "End Time": "2026-09-20T01:00:00.000"}) == 26
