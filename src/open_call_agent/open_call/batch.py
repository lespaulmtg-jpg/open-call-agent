"""
Run the open call checker over every promoter website in scraped event (or festival) CSVs.

Sites are crawled in parallel and their pages classified on one shared model, loaded once: by default a llama-server
that batches concurrent requests. Results are written to a CSV one row per website as each site finishes, so an
interrupted run keeps its progress, and the full report for each site is saved as JSON.
"""
import csv
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from ..progress import progress
from .. import runs
from ..storage import read_json, write_json_atomic
from ..urls import website_key
from ..paths import (EVENTS_PREFIX, FESTIVALS_PREFIX, find_csvs, logs_dir, open_calls_dir, reports_dir,
                     safe_filename, scraped_dir)
from ..ra.events import read_rows, split_links
from .checker import SiteChecker
from .classifier import SMALL_MODEL, LlamaServer, LocalClassifier, ServerClassifier, resolve_model_path
from .dates import MIN_PAGE_DATE
from .links import check_links_in_csv

logger = logging.getLogger(__name__)

# A site's status, most positive first. deadline is YYYY-MM-DD (several separated by " | "), with the text as written
# on the page in deadline_text. announcement_url is the page announcing the call; open_call_url is the form on it.
STATUSES = ["open", "needs_review", "closed", "none"]
# The *_check columns hold link check results (see links.py), one per link in the matching column.
RESULT_COLUMNS = ["website", "status", "status_reason", "deadline", "deadline_text", "open_call_url", "announcement_url",
                  "opportunity_types", "how_to_apply", "contact_emails", "promoter_instagrams", "website_check",
                  "announcement_url_check", "open_call_url_check", "instagram_check", "email_check", "event_names",
                  "event_urls", "promoters", "countries", "cities", "pages_visited", "issues", "checked_on",
                  "report_path"]


def row_websites(row):
    """
    :param row: An events or festivals CSV row.
    :return: The event's own website first, then its promoters' websites, without duplicates.
    """
    websites = split_links(row.get("Event Website")) + split_links(row.get("Promoter Websites"))
    return list(dict.fromkeys(websites))


def collect_websites(csv_paths):
    """
    Collect unique websites from event CSVs, with the promoters, events and places they relate to.

    :param csv_paths: Paths of events or festivals CSVs.
    :return: A dict of website key -> site info dict, in first-seen order.
    """
    sites = {}
    for path in csv_paths:
        for row in read_rows(path):
            for website in row_websites(row):
                site = sites.setdefault(website_key(website), {
                    "website": website, "promoters": {}, "event_names": {}, "event_urls": {},
                    "promoter_instagrams": {}, "countries": {}, "cities": {}})
                # Dicts keep insertion order and de-duplicate
                for promoter in (row.get("Promoters") or "").split(", "):
                    if promoter:
                        site["promoters"][promoter] = None
                site["event_names"][row["Event name"]] = None
                site["event_urls"][row["Event URL"]] = None
                for instagram in split_links(row.get("Event Instagram")) + split_links(row.get("Promoter Instagrams")):
                    site["promoter_instagrams"][instagram] = None
                site["countries"][row["Country"]] = None
                site["cities"][row["City"]] = None
    return sites


def result_row(site, report, report_path):
    """
    Flatten a site report into an output CSV row. URLs and contact details are kept in separate columns.

    :param site: The site info dict from collect_websites.
    :param report: The SiteChecker report.
    :param report_path: The Path of the saved JSON report.
    :return: A dict keyed by RESULT_COLUMNS.
    """
    issues = report["warnings"] + [f"{error['stage']}: {error['url']}: {error['error']}" for error in report["errors"]]
    return {
        "website": site["website"],
        "status": report["status"],
        "status_reason": report["status_reason"],
        "deadline": report["deadline"],
        "deadline_text": report.get("deadline_text", ""),
        "open_call_url": ", ".join(report["open_call_urls"]),
        "announcement_url": ", ".join(report["announcement_urls"]),
        "opportunity_types": ", ".join(report["opportunity_types"]),
        "how_to_apply": report["how_to_apply"],
        "contact_emails": ", ".join(report["contact_emails"]),
        "promoter_instagrams": ", ".join(site["promoter_instagrams"]),
        "promoters": ", ".join(site["promoters"]),
        "event_names": " | ".join(site["event_names"]),
        "event_urls": ", ".join(site["event_urls"]),
        "countries": ", ".join(site["countries"]),
        "cities": ", ".join(site["cities"]),
        "pages_visited": report["coverage"]["pages_visited"],
        "issues": " | ".join(issues),
        "checked_on": report["checked_on"],
        "report_path": str(report_path),
    }


def prepare_site(website, max_pages=40, max_llm_pages=12, ignore_robots=False, min_page_date=MIN_PAGE_DATE):
    """
    Crawl a site and pick its pages to classify.

    :param website: The website URL.
    :param max_pages: Maximum pages to fetch.
    :param max_llm_pages: Maximum pages to classify.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param min_page_date: Don't process pages last modified or created before this date; None processes all.
    :return: A tuple of (prepared SiteChecker, crawl seconds).
    """
    started = time.perf_counter()
    checker = SiteChecker(website, None, max_pages, max_llm_pages, ignore_robots, min_page_date=min_page_date)
    checker.prepare()
    return checker, time.perf_counter() - started


def classify_prepared(checker, classifier, classify_pool):
    """
    Classify a prepared site's pages on a shared pool, so chunks from many sites are processed concurrently
    (and batched by llama-server), then build the report.

    :param checker: A SiteChecker after prepare().
    :param classifier: The shared classifier.
    :param classify_pool: A ThreadPoolExecutor sized to classifier.max_concurrency.
    :return: A tuple of (report, classify seconds).
    """
    started = time.perf_counter()
    checker.classifier = classifier
    futures = {page.url: [classify_pool.submit(checker.classify_chunk, page, chunk) for chunk in checker.chunk_texts(page)]
               for page in checker.selected_pages}
    pages = {page.url: page for page in checker.selected_pages}
    classifications = {url: checker.combine_chunk_results(pages[url], [future.result() for future in page_futures])
                       for url, page_futures in futures.items()}
    return checker.finish(classifications), time.perf_counter() - started


def check_sites(websites, classifier, crawl_workers=1, max_pages=40, max_llm_pages=12, ignore_robots=False,
                min_page_date=MIN_PAGE_DATE):
    """
    Check websites with parallel crawling and shared, concurrent classification.

    :param websites: Website URLs.
    :param classifier: A loaded classifier.
    :param crawl_workers: Number of sites crawled at the same time.
    :param max_pages: Maximum pages to fetch per site.
    :param max_llm_pages: Maximum pages to classify per site.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param min_page_date: Don't process pages last modified or created before this date; None processes all.
    :return: A generator of (website, report, crawl seconds, classify seconds) in completion order.
    """
    def check(website):
        try:
            checker, crawl_seconds = prepare_site(website, max_pages, max_llm_pages, ignore_robots, min_page_date)
            report, classify_seconds = classify_prepared(checker, classifier, classify_pool)
            return website, report, crawl_seconds, classify_seconds
        except Exception as error:  # One broken site must not stop the batch
            logger.exception("%s failed", website)
            return website, failed_report(website, error), 0, 0

    with ThreadPoolExecutor(classifier.max_concurrency, thread_name_prefix="classify") as classify_pool, \
            ThreadPoolExecutor(crawl_workers, thread_name_prefix="crawl") as crawl_pool:
        futures = [crawl_pool.submit(check, website) for website in websites]
        try:
            for future in as_completed(futures):
                yield future.result()
        finally:
            # When stopped early, don't start queued sites; only sites already in progress are waited for
            for future in futures:
                future.cancel()


def create_classifier(backend="server", model_path=None, llm_slots=4, constrained=False, log_dir=None):
    """
    Create a classifier.

    :param backend: "server" (llama-server with parallel slots and continuous batching) or "local" (in-process).
    :param model_path: Optional path to a GGUF model.
    :param llm_slots: Parallel sequences for the server backend.
    :param constrained: Always constrain local output with the JSON-schema grammar (the server always does).
    :param log_dir: Optional directory for the llama-server log.
    :return: A tuple of (classifier, cleanup function).
    """
    if backend == "local":
        classifier = LocalClassifier(model_path, constrained)
        return classifier, classifier.close
    log_path = Path(log_dir) / "llama-server.log" if log_dir else None
    server = LlamaServer(model_path, llm_slots, log_path)
    return ServerClassifier(server), server.close


def check_open_calls(data_dir, country_code=None, city=None, festivals_only=False, limit=None, ignore_robots=False,
                     model_path=None, max_pages=40, max_llm_pages=12, reuse_reports=True, output_path=None,
                     backend="server", llm_slots=4, crawl_workers=8, constrained=False, min_page_date=MIN_PAGE_DATE,
                     csv_paths=None, scope=None):
    """
    Check every promoter website found in existing CSVs for open calls. No scraping happens here.

    :param data_dir: The data directory.
    :param country_code: Optional RA country code to limit which CSVs are read.
    :param city: Optional city folder name to limit which CSVs are read.
    :param festivals_only: Read festivals CSVs instead of events CSVs (run identify_festivals first).
    :param limit: Optional maximum number of websites to check.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param model_path: Optional path to a GGUF model.
    :param max_pages: Maximum pages to fetch per site.
    :param max_llm_pages: Maximum pages to classify per site.
    :param reuse_reports: Don't check a site again if its saved report was made by the same model (on any day).
    :param output_path: Optional output CSV Path; defaults to data/open_calls/open_calls_<scope>_<date>.csv.
    :param backend: "server" (llama-server, continuous batching) or "local" (in-process, one call at a time).
    :param llm_slots: Parallel sequences for the server backend. 4 was fastest in benchmarks; 8 gave no gain.
    :param crawl_workers: Number of sites crawled at the same time. With 4 slots, 8 crawlers keep the model busy.
    :param constrained: Always constrain local output with the JSON-schema grammar (the server always does).
    :param min_page_date: Don't process pages last modified or created before this date (pages without a date are
        processed); None processes all.
    :param csv_paths: Optional explicit list of event or festival CSVs to read instead of searching by country and city.
    :param scope: Optional name for the output file, e.g. "europe_festivals"; defaults to the country, city and type.
    :return: The output CSV Path.
    :raises FileNotFoundError: If no matching CSVs exist.
    """
    data_dir = Path(data_dir)
    prefix = FESTIVALS_PREFIX if festivals_only else EVENTS_PREFIX
    csv_paths = list(csv_paths) if csv_paths is not None else find_csvs(data_dir, prefix, country_code, city)
    if not csv_paths:
        raise FileNotFoundError(f"No {prefix}*.csv files found in {scraped_dir(data_dir)} for country={country_code} city={city}. "
                                f"Run the scraper{' and festivals' if festivals_only else ''} first.")

    sites = list(collect_websites(csv_paths).values())[:limit]
    scope = scope or "_".join(part for part in [country_code, city, "festivals" if festivals_only else None] if part) or "all"
    output_path = Path(output_path or open_calls_dir(data_dir) / f"open_calls_{scope}_{date.today().isoformat()}.csv")
    reports = reports_dir(data_dir)
    reports.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("%d websites to check from %d CSV files", len(sites), len(csv_paths))
    runs.set_total(len(sites), "site")
    runs.report(websites=len(sites))

    def report_path_for(site):
        return reports / f"{safe_filename(website_key(site['website']))}.json"

    # Identifies the model without loading it, so sites it has already checked can be skipped
    model_name = Path(resolve_model_path(model_path, SMALL_MODEL)).name

    with open(output_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=RESULT_COLUMNS)
        writer.writeheader()

        def write(site, report):
            writer.writerow(result_row(site, report, report_path_for(site)))
            file.flush()

        to_check = []
        for site in sites:
            report_path = report_path_for(site)
            if reuse_reports and report_path.exists():
                saved = read_json(report_path) or {}
                if saved.get("model") == model_name:
                    logger.info("Already checked by %s on %s: %s", model_name, saved["checked_on"], site["website"])
                    write(site, saved)
                    runs.report(reused=1, **{saved["status"]: 1})
                    continue
            to_check.append(site)
        runs.report(progress=len(sites) - len(to_check))

        if to_check:
            # Loaded once, only when a site actually needs checking
            classifier, cleanup = create_classifier(backend, model_path, llm_slots, constrained, logs_dir(data_dir))
            sites_by_website = {site["website"]: site for site in to_check}
            results = check_sites(sites_by_website, classifier, crawl_workers, max_pages, max_llm_pages,
                                  ignore_robots, min_page_date)
            statuses = Counter()
            try:
                with progress(total=len(to_check), desc="Small model", unit="site") as bar:
                    for done, (website, report, crawl_seconds, classify_seconds) in enumerate(results, start=1):
                        statuses[report["status"]] += 1
                        bar.update(1)
                        bar.set_postfix(statuses, refresh=False)
                        site = sites_by_website[website]
                        # Only a site the model actually classified counts as checked; unreachable sites are retried
                        report["model"] = model_name if report["coverage"]["pages_classified"] else None
                        # Saved as soon as each site is classified: a stopped run keeps every finished site
                        write_json_atomic(report_path_for(site), report)
                        write(site, report)
                        logger.info("[%d/%d] %s: %s (crawl %.0fs, classify %.0fs)", done, len(to_check), website,
                                    report["status"], crawl_seconds, classify_seconds)
                        runs.report(progress=len(sites) - len(to_check) + done, checked=1, **{report["status"]: 1})
            finally:
                results.close()  # Cancel queued sites before the model server is stopped
                cleanup()

    # Sanity-check the links and emails the small model's results point to (network only; the model is unloaded)
    check_links_in_csv(output_path, RESULT_COLUMNS)
    logger.info("Open call results -> %s", output_path)
    return output_path


def failed_report(website, error):
    """
    Build a report for a site whose check crashed, so it shows up as needing review rather than missing.

    :param website: The website URL.
    :param error: The exception.
    :return: A report dict with the same shape as SiteChecker.build_report.
    """
    return {
        "site": website, "checked_on": date.today().isoformat(), "status": "needs_review",
        "status_reason": f"Site check crashed: {error!r}", "opportunity_types": [], "deadline": "",
        "deadline_text": "", "announcement_urls": [], "open_call_urls": [], "how_to_apply": "", "call_pages": [],
        "contact_emails": [],
        "warnings": [], "pages": [],
        "errors": [{"url": website, "stage": "crash", "error": repr(error)}],
        "coverage": {"discovery": None, "sitemap_urls_found": 0, "pages_visited": 0, "js_rendered_pages": 0,
                     "pages_classified": 0, "llm_used": False, "skipped_by_robots": [], "unchecked_documents": []},
    }
