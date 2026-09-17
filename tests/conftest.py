"""
Shared fixtures. Tests only use RA data for Switzerland between 2026-09-16 and 2026-09-18, scraped once per session.
"""
import pytest

from open_call_agent.festivals import identify_festivals
from open_call_agent.ra.client import RAClient
from open_call_agent.scraper import scrape_country

COUNTRY = "ch"
START_DATE = "2026-09-16"
END_DATE = "2026-09-18"


@pytest.fixture(scope="session")
def ra_client():
    return RAClient()


@pytest.fixture(scope="session")
def swiss_data_dir(tmp_path_factory, ra_client):
    """
    A data directory with Switzerland's events and festivals for the test dates.
    """
    data_dir = tmp_path_factory.mktemp("data")
    scrape_country(COUNTRY, START_DATE, END_DATE, data_dir, ra_client)
    identify_festivals(data_dir, COUNTRY)
    return data_dir
