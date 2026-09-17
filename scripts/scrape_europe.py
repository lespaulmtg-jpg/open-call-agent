"""
Scrape every RA country in a continent (default: Europe, 2025-01-01 to 2026-12-31), then identify festivals.

The range is scraped one country and one month at a time into data/scraped_data/.chunks/<YYYY-MM>/, with a marker
file per finished chunk, so an interrupted run resumes where it stopped. Chunks are then merged into one CSV per city,
de-duplicated by event ID (events spanning a month boundary appear in both months):

    data/scraped_data/<country>/<city>/events_<start>_<end>.csv
    data/scraped_data/<country>/<city>/festivals_<start>_<end>.csv

Usage:
    python scripts/scrape_europe.py
    python scripts/scrape_europe.py --start 2025-01-01 --end 2026-12-31 --continent europe --data-dir data
"""
import argparse
import logging
import sys
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from open_call_agent.festivals import identify_festivals
from open_call_agent.paths import SCRAPED_DIR, events_csv_path, logs_dir, scraped_dir
from open_call_agent.progress import progress, progress_logging
from open_call_agent.storage import stop_on_sigterm
from open_call_agent.ra.areas import CONTINENTS, countries_in_continent
from open_call_agent.ra.client import RAClient
from open_call_agent.ra.events import read_rows, write_rows
from open_call_agent.scraper import scrape_country

logger = logging.getLogger("scrape_europe")


def month_ranges(start, end):
    """
    Split a date range into calendar months, clipped to the range.

    :param start: The start date.
    :param end: The end date.
    :return: A list of (label, start date, end date) tuples, e.g. ("2025-01", 2025-01-01, 2025-01-31).
    """
    ranges = []
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        month_start = max(start, date(year, month, 1))
        month_end = min(end, date(year, month, monthrange(year, month)[1]))
        ranges.append((f"{year}-{month:02d}", month_start, month_end))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return ranges


def scrape_chunks(countries, months, chunks_dir, client):
    """
    Scrape each country and month into the chunks directory, skipping chunks already finished.

    :param countries: RA country dicts.
    :param months: Month ranges from month_ranges.
    :param chunks_dir: The chunks directory.
    :param client: An RAClient.
    :return: A list of (country code, month label, error) for chunks that failed.
    """
    failures = []
    total = len(countries) * len(months)
    done = 0
    bar = progress(total=total, desc="Country-months", unit="month")
    for country in countries:
        code = country["urlCode"].lower()
        for label, month_start, month_end in months:
            done += 1
            bar.update(1)
            bar.set_postfix_str(f"{country['name']} {label}", refresh=False)
            marker = chunks_dir / label / code / ".done"
            if marker.exists():
                continue
            try:
                paths = scrape_country(code, month_start.isoformat(), month_end.isoformat(), chunks_dir / label, client)
            except (RuntimeError, ValueError) as error:
                logger.error("[%d/%d] %s %s failed: %s", done, total, country["name"], label, error)
                failures.append((code, label, str(error)))
                continue
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            events = sum(len(read_rows(path)) for path in paths)
            logger.info("[%d/%d] %s %s: %d events in %d cities", done, total, country["name"], label, events, len(paths))
    bar.close()
    return failures


def merge_chunks(chunks_dir, data_dir, start, end):
    """
    Merge monthly chunk CSVs into one events CSV per city, de-duplicated by event ID.

    :param chunks_dir: The chunks directory.
    :param data_dir: The data directory.
    :param start: The start date.
    :param end: The end date.
    :return: A list of written events CSV Paths.
    """
    chunk_paths = defaultdict(list)
    # Each month is a data directory of its own: .chunks/<YYYY-MM>/scraped_data/<country>/<city>/events_*.csv
    for path in sorted(chunks_dir.glob(f"*/{SCRAPED_DIR}/*/*/events_*.csv")):
        country_code, city = path.parent.parent.name, path.parent.name
        chunk_paths[(country_code, city)].append(path)

    written = []
    for (country_code, city), paths in sorted(chunk_paths.items()):
        rows = {}
        for path in paths:
            for row in read_rows(path):
                rows.setdefault(row["Event ID"], row)
        output_path = events_csv_path(data_dir, country_code, city, start.isoformat(), end.isoformat())
        write_rows(sorted(rows.values(), key=lambda row: (row["Start Time"], row["Event ID"])), output_path)
        written.append(output_path)
    return written


def main():
    parser = argparse.ArgumentParser(description="Scrape a whole continent from RA, month by month, with resume.")
    parser.add_argument("--continent", default="europe", choices=CONTINENTS)
    parser.add_argument("--start", default="2025-01-01", type=date.fromisoformat, help="YYYY-MM-DD (default: 2025-01-01)")
    parser.add_argument("--end", default="2026-12-31", type=date.fromisoformat, help="YYYY-MM-DD (default: 2026-12-31)")
    parser.add_argument("--data-dir", default="data", type=Path)
    args = parser.parse_args()

    log_path = logs_dir(args.data_dir) / f"scrape_{args.continent}_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)])

    stop_on_sigterm()  # Finished country-months are kept; running the script again continues
    client = RAClient()
    countries = countries_in_continent(client.get_countries(), args.continent)
    months = month_ranges(args.start, args.end)
    chunks_dir = scraped_dir(args.data_dir) / ".chunks"
    logger.info("Scraping %d countries in %s x %d months (%s to %s). Log: %s",
                len(countries), args.continent, len(months), args.start, args.end, log_path)

    with progress_logging():
        failures = scrape_chunks(countries, months, chunks_dir, client)

    logger.info("Merging monthly chunks")
    written = merge_chunks(chunks_dir, args.data_dir, args.start, args.end)
    total_events = sum(len(read_rows(path)) for path in written)
    festival_paths = [path for events_path in written for path in
                      identify_festivals(args.data_dir, events_path.parent.parent.name, events_path.parent.name)]
    festivals = sum(len(read_rows(path)) for path in set(festival_paths))
    logger.info("Done: %d events and %d festivals in %d city CSVs", total_events, festivals, len(written))

    if failures:
        logger.error("%d country-months failed; rerun this script to retry them: %s", len(failures),
                     ", ".join(f"{code} {label}" for code, label, _ in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
