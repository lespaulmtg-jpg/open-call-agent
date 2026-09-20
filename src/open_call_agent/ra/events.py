"""
Convert RA events to CSV rows, including cleaned promoter website and Instagram links.
"""
import csv
import io
import re
from urllib.parse import urlparse

YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

from ..storage import write_text_atomic

SOCIAL_MEDIA_DOMAINS = ("instagram.com", "facebook.com", "fb.com", "twitter.com", "x.com", "tiktok.com",
                        "soundcloud.com", "youtube.com")

# Link hosts that are never a festival's own website: social media, music profiles and chat apps. Link hubs such as
# Linktree are kept, since they are how some festivals publish their own links. ra.co is the listing we came from.
NON_WEBSITE_DOMAINS = SOCIAL_MEDIA_DOMAINS + ("fb.me", "t.me", "telegram.me", "whatsapp.com", "discord.com",
                                              "discord.gg", "threads.net", "snapchat.com", "mixcloud.com",
                                              "spotify.com", "ra.co")
# Words that mark a link as tickets or merchandise rather than the festival's website, in its title or its host
TICKET_LINK_WORDS = ("ticket", "tix", "shop", "store", "kasse", "billet", "eintritt", "rsvp", "eventbrite", "dice.fm",
                     "seetickets", "sixtix", "bandcamp", "playlist")
# Title words that say a link is the festival's own page, e.g. "Website", "Web site", "Linktree", "all links"
WEBSITE_TITLE_WORDS = ("site", "web", "link")

CSV_COLUMNS = ["Event ID", "Event name", "Date", "Start Time", "End Time", "Artists", "Venue", "Country", "City",
               "Event URL", "Number of guests attending", "Promoters", "Event Website", "Event Instagram",
               "Promoter Websites", "Promoter Instagrams"]


def event_links(event):
    """
    :param event: The event dict from the RA API.
    :return: The event's promotional links as (title, URL) pairs, with a scheme added where RA stores the URL without.
    """
    links = []
    for link in event.get("promotionalLinks") or []:
        url = (link.get("url") or "").strip()
        if url:
            links.append(((link.get("title") or "").strip().lower(), url if "://" in url else f"https://{url}"))
    return links


def name_letters(text):
    """
    :param text: An event title or a host name.
    :return: Its letters and digits only, lower case and without years, e.g. "Garbicz Festival 2026" -> "garbiczfestival".
    """
    return re.sub(r"[^a-z0-9]", "", YEAR_RE.sub("", (text or "").lower()))


def host_name(host):
    """
    :param host: A link's host name.
    :return: Its name without "www." and without the ending, e.g. "www.garbiczfestival.com" -> "garbiczfestival".
    """
    return host.removeprefix("www.").rsplit(".", 1)[0]


def names_match(name, event_title):
    """
    Decide whether a link title or host is the event's own name, e.g. the link titled "Garbicz Festival" or the host
    garbiczfestival.com for the event "Garbicz Festival 2026".

    :param name: A link title or host name.
    :param event_title: The event title.
    :return: True if one name contains the other.
    """
    link_name = name_letters(name)
    event_name = name_letters(event_title)
    return len(link_name) >= 5 and len(event_name) >= 5 and (link_name in event_name or event_name in link_name)


def get_event_website(event):
    """
    Get an event's own website from the promotional links RA shows on the event page. These belong to the festival
    itself, while a promoter's website can cover several festivals, e.g. detectclassic.com for Between Mountains.

    Organisers write the link titles themselves, so links are ranked:
    1. The title says it is the site: it contains "site", "web" or "link" ("Website", "FOG website", "Linktree").
    2. The title or the host is the festival's own name ("Garbicz Festival" -> garbiczfestival.com).
    3. Anything else left over, in the order RA lists it.

    Social media, chat apps and ticket sellers never count, whichever rank they would land in.

    :param event: The event dict from the RA API.
    :return: The website URL, or None if the event has none.
    """
    candidates = []
    for position, (title, url) in enumerate(event_links(event)):
        host = urlparse(url).netloc.lower()
        if any(host == domain or host.endswith(f".{domain}") for domain in NON_WEBSITE_DOMAINS):
            continue
        if any(word in title or word in host for word in TICKET_LINK_WORDS):
            continue
        if any(word in title for word in WEBSITE_TITLE_WORDS):
            rank = 0
        elif names_match(title, event.get("title", "")) or names_match(host_name(host), event.get("title", "")):
            rank = 1
        else:
            rank = 2
        candidates.append((rank, position, url))

    url = min(candidates, default=(None, None, None))[2]
    # A link to the festival's own ticket page, e.g. szigetfestival.com/en/tickets, becomes the site itself
    if url and any(word in urlparse(url).path.lower() for word in TICKET_LINK_WORDS):
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


def get_event_instagram(event):
    """
    :param event: The event dict from the RA API.
    :return: The event's own Instagram profile URL, or None.
    """
    for _, url in event_links(event):
        parsed = urlparse(url)
        handle = parsed.path.strip("/").split("/")[0]
        if parsed.netloc.lower().endswith("instagram.com") and handle:
            return f"https://www.instagram.com/{handle}"
    return None


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
        "Event Website": get_event_website(event) or "",
        "Event Instagram": get_event_instagram(event) or "",
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
