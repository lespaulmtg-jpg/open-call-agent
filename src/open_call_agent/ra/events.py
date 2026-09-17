"""
Convert RA events to CSV rows, including cleaned promoter website and Instagram links.
"""
import csv
import io
from urllib.parse import urlparse

from ..storage import write_text_atomic

SOCIAL_MEDIA_DOMAINS = ("instagram.com", "facebook.com", "fb.com", "twitter.com", "x.com", "tiktok.com",
                        "soundcloud.com", "youtube.com")

CSV_COLUMNS = ["Event ID", "Event name", "Date", "Start Time", "End Time", "Artists", "Venue", "Country", "City",
               "Event URL", "Number of guests attending", "Promoters", "Promoter Websites", "Promoter Instagrams"]


def get_promoter_website(promoter):
    """
    Get a promoter's website URL, ignoring social media pages entered as a website.

    :param promoter: A promoter.
    :return: The website URL, or None if the promoter has no valid one.
    """
    website = (promoter.get("website") or "").strip()
    if not website:
        return None
    if "://" not in website:
        website = f"https://{website}"

    host = urlparse(website).netloc.lower()
    if not host or any(host == domain or host.endswith(f".{domain}") for domain in SOCIAL_MEDIA_DOMAINS):
        return None
    return website


def get_promoter_instagram(promoter):
    """
    Get a promoter's Instagram profile URL, which RA mostly stores in socialMediaLinks.

    :param promoter: A promoter.
    :return: The Instagram profile URL, or None if the promoter has no valid one.
    """
    links = [social_link["link"] for social_link in promoter.get("socialMediaLinks") or []
             if social_link["platform"] == "INSTAGRAM"]
    links.append(promoter.get("instagram"))

    for link in links:
        if not link:
            continue
        parsed = urlparse(link if "://" in link else f"https://{link}")
        handle = parsed.path.strip("/").split("/")[0]
        # Skip links without an account handle, e.g. "https://instagram.com/?hl=fr"
        if parsed.netloc.lower().endswith("instagram.com") and handle:
            return f"https://www.instagram.com/{handle}"
    return None


def format_links(links):
    """
    Join links as unique URLs only, skipping empty ones.

    :param links: A list of URLs or None values.
    :return: A string of unique URLs separated by ", ".
    """
    return ", ".join(dict.fromkeys(link for link in links if link))


def split_links(value):
    """
    Split a links column back into a list of URLs.

    :param value: A string of URLs separated by ", ".
    :return: A list of URLs.
    """
    return [link for link in (value or "").split(", ") if link]


def event_to_row(event, country_code, city):
    """
    Convert an RA event to a CSV row.

    :param event: The event dict from the RA API.
    :param country_code: The RA country code, e.g. "ch".
    :param city: The city folder name, e.g. "zurich".
    :return: A dict keyed by CSV_COLUMNS.
    """
    promoters = event.get("promoters") or []
    return {
        "Event ID": event["id"],
        "Event name": event["title"],
        "Date": event["date"],
        "Start Time": event["startTime"],
        "End Time": event["endTime"],
        "Artists": ", ".join(artist["name"] for artist in event.get("artists") or []),
        "Venue": (event.get("venue") or {}).get("name") or "",
        "Country": country_code.lower(),
        "City": city,
        "Event URL": f"https://ra.co{event['contentUrl']}",
        "Number of guests attending": event.get("attending") or 0,
        "Promoters": ", ".join(promoter["name"] for promoter in promoters),
        "Promoter Websites": format_links(get_promoter_website(promoter) for promoter in promoters),
        "Promoter Instagrams": format_links(get_promoter_instagram(promoter) for promoter in promoters),
    }


def write_rows(rows, path, columns=CSV_COLUMNS):
    """
    Write rows to a CSV file atomically, creating parent directories.

    :param rows: A list of dicts.
    :param path: The output Path.
    :param columns: The CSV columns.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    # Written in one step, so a stop mid-write never leaves a truncated CSV
    write_text_atomic(path, buffer.getvalue())


def read_rows(path):
    """
    Read a CSV file.

    :param path: The CSV Path.
    :return: A list of dicts.
    """
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))
