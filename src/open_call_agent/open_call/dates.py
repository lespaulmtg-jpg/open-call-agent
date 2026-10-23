"""
Dates: normalising deadlines to YYYY-MM-DD, and finding when a web page was last modified or created.

Pages whose most recent known date (sitemap lastmod, page metadata or the Last-Modified header) is before MIN_PAGE_DATE
are not processed. Pages without any date are processed. The most recent date is used, so a page published in 2017 but
modified in 2026 is still processed.
"""
import calendar
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

import dateparser

MIN_PAGE_DATE = date(2025, 1, 1)
DATE_FORMAT = "%Y-%m-%d"

# Metadata that states when a page was published or modified. Event dates (<time>, startDate) are deliberately excluded.
META_DATE_KEYS = re.compile(r"^(article:(published|modified)_time|og:updated_time|(date|last)[-_]?(published|modified|created)|"
                            r"(date|last)[-_]?updated|modification[-_]?date|creation[-_]?date|dc\.date(\.(created|modified))?|"
                            r"dcterms\.(created|modified)|revised|last-modified|date)$", re.IGNORECASE)
JSON_LD_DATE_KEYS = ("dateModified", "datePublished", "dateCreated")

# Approximate deadlines: "mid-November 2026", "Ende Oktober 2026", "fin novembre 2026", "early March 2027"
MID_RE = re.compile(r"\b(mid|middle of|mitte|mi-|mi |metà|meta|a metà)\b[-\s]*", re.IGNORECASE)
END_RE = re.compile(r"\b(end of|late|ende|fin( de| d')?|fine( di)?)\b[-\s]*", re.IGNORECASE)
START_RE = re.compile(r"\b(early|beginning of|start of|anfang|début( de| d')?|debut( de| d')?|inizio( di)?)\b[-\s]*", re.IGNORECASE)


def parse_date(text, prefer_future=False):
    """
    Parse a date written in any common format or language. Numeric dates are read day first (European order).

    :param text: The date text, e.g. "2026-07-30T15:06:09+00:00", "16.03.26", "June 12, 2026" or "31. Oktober 2026".
    :param prefer_future: Resolve dates without a year to the next occurrence.
    :return: A date, or None if the text isn't a date.
    """
    text = (text or "").strip()
    if not text:
        return None
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    short_year = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2})", text)
    try:
        if iso:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        if short_year:
            # "16.03.26": dateparser would resolve a two-digit year to 2126 when preferring future dates
            return date(2000 + int(short_year.group(3)), int(short_year.group(2)), int(short_year.group(1)))
    except ValueError:
        return None
    settings = {"DATE_ORDER": "DMY", "PREFER_DAY_OF_MONTH": "first", "RETURN_AS_TIMEZONE_AWARE": False}
    if prefer_future:
        settings["PREFER_DATES_FROM"] = "future"
    parsed = dateparser.parse(text, settings=settings)
    return parsed.date() if parsed else None


def normalize_deadline(text):
    """
    Normalise a deadline as written on a page to a date. Approximate deadlines are resolved to a day: "mid-" to the 15th,
    "end of" (or just a month and year) to the last day, "early" to the 1st.

    :param text: The deadline text, e.g. "16.03.26", "mid-November 2026" or "until 31 October 2026".
    :return: A date, or None if no deadline can be read.
    """
    text = re.sub(r"^\s*(until|by|before|deadline:?|bis( zum)?|jusqu'au|entro( il)?|avant le)\s+", "", text or "",
                  flags=re.IGNORECASE).strip()
    if not text or text.lower() in ("none", "n/a", "unknown", "closed", "open", "ongoing", "rolling"):
        return None

    day = None
    if MID_RE.search(text):
        day, text = 15, MID_RE.sub("", text)
    elif END_RE.search(text):
        day, text = "last", END_RE.sub("", text)
    elif START_RE.search(text):
        day, text = 1, START_RE.sub("", text)

    if re.fullmatch(r"\s*\d{4}\s*", text):
        return None  # A year alone isn't a deadline
    parsed = parse_date(text, prefer_future=True)
    if parsed is None:
        return None
    # A month and year without a day ("November 2026") is parsed as the 1st; treat it as the end of that month
    has_day = bool(re.search(r"(?<!\d)\d{1,2}(?!\d)", text))
    if day is None and not has_day:
        day = "last"
    if day == "last":
        return parsed.replace(day=calendar.monthrange(parsed.year, parsed.month)[1])
    if day is not None:
        return parsed.replace(day=day)
    return parsed


def format_date(value):
    """
    :param value: A date or None.
    :return: The date as YYYY-MM-DD, or an empty string.
    """
    return value.strftime(DATE_FORMAT) if value else ""


def normalize_deadline_column(value):
    """
    Normalise a deadline column value, which may hold several deadlines separated by " | ", to YYYY-MM-DD dates.

    :param value: The column value, e.g. "2026-11-15 | 16.03.26".
    :return: Unique YYYY-MM-DD dates, sorted and separated by " | ", or an empty string.
    """
    dates = {normalize_deadline(part) for part in (value or "").split(" | ")}
    return " | ".join(sorted(format_date(deadline) for deadline in dates if deadline))


def past_deadline(value, today=None):
    """
    Whether every deadline in a column has gone by. A call with several deadlines is still open while one of them is
    ahead, so only a column where all of them have passed counts as past.

    :param value: A deadline column value, which may hold several deadlines separated by " | ".
    :param today: The day to compare against, default today.
    :return: True if the column holds deadlines and all of them are in the past.
    """
    deadlines = [deadline for deadline in (normalize_deadline(part) for part in (value or "").split(" | ")) if deadline]
    return bool(deadlines) and all(deadline < (today or date.today()) for deadline in deadlines)


def html_page_dates(soup):
    """
    Find publication and modification dates in a page's metadata.

    :param soup: The page's BeautifulSoup.
    :return: A list of (source, date) tuples.
    """
    dates = []
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or meta.get("itemprop") or meta.get("http-equiv") or ""
        if META_DATE_KEYS.match(key.strip()) and meta.get("content"):
            parsed = parse_date(meta["content"])
            if parsed:
                dates.append((f"meta {key.strip()}", parsed))
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for key, value in json_ld_values(data):
            parsed = parse_date(value) if isinstance(value, str) else None
            if parsed:
                dates.append((f"JSON-LD {key}", parsed))
    return dates


def json_ld_values(data):
    """
    :param data: Parsed JSON-LD.
    :return: A generator of (key, value) for date keys at any depth.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if key in JSON_LD_DATE_KEYS:
                yield key, value
            else:
                yield from json_ld_values(value)
    elif isinstance(data, list):
        for item in data:
            yield from json_ld_values(item)


def http_last_modified(headers):
    """
    :param headers: HTTP response headers, or None.
    :return: A list with a ("Last-Modified header", date) tuple, or an empty list.
    """
    value = (headers or {}).get("Last-Modified")
    if not value:
        return []
    try:
        return [("Last-Modified header", parsedate_to_datetime(value).date())]
    except (TypeError, ValueError):
        return []


def is_too_old(dates, min_date=MIN_PAGE_DATE):
    """
    :param dates: (source, date) tuples for one page.
    :param min_date: The earliest date to process, or None to process every page.
    :return: A (source, date) tuple for the page's most recent date if it is before min_date, else None.
    """
    if not dates or min_date is None:
        return None
    newest = max(dates, key=lambda item: item[1])
    return newest if newest[1] < min_date else None


def parse_min_date(value):
    """
    :param value: A YYYY-MM-DD string, a date, or "none" to disable the filter.
    :return: A date or None.
    """
    if value is None or isinstance(value, date):
        return value
    if str(value).lower() == "none":
        return None
    return datetime.strptime(value, DATE_FORMAT).date()
