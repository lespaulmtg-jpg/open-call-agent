"""
Client for the ra.co GraphQL API: countries, areas and event listings.
"""
import logging
import time
from datetime import date, timedelta

import requests

from ..progress import progress

logger = logging.getLogger(__name__)

URL = "https://ra.co/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://ra.co/events/uk/london",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:106.0) Gecko/20100101 Firefox/106.0",
}
DELAY = 1  # Seconds between requests
MAX_RETRIES = 4
PAGE_SIZE = 100  # RA rejects larger pages
LISTINGS_LIMIT = 10000  # RA returns no more than this many listings per query, silently

COUNTRIES_QUERY = "{ countries { id name urlCode } }"

COUNTRY_AREAS_QUERY = """
query GET_COUNTRY_AREAS($urlCode: String) {
  country(urlCode: $urlCode) { id name urlCode areas { id name urlName parentId } }
}
"""

AREA_QUERY = """
query GET_AREA($areaUrlName: String, $countryUrlCode: String) {
  area(areaUrlName: $areaUrlName, countryUrlCode: $countryUrlCode) {
    id name urlName parentId country { name urlCode }
  }
}
"""

EVENT_LISTINGS_QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $pageSize: Int, $page: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
    totalResults
    data {
      id
      event {
        id title date startTime endTime contentUrl attending
        artists { id name }
        venue { id name area { id name urlName } }
        promoters { id name website instagram socialMediaLinks { link platform } }
      }
    }
  }
}
"""


class RAClient:
    """
    Minimal ra.co GraphQL client with rate limiting and retries.
    """

    def __init__(self, delay=DELAY):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self.last_request_time = 0

    def query(self, query, variables=None):
        """
        Run a GraphQL query, retrying rate limits, server errors and connection failures with backoff.

        :param query: The GraphQL query.
        :param variables: The query variables.
        :return: The "data" object of the response.
        :raises RuntimeError: If the query keeps failing or returns GraphQL errors.
        """
        for attempt in range(MAX_RETRIES + 1):
            elapsed = time.time() - self.last_request_time
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_request_time = time.time()

            try:
                response = self.session.post(URL, json={"query": query, "variables": variables or {}}, timeout=30)
            except requests.exceptions.RequestException as error:
                last_error = str(error)
            else:
                if response.status_code == 200:
                    body = response.json()
                    if body.get("errors"):
                        raise RuntimeError(f"RA GraphQL error: {body['errors'][0].get('message')}")
                    return body["data"]
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break

            wait = 2 ** attempt * 5
            logger.warning("RA request failed (%s), retrying in %ss", last_error, wait)
            time.sleep(wait)

        raise RuntimeError(f"RA request failed: {last_error}")

    def get_countries(self):
        """
        :return: A list of countries as dicts with id, name and urlCode.
        """
        return self.query(COUNTRIES_QUERY)["countries"]

    def get_country_areas(self, country_code):
        """
        :param country_code: The RA country code, e.g. "ch", "uk", "us".
        :return: A tuple of (country dict, list of area dicts with id, name, urlName and parentId).
        :raises ValueError: If the country doesn't exist.
        """
        countries = self.query(COUNTRY_AREAS_QUERY, {"urlCode": country_code.upper()})["country"]
        if not countries:
            raise ValueError(f"Unknown RA country code: {country_code}")
        return countries[0], countries[0]["areas"]

    def get_area(self, country_code, city):
        """
        Look up an area by country code and city.

        :param country_code: The RA country code, e.g. "ch".
        :param city: The city name or RA URL slug, e.g. "zurich" or "Los Angeles".
        :return: The area dict.
        :raises ValueError: If no matching area exists.
        """
        # RA URL slugs are lowercase with no spaces or hyphens, e.g. "losangeles"
        city_slug = city.lower().replace(" ", "").replace("-", "")
        area = self.query(AREA_QUERY, {"areaUrlName": city_slug, "countryUrlCode": country_code.lower()})["area"]
        if area is None:
            raise ValueError(f"No RA area found for country '{country_code}' and city '{city}'. "
                             f"Check the slug in the ra.co URL, e.g. ra.co/events/uk/london.")
        return area

    def get_events(self, area_id, start_date, end_date, description=None):
        """
        Fetch all events listed in an area between two dates, de-duplicated by event ID, showing a progress bar of
        listings fetched.

        :param area_id: The RA area ID.
        :param start_date: The start date (inclusive), format YYYY-MM-DD.
        :param end_date: The end date (inclusive), format YYYY-MM-DD.
        :param description: Optional progress bar label, e.g. "ch All".
        :return: A list of event dicts.
        """
        events = {}
        with progress(total=0, desc=description or f"Area {area_id}", unit="listing", leave=False) as bar:
            self._collect_events(area_id, date.fromisoformat(start_date), date.fromisoformat(end_date), events, bar)
        return list(events.values())

    def _collect_events(self, area_id, start, end, events, bar):
        """
        Page through listings into events, splitting the date range in half whenever it exceeds LISTINGS_LIMIT.

        :param area_id: The RA area ID.
        :param start: The start date (inclusive).
        :param end: The end date (inclusive).
        :param events: A dict of event ID -> event, updated in place.
        :param bar: The progress bar, advanced by listings fetched.
        """
        filters = {"areas": {"eq": int(area_id)},
                   "listingDate": {"gte": f"{start.isoformat()}T00:00:00.000Z", "lte": f"{end.isoformat()}T23:59:59.999Z"}}
        page = 1
        while True:
            listings = self.query(EVENT_LISTINGS_QUERY, {"filters": filters, "pageSize": PAGE_SIZE, "page": page})
            if page == 1 and listings["eventListings"]["totalResults"] >= LISTINGS_LIMIT:
                if start == end:
                    logger.warning("Area %s has more than %d listings on %s; some events are missing",
                                   area_id, LISTINGS_LIMIT, start)
                else:
                    middle = start + (end - start) // 2
                    logger.debug("Area %s %s..%s exceeds %d listings, splitting", area_id, start, end, LISTINGS_LIMIT)
                    self._collect_events(area_id, start, middle, events, bar)
                    self._collect_events(area_id, middle + timedelta(days=1), end, events, bar)
                    return
            if page == 1:
                bar.total += min(listings["eventListings"]["totalResults"], LISTINGS_LIMIT)
                bar.refresh()

            data = listings["eventListings"]["data"]
            if not data:
                return
            # Multi-day events are listed once per day, so the same event can appear on several pages
            for listing in data:
                events.setdefault(listing["event"]["id"], listing["event"])
            bar.update(len(data))
            bar.set_postfix(events=len(events), refresh=False)
            page += 1
