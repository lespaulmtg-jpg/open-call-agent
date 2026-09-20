"""
Add the "Event Website" and "Event Instagram" columns to CSVs scraped before those columns existed.

Links are read from a saved raw response when a scrape used --save-raw, and asked of RA otherwise.

An event's own links (RA's promotionalLinks, shown on the event page) are more specific than its promoters' websites:
Between Mountains links to betweenmountains.ch, while its promoter's website, detectclassic.com, covers several
festivals. Rescraping everything would take hours; asking RA only for the links takes about a minute per 10,000 events.

CSVs that already have the columns are skipped, so a stopped run can simply be started again.

Usage:
    python scripts/backfill_event_links.py                  # every scraped CSV
    python scripts/backfill_event_links.py --country ch     # one country
    python scripts/backfill_event_links.py --festivals-only # only the festival CSVs
"""
import argparse
import logging
import sys

from open_call_agent.paths import DEFAULT_DATA_DIR, EVENTS_PREFIX, FESTIVALS_PREFIX, find_csvs, raw_event_path
from open_call_agent.progress import progress, progress_logging
from open_call_agent.ra.client import RAClient
from open_call_agent.ra.events import CSV_COLUMNS, get_event_instagram, get_event_website, read_rows, write_rows
from open_call_agent.storage import read_json, stop_on_sigterm

logger = logging.getLogger("backfill_event_links")

BATCH_SIZE = 100
NEW_COLUMNS = ["Event Website", "Event Instagram"]


def event_links(event):
    """
    :param event: An event from the RA API.
    :return: The columns to fill in for it.
    """
    return {"Event Website": get_event_website(event) or "",
            "Event Instagram": get_event_instagram(event) or ""}


def saved_links(data_dir, country_code, city, event_ids):
    """
    Read the links from raw responses saved by a scrape with --save-raw, so RA isn't asked again for those events.

    :return: A dict of event ID -> columns, for the events that have a saved response.
    """
    saved = {}
    for event_id in event_ids:
        response = read_json(raw_event_path(data_dir, country_code, city, event_id))
        if response and response.get("event"):
            saved[event_id] = event_links(response["event"])
    return saved


def fetch_links(client, event_ids):
    """
    Ask RA for the promotional links of many events at once, as one aliased query per batch.

    :param client: An RAClient.
    :param event_ids: Event IDs.
    :return: A dict of event ID -> {"Event Website": url, "Event Instagram": url}.
    """
    links = {}
    if not event_ids:
        return links
    batches = [event_ids[start:start + BATCH_SIZE] for start in range(0, len(event_ids), BATCH_SIZE)]
    for batch in progress(batches, desc="Fetching event links", unit="batch"):
        # The title is needed too: a link is the festival's website when its own name matches the event's
        query = "query {" + " ".join(f'event{index}: event(id: "{event_id}") {{ id title promotionalLinks {{ title url }} }}'
                                     for index, event_id in enumerate(batch)) + "}"
        for event in client.query(query).values():
            # RA returns null for events that have been removed
            if event:
                links[event["id"]] = event_links(event)
    return links


def needs_backfill(path):
    """
    :param path: An events or festivals CSV Path.
    :return: True if the file doesn't have the new columns yet.
    """
    with open(path, encoding="utf-8") as file:
        header = file.readline()
    return not all(column in header for column in NEW_COLUMNS)


def backfill(data_dir, country_code=None, city=None, festivals_only=False, client=None, force=False):
    """
    Add the columns to every scraped CSV that doesn't have them.

    :param data_dir: The data directory.
    :param country_code: Optional RA country code.
    :param city: Optional city folder name.
    :param festivals_only: Only update festivals CSVs.
    :param client: Optional RAClient.
    :param force: Update CSVs that already have the columns, e.g. after the links are read differently.
    :return: The number of CSVs updated.
    """
    client = client or RAClient()
    prefixes = [FESTIVALS_PREFIX] if festivals_only else [EVENTS_PREFIX, FESTIVALS_PREFIX]
    paths = [path for prefix in prefixes for path in find_csvs(data_dir, prefix, country_code, city)]
    paths = [path for path in paths if force or needs_backfill(path)]
    if not paths:
        logger.info("Every CSV already has %s", " and ".join(NEW_COLUMNS))
        return 0

    updated = 0
    for path in progress(paths, desc="CSV files", unit="file"):
        rows = read_rows(path)
        country, city = path.parent.parent.name, path.parent.name
        links = saved_links(data_dir, country, city, [row["Event ID"] for row in rows])
        links.update(fetch_links(client, [row["Event ID"] for row in rows if row["Event ID"] not in links]))
        for row in rows:
            row.update(links.get(row["Event ID"], {column: "" for column in NEW_COLUMNS}))
        # Festival CSVs keep their extra Duration Hours column
        columns = CSV_COLUMNS + [column for column in rows[0] if column not in CSV_COLUMNS] if rows else CSV_COLUMNS
        write_rows(rows, path, columns)
        found = sum(bool(row["Event Website"]) for row in rows)
        logger.info("%s/%s: %d of %d events have their own website", country, city, found, len(rows))
        updated += 1
    return updated


def main():
    parser = argparse.ArgumentParser(description="Add the event website and Instagram columns to scraped CSVs.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--country", help="Limit to a country code.")
    parser.add_argument("--city", help="Limit to a city folder (requires --country).")
    parser.add_argument("--festivals-only", action="store_true", help="Only update the festival CSVs.")
    parser.add_argument("--force", action="store_true", help="Update CSVs that already have the columns.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop_on_sigterm()
    try:
        with progress_logging():
            updated = backfill(args.data_dir, args.country, args.city, args.festivals_only, force=args.force)
    except KeyboardInterrupt:
        print("Stopped. CSVs already updated are kept; run the same command again to continue.", file=sys.stderr)
        return 130
    print(f"Updated {updated} CSV files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
