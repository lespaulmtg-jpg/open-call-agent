"""
Check every European festival in the scraped data for open calls, then write summary tables.

1. Collect the festival CSVs for one scrape period (default 2025-01-01_2026-12-31) in European countries.
2. Small model (Qwen2.5-3B) checks every unique promoter website; link checks follow.
3. Large model (Qwen2.5-7B) reviews the websites marked open or needs_review. Only one model is on the GPU at a time.
4. Summary tables join every festival with its websites' results.

Websites already checked or reviewed by the same model are skipped, so an interrupted run can simply be started again.

Outputs in data/open_calls/:
    open_calls_europe_festivals_<period>_<date>.csv            small model results per website
    open_calls_europe_festivals_<period>_<date>_reviewed.csv   final results per website
    summary/europe_festivals_<period>_<date>.csv               one row per festival
    summary/europe_festivals_<period>_<date>_by_country.csv    counts per country
    summary/europe_festivals_<period>_<date>.md                country table, open calls and festivals to review

Usage:
    python scripts/check_europe_festivals.py
    python scripts/check_europe_festivals.py --limit 20            # try it on 20 websites first
    python scripts/check_europe_festivals.py --summary-only        # rebuild the tables from the latest results
"""
import argparse
import logging
import sys
from datetime import date, datetime

from open_call_agent.open_call.batch import check_open_calls
from open_call_agent.open_call.dates import parse_min_date
from open_call_agent.open_call.review import review_open_calls
from open_call_agent.paths import FESTIVALS_PREFIX, logs_dir, open_calls_dir, scraped_dir, summary_dir
from open_call_agent.progress import progress_logging
from open_call_agent import runs
from open_call_agent.storage import stop_on_sigterm
from open_call_agent.ra.areas import CONTINENTS, COUNTRY_CONTINENTS
from open_call_agent.ra.client import RAClient
from open_call_agent.summary import write_summary

logger = logging.getLogger("check_europe_festivals")


def festival_csvs(data_dir, continent, period):
    """
    :return: Festival CSV Paths for one scrape period in one continent's countries.
    """
    paths = sorted(scraped_dir(data_dir).glob(f"*/*/{FESTIVALS_PREFIX}{period}.csv"))
    return [path for path in paths if COUNTRY_CONTINENTS.get(path.parent.parent.name.upper()) == continent]


def country_names():
    """
    :return: A dict of lower-case RA country code -> country name, or an empty dict if RA can't be reached.
    """
    try:
        return {country["urlCode"].lower(): country["name"] for country in RAClient().get_countries()}
    except Exception as error:
        logger.warning("Could not load country names from RA, using codes: %s", error)
        return {}


def latest_results(data_dir, scope):
    """
    :return: The most recent reviewed results CSV for scope, else the most recent small model results, else None.
    """
    directory = open_calls_dir(data_dir)
    reviewed = list(directory.glob(f"open_calls_{scope}_*_reviewed.csv"))
    first_pass = [path for path in directory.glob(f"open_calls_{scope}_*.csv") if not path.stem.endswith("_reviewed")]
    # Prefer a review of the latest small model results; a review older than them is out of date
    latest_first_pass = max(first_pass, key=lambda path: path.stat().st_mtime, default=None)
    latest_reviewed = max(reviewed, key=lambda path: path.stat().st_mtime, default=None)
    if latest_reviewed and (not latest_first_pass or latest_reviewed.stat().st_mtime >= latest_first_pass.stat().st_mtime):
        return latest_reviewed
    return latest_first_pass


def run_checks(args, paths, scope):
    """
    Run the small model check and the large model review, or find the latest results with --summary-only.

    :return: The results CSV Path to summarise, or None.
    """
    if args.summary_only:
        results_path = latest_results(args.data_dir, scope)
        logger.info("Summarising %s", results_path or "festivals without any open call results")
        return results_path
    results_path = check_open_calls(args.data_dir, festivals_only=True, limit=args.limit,
                                    ignore_robots=args.ignore_robots, reuse_reports=not args.no_reuse,
                                    crawl_workers=args.crawl_workers, llm_slots=args.llm_slots,
                                    min_page_date=args.min_page_date, csv_paths=paths, scope=scope)
    if not args.no_review:
        # check_open_calls has stopped the small model, so the large model has the GPU to itself
        results_path = review_open_calls(results_path, llm_slots=args.review_slots, ignore_robots=args.ignore_robots,
                                         log_dir=logs_dir(args.data_dir), min_page_date=args.min_page_date,
                                         reuse_reviews=not args.no_reuse, data_dir=args.data_dir)
    return results_path


def main():
    parser = argparse.ArgumentParser(description="Check European festivals for open calls and summarise the results.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--continent", default="europe", choices=CONTINENTS)
    parser.add_argument("--period", default="2025-01-01_2026-12-31", help="Scrape period in the festival CSV names.")
    parser.add_argument("--limit", type=int, help="Check at most this many websites (for a trial run).")
    parser.add_argument("--summary-only", action="store_true", help="Only rebuild the summary from the latest results.")
    parser.add_argument("--no-review", action="store_true", help="Skip the large model review.")
    parser.add_argument("--no-reuse", action="store_true", help="Check websites again even if already checked.")
    parser.add_argument("--ignore-robots", action="store_true", help="Fetch pages even if robots.txt disallows it.")
    parser.add_argument("--min-page-date", default="2025-01-01", type=parse_min_date,
                        help="Don't process pages last modified or created before this date (default: 2025-01-01).")
    parser.add_argument("--crawl-workers", type=int, default=8)
    parser.add_argument("--llm-slots", type=int, default=4)
    parser.add_argument("--review-slots", type=int, default=2)
    args = parser.parse_args()

    log_path = logs_dir(args.data_dir) / f"check_{args.continent}_festivals_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)])
    for noisy in ("httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    paths = festival_csvs(args.data_dir, args.continent, args.period)
    if not paths:
        logger.error("No %s*%s.csv files for %s in %s", FESTIVALS_PREFIX, args.period, args.continent,
                     scraped_dir(args.data_dir))
        return 1
    scope = f"{args.continent}_festivals_{args.period}"
    logger.info("%d festival CSVs in %d countries. Log: %s", len(paths), len({path.parent.parent.name for path in paths}),
                log_path)

    stop_on_sigterm()
    stopped = False
    try:
        with progress_logging(), runs.run("open_calls", args.data_dir, f"{args.continent} festivals, {args.period}",
                                          vars(args)):
            results_path = run_checks(args, paths, scope)
    except KeyboardInterrupt:
        # Every site finished before the stop is saved; summarise what there is
        stopped = True
        results_path = latest_results(args.data_dir, scope)
        logger.warning("Stopped. Writing the summary from results saved so far (%s); run the same command again to "
                       "continue.", results_path)

    output_dir = summary_dir(args.data_dir)
    title = f"{args.continent.replace('_', ' ').title()} festival open calls ({args.period.replace('_', ' to ')})"
    festival_path, country_path, markdown_path = write_summary(
        paths, results_path, output_dir, f"{scope}_{date.today()}",
        country_names(), title)
    print(f"Festivals: {festival_path}\nBy country: {country_path}\nOverview: {markdown_path}")
    return 130 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
