"""
Aggregate festivals and their open call results into summary tables.

- One row per festival (RA event lasting more than 24 hours), merging its editions and duplicate listings, joined with
  the open call result of its promoters' websites. A festival with several websites takes the most positive result:
  open, needs_review, closed, none.
  Festivals without a promoter website are "no_website"; websites missing from the results are "not_checked".
- One row per country with counts per status.
- A Markdown overview with the country table and every open call.
"""
import logging
import re
from collections import Counter, defaultdict
from datetime import date

from .open_call.batch import website_key
from .ra.events import read_rows, split_links, write_rows

logger = logging.getLogger(__name__)

YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NON_WORD_RE = re.compile(r"[\W_]+")

STATUS_RANK = ["open", "needs_review", "closed", "none", "not_checked", "no_website"]

FESTIVAL_SUMMARY_COLUMNS = [
    "Open Call Status", "Deadline", "Country", "City", "Festival", "Start Date", "End Date", "Duration Hours", "Editions",
    "Edition Dates", "Venue", "Promoters", "Website", "Open Call URL", "Announcement URL", "Opportunity Types",
    "How To Apply", "Contact Emails", "Instagram", "Status Reason", "Small Model Status", "Large Model Status",
    "Link Checks", "Attending", "Event URL", "Event URLs", "Event ID", "Country Code",
]
COUNTRY_SUMMARY_COLUMNS = ["Country", "Country Code", "Festivals", "With Website", "Open", "Needs Review", "Closed",
                           "None", "Not Checked", "No Website"]


def best_result(results):
    """
    :param results: Open call result rows for one festival's websites.
    :return: The most positive result row.
    """
    return min(results, key=lambda row: STATUS_RANK.index(row["status"]) if row["status"] in STATUS_RANK else 3)


def link_check_summary(result):
    """
    :param result: An open call result row.
    :return: Link check problems, e.g. "open_call_url: broken (404)", or an empty string when all links are fine.
    """
    problems = []
    for column in ("website_check", "announcement_url_check", "open_call_url_check", "email_check"):
        for value in filter(None, (result.get(column) or "").split(" | ")):
            if not value.startswith(("ok", "unverifiable")):
                problems.append(f"{column.removesuffix('_check')}: {value}")
    return " | ".join(dict.fromkeys(problems))


def festival_key(festival):
    """
    Identify a festival across its editions and duplicate listings: same country, city and name, ignoring years, case,
    punctuation and spacing, e.g. "FOG Festival 2025" and "FOG festival 2026" in Zurich.

    :param festival: A festivals CSV row.
    :return: A tuple key, or the event ID for names that are only a year or punctuation.
    """
    name = YEAR_RE.sub(" ", festival["Event name"].casefold())
    name = " ".join(NON_WORD_RE.sub(" ", name).split())
    return (festival["Country"], festival["City"], name) if name else (festival["Event ID"],)


def main_edition(editions, today=None):
    """
    :param editions: Festival rows of one festival.
    :return: The next edition that hasn't ended yet, or the latest one if all are over.
    """
    today = (today or date.today()).isoformat()
    editions = sorted(editions, key=lambda row: row["Start Time"])
    return next((row for row in editions if row["End Time"][:10] >= today), editions[-1])


def join_unique(values, separator=", "):
    """
    :param values: Strings, each possibly holding several items separated by separator.
    :return: The unique non-empty items, in order, joined by separator.
    """
    items = (item.strip() for value in values for item in (value or "").split(separator))
    return separator.join(dict.fromkeys(item for item in items if item))


def build_festival_rows(festival_paths, results_path, country_names=None, today=None):
    """
    Join festivals with open call results, one row per festival.

    Editions and duplicate listings of the same festival (see festival_key) are merged: dates, venue and event URL come
    from the next edition (or the latest), and websites, Instagrams and promoters from all editions.

    :param festival_paths: Festival CSV Paths.
    :param results_path: A (reviewed) open call results CSV Path, or None if no checks have run.
    :param country_names: Optional dict of lower-case country code -> name.
    :param today: Optional date deciding which edition is next, default today.
    :return: A list of summary rows keyed by FESTIVAL_SUMMARY_COLUMNS, sorted by status, deadline, country and start.
    """
    results = {website_key(row["website"]): row for row in read_rows(results_path)} if results_path else {}
    country_names = country_names or {}
    festivals = defaultdict(dict)
    for path in festival_paths:
        for row in read_rows(path):
            festivals[festival_key(row)].setdefault(row["Event ID"], row)

    summary = []
    for editions in festivals.values():
        editions = sorted(editions.values(), key=lambda row: row["Start Time"])
        festival = main_edition(editions, today)
        websites = split_links(join_unique(row["Promoter Websites"] for row in editions))
        found = [results[website_key(website)] for website in websites if website_key(website) in results]
        if not websites:
            status, result = "no_website", {}
        elif not found:
            status, result = "not_checked", {}
        else:
            result = best_result(found)
            status = result["status"]

        summary.append({
            "Open Call Status": status,
            "Deadline": result.get("deadline", ""),
            "Country": country_names.get(festival["Country"], festival["Country"].upper()),
            "Country Code": festival["Country"],
            "City": festival["City"],
            "Festival": festival["Event name"],
            "Start Date": festival["Start Time"][:10],
            "End Date": festival["End Time"][:10],
            "Duration Hours": festival.get("Duration Hours", ""),
            "Editions": len(editions),
            "Edition Dates": join_unique((f"{row['Start Time'][:10]} to {row['End Time'][:10]}" for row in editions),
                                         " | "),
            "Venue": festival["Venue"],
            "Promoters": join_unique(row["Promoters"] for row in editions),
            "Website": ", ".join(websites),
            "Open Call URL": result.get("open_call_url", ""),
            "Announcement URL": result.get("announcement_url", ""),
            "Opportunity Types": result.get("opportunity_types", ""),
            "How To Apply": result.get("how_to_apply", ""),
            "Contact Emails": result.get("contact_emails", ""),
            "Instagram": join_unique(row["Promoter Instagrams"] for row in editions),
            "Status Reason": result.get("review_reason") or result.get("status_reason", ""),
            "Small Model Status": result.get("small_model_status") or result.get("status", ""),
            "Large Model Status": result.get("large_model_status", ""),
            "Link Checks": link_check_summary(result) if result else "",
            "Attending": festival["Number of guests attending"],
            "Event URL": festival["Event URL"],
            "Event URLs": join_unique((row["Event URL"] for row in editions), " | "),
            "Event ID": festival["Event ID"],
        })

    summary.sort(key=lambda row: (STATUS_RANK.index(row["Open Call Status"]), row["Deadline"] or "9999",
                                  row["Country"], row["Start Date"]))
    return summary


def build_country_rows(festival_rows):
    """
    :param festival_rows: Rows from build_festival_rows.
    :return: One row per country keyed by COUNTRY_SUMMARY_COLUMNS, most open calls first, plus a "Total" row.
    """
    by_country = defaultdict(Counter)
    names = {}
    for row in festival_rows:
        counts = by_country[row["Country Code"]]
        names[row["Country Code"]] = row["Country"]
        counts["Festivals"] += 1
        counts["With Website"] += row["Open Call Status"] != "no_website"
        counts[row["Open Call Status"]] += 1

    def country_row(name, code, counts):
        return {"Country": name, "Country Code": code, "Festivals": counts["Festivals"],
                "With Website": counts["With Website"], "Open": counts["open"], "Needs Review": counts["needs_review"],
                "Closed": counts["closed"], "None": counts["none"], "Not Checked": counts["not_checked"],
                "No Website": counts["no_website"]}

    rows = [country_row(names[code], code, counts) for code, counts in by_country.items()]
    rows.sort(key=lambda row: (-row["Open"], -row["Needs Review"], -row["Festivals"], row["Country"]))
    total = sum(by_country.values(), Counter())
    return rows + [country_row("Total", "", total)]


def dates(row):
    """
    :param row: A festival summary row.
    :return: The main edition's dates, plus the number of other editions, e.g. "2026-09-18 to 2026-09-20 (+1 edition)".
    """
    others = int(row["Editions"]) - 1
    return f"{row['Start Date']} to {row['End Date']}" + (f" (+{others} edition{'s' * (others > 1)})" if others else "")


def markdown_summary(title, festival_rows, country_rows):
    """
    :return: A Markdown overview: totals by country, then every open call and every festival needing review.
    """
    lines = [f"# {title}", "", "## By country", "",
             "| " + " | ".join(COUNTRY_SUMMARY_COLUMNS) + " |", "|" + "---|" * len(COUNTRY_SUMMARY_COLUMNS)]
    lines += ["| " + " | ".join(str(row[column]) for column in COUNTRY_SUMMARY_COLUMNS) + " |" for row in country_rows]

    for status, heading in (("open", "Open calls"), ("needs_review", "Needs review")):
        selected = [row for row in festival_rows if row["Open Call Status"] == status]
        lines += ["", f"## {heading} ({len(selected)})", "",
                  "| Deadline | Festival | Dates | Country | City | Types | Open call | Announcement |",
                  "|---|---|---|---|---|---|---|---|"]
        for row in selected:
            cells = [row["Deadline"], f"[{row['Festival']}]({row['Event URL']})", dates(row),
                     row["Country"], row["City"], row["Opportunity Types"], row["Open Call URL"], row["Announcement URL"]]
            lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def write_summary(festival_paths, results_path, output_dir, name, country_names=None, title="Festival open calls"):
    """
    Build and write the festival table, country table and Markdown overview.

    :param festival_paths: Festival CSV Paths.
    :param results_path: A (reviewed) open call results CSV Path, or None.
    :param output_dir: Directory for the outputs.
    :param name: Base file name, e.g. "europe_festivals_summary_2026-09-17".
    :param country_names: Optional dict of lower-case country code -> name.
    :param title: Title of the Markdown overview.
    :return: A tuple of (festival CSV Path, country CSV Path, Markdown Path).
    """
    festival_rows = build_festival_rows(festival_paths, results_path, country_names)
    country_rows = build_country_rows(festival_rows)
    festival_path = output_dir / f"{name}.csv"
    country_path = output_dir / f"{name}_by_country.csv"
    markdown_path = output_dir / f"{name}.md"
    write_rows(festival_rows, festival_path, FESTIVAL_SUMMARY_COLUMNS)
    write_rows(country_rows, country_path, COUNTRY_SUMMARY_COLUMNS)
    markdown_path.write_text(markdown_summary(title, festival_rows, country_rows), encoding="utf-8")
    logger.info("Summary: %d festivals -> %s, %s, %s", len(festival_rows), festival_path, country_path, markdown_path)
    return festival_path, country_path, markdown_path
