from datetime import date

import pytest
from bs4 import BeautifulSoup

from open_call_agent.open_call.checker import SiteChecker
from open_call_agent.open_call.dates import (MIN_PAGE_DATE, html_page_dates, http_last_modified, is_too_old,
                                             normalize_deadline, normalize_deadline_column)


@pytest.mark.parametrize("text, expected", [
    # Deadlines as the models extracted them from Swiss festival pages
    ("16.03.26", date(2026, 3, 16)),  # m4music.ch
    ("22.04.2026", date(2026, 4, 22)),  # m4music.ch
    ("June 12, 2026", date(2026, 6, 12)),  # schwingundstampf.ch
    ("mid-November 2026", date(2026, 11, 15)),  # fogstudio.ch Google Form
    ("2027-08-14", date(2027, 8, 14)),  # streetparade.com
    ("until 31 October 2026", date(2026, 10, 31)),
    ("31. Oktober 2026", date(2026, 10, 31)),
    ("Mitte Dezember 2026", date(2026, 12, 15)),
    ("fin novembre 2026", date(2026, 11, 30)),
    ("entro il 30 settembre 2026", date(2026, 9, 30)),
    ("November 2026", date(2026, 11, 30)),
    ("March 16, 2026 at 23:59", date(2026, 3, 16)),
    ("", None), ("none", None), ("2026", None),
])
def test_deadlines_are_normalized(text, expected):
    assert normalize_deadline(text) == expected


def test_deadline_column_uses_one_format():
    assert normalize_deadline_column("June 12, 2026 | 16.03.26 | 2026-06-12") == "2026-03-16 | 2026-06-12"
    assert normalize_deadline_column("") == ""


def test_most_recent_page_date_decides():
    # kaufleuten.ch homepage: published 2017, modified 2026
    soup = BeautifulSoup('<meta property="article:modified_time" content="2026-07-30T15:06:09+00:00">'
                         '<script type="application/ld+json">{"@graph": [{"datePublished": "2017-08-25T13:34:34+00:00",'
                         '"dateModified": "2026-07-30T15:06:09+00:00"}]}</script>', "lxml")
    dates = html_page_dates(soup)
    assert ("JSON-LD datePublished", date(2017, 8, 25)) in dates
    assert is_too_old(dates) is None

    # m4music.ch jobs page: <meta name="modification_date" content="08.09.2026">
    soup = BeautifulSoup('<meta name="modification_date" content="08.09.2026">', "lxml")
    assert html_page_dates(soup) == [("meta modification_date", date(2026, 9, 8))]

    old = [("JSON-LD datePublished", date(2017, 8, 25))]
    assert is_too_old(old) == old[0]
    assert is_too_old(old, None) is None  # filter disabled
    assert is_too_old([]) is None  # no date: processed
    assert http_last_modified({"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}) == [("Last-Modified header", date(2015, 10, 21))]


class FakeResponse:
    def __init__(self, url, text, status_code=200, content_type="text/html"):
        self.url, self.text, self.status_code = url, text, status_code
        self.content = text.encode()
        self.headers = {"Content-Type": content_type}


def fake_site(monkeypatch, sitemap, pages):
    """
    Serve a fake hiveclub.ch with a sitemap and pages, without network access.
    """
    def fetch_static(self, url):
        if url.endswith("/robots.txt"):
            return FakeResponse(url, "", 404)
        if url.endswith("/sitemap.xml"):
            return FakeResponse(url, sitemap, content_type="application/xml")
        path = url.split("hiveclub.ch", 1)[1] or "/"
        if path in pages:
            return FakeResponse(url, pages[path])
        return FakeResponse(url, "", 404)

    monkeypatch.setattr(SiteChecker, "fetch_static", fetch_static)
    monkeypatch.setattr(SiteChecker, "throttle", lambda self: None)


def page_html(body, meta=""):
    return f"<html><head><title>Hive</title>{meta}</head><body><p>{body}</p><p>{'Text ' * 100}{body}</p></body></html>"


def test_crawler_skips_pages_older_than_the_cutoff(monkeypatch):
    sitemap = """<urlset>
      <url><loc>https://www.hiveclub.ch/</loc><lastmod>2026-09-16</lastmod></url>
      <url><loc>https://www.hiveclub.ch/open-call-2019</loc><lastmod>2019-05-01</lastmod></url>
      <url><loc>https://www.hiveclub.ch/open-call-2026</loc><lastmod>2026-09-14</lastmod></url>
    </urlset>"""
    # Each page's text differs, otherwise repeated lines are removed as site-wide boilerplate
    call = "Open call {}: we are looking for DJs. Send us your proposal.".format
    fake_site(monkeypatch, sitemap, {
        "/": page_html('<a href="/open-call-2019">a</a> <a href="/open-call-2026">b</a> <a href="/undated-call">c</a> '
                       '<a href="/old-by-metadata">d</a>'),
        "/open-call-2019": page_html(call(2019)),
        "/open-call-2026": page_html(call(2026)),
        "/undated-call": page_html(call("undated")),
        "/old-by-metadata": page_html(call(2020), '<meta property="article:published_time" content="2020-02-02">'),
    })

    checker = SiteChecker("https://www.hiveclub.ch/", ignore_robots=True)
    selected = {page.url for page in checker.prepare()}

    skipped = {entry["url"]: entry for entry in checker.skipped_by_date}
    # Old by sitemap lastmod: never fetched
    assert "https://www.hiveclub.ch/open-call-2019" not in checker.pages
    assert skipped["https://www.hiveclub.ch/open-call-2019"] == {
        "url": "https://www.hiveclub.ch/open-call-2019", "date": "2019-05-01", "source": "sitemap lastmod"}
    # Old by page metadata: fetched (for links) but not classified
    assert skipped["https://www.hiveclub.ch/old-by-metadata"]["source"] == "meta article:published_time"
    assert "https://www.hiveclub.ch/old-by-metadata" not in selected
    # Recent and undated pages are processed
    assert {"https://www.hiveclub.ch/open-call-2026", "https://www.hiveclub.ch/undated-call"} <= selected

    # Disabling the filter processes everything
    checker = SiteChecker("https://www.hiveclub.ch/", ignore_robots=True, min_page_date=None)
    assert "https://www.hiveclub.ch/open-call-2019" in {page.url for page in checker.prepare()}


def test_identical_sitemap_dates_are_ignored(monkeypatch):
    urls = "".join(f"<url><loc>https://www.hiveclub.ch/page-{index}</loc><lastmod>2019-01-01</lastmod></url>"
                   for index in range(6))
    fake_site(monkeypatch, f"<urlset>{urls}</urlset>", {"/": page_html("Home")})
    checker = SiteChecker("https://www.hiveclub.ch/", ignore_robots=True)
    checker.discover_sitemap_urls()
    assert checker.sitemap_lastmod == {}
    assert MIN_PAGE_DATE == date(2025, 1, 1)
