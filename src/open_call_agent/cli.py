"""
Command line interface.

    open-call-agent scrape --country ch --start 2026-09-16 --end 2026-09-18
    open-call-agent scrape --city ch/zurich --start 2026-09-16 --end 2026-09-18
    open-call-agent scrape --continent europe --start 2026-09-16 --end 2026-09-18
    open-call-agent festivals --country ch
    open-call-agent check-open-calls --country ch --festivals-only
    open-call-agent review-open-calls --input data/open_calls/open_calls_ch_festivals_2026-09-17.csv
    open-call-agent check-site -u http://fogstudio.ch/
"""
import argparse
import contextlib
import json
import logging
import sys
from datetime import date

from . import runs
from .paths import DEFAULT_DATA_DIR, logs_dir, scraped_dir
from .progress import progress_logging
from .storage import stop_on_sigterm


def valid_date(value):
    """
    :param value: A date string.
    :return: The value, if it is a valid YYYY-MM-DD date.
    :raises argparse.ArgumentTypeError: If it isn't.
    """
    try:
        date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}', expected YYYY-MM-DD")
    return value


def add_checker_arguments(parser):
    parser.add_argument("--ignore-robots", action="store_true", help="Fetch pages even if robots.txt disallows it.")
    parser.add_argument("--model-path", help="Path to a GGUF model (default: Qwen2.5-3B-Instruct Q4_K_M from the "
                                             "Hugging Face cache).")
    parser.add_argument("--max-pages", type=int, default=40, help="Maximum pages to fetch per site (default: 40).")
    parser.add_argument("--max-llm-pages", type=int, default=12, help="Maximum pages to classify per site (default: 12).")
    add_date_argument(parser)


def add_date_argument(parser):
    parser.add_argument("--min-page-date", type=valid_min_date, default="2025-01-01",
                        help="Don't process pages last modified or created before this date, YYYY-MM-DD (default: "
                             "2025-01-01). Pages without a date are processed. 'none' processes every page.")


def valid_min_date(value):
    """
    :param value: A YYYY-MM-DD date or "none".
    :return: A date or None.
    :raises argparse.ArgumentTypeError: If the value is invalid.
    """
    from .open_call.dates import parse_min_date

    try:
        return parse_min_date(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}', expected YYYY-MM-DD or 'none'")


def add_review_arguments(parser):
    parser.add_argument("--review-model-path", help="Path to the larger GGUF model (default: Qwen2.5-7B-Instruct Q4_K_M "
                                                    "from the Hugging Face cache).")
    parser.add_argument("--review-slots", type=int, default=2,
                        help="Parallel sequences for the larger model (default: 2).")


def build_parser():
    parser = argparse.ArgumentParser(prog="open-call-agent",
                                     description="Scrape RA events and check promoter websites for open calls.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help=f"Data directory (default: {DEFAULT_DATA_DIR}, or $OPEN_CALL_AGENT_DATA_DIR).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    commands = parser.add_subparsers(dest="command", required=True)

    scrape = commands.add_parser("scrape", help="Scrape RA events into data/<country>/<city>/events_<start>_<end>.csv.")
    where = scrape.add_mutually_exclusive_group(required=True)
    where.add_argument("--continent", help="e.g. europe, north_america, south_america, asia, africa, oceania.")
    where.add_argument("--country", help="RA country code as used in ra.co URLs, e.g. ch, uk, de, us.")
    where.add_argument("--city", help="<country>/<city> as used in ra.co URLs, e.g. ch/zurich.")
    scrape.add_argument("--start", required=True, type=valid_date, help="Start date (inclusive), YYYY-MM-DD.")
    scrape.add_argument("--end", required=True, type=valid_date, help="End date (inclusive), YYYY-MM-DD.")
    scrape.add_argument("--festivals", action="store_true", help="Also identify festivals in the scraped CSVs.")
    scrape.add_argument("--save-raw", action="store_true",
                        help="Also save each festival's full RA response as data/raw_events/<country>/<city>/<id>.json.")

    festivals = commands.add_parser("festivals", help="Find events lasting more than 24 hours in existing event CSVs.")
    festivals.add_argument("--country", help="Limit to a country code.")
    festivals.add_argument("--city", help="Limit to a city folder (requires --country).")
    festivals.add_argument("--min-hours", type=float, default=24, help="Minimum duration in hours (default: 24).")

    check = commands.add_parser("check-open-calls", help="Check promoter websites in existing CSVs for open calls.")
    check.add_argument("--country", help="Limit to a country code.")
    check.add_argument("--city", help="Limit to a city folder (requires --country).")
    check.add_argument("--festivals-only", action="store_true", help="Only check websites of festivals.")
    check.add_argument("--limit", type=int, help="Maximum number of websites to check.")
    check.add_argument("--no-reuse", action="store_true",
                       help="Check sites again even if the same model already checked or reviewed them.")
    check.add_argument("-o", "--output", help="Output CSV path.")
    check.add_argument("--backend", choices=["server", "local"], default="server",
                       help="server: llama-server with continuous batching (default); local: in-process, one call at a time.")
    check.add_argument("--llm-slots", type=int, default=4, help="Parallel sequences for the server backend (default: 4).")
    check.add_argument("--crawl-workers", type=int, default=8, help="Sites crawled at the same time (default: 8).")
    check.add_argument("--constrained-json", action="store_true",
                       help="With --backend local, always constrain output with the JSON schema (slower). "
                            "The server backend always does, at no cost.")
    check.add_argument("--no-review", action="store_true",
                       help="Skip the second pass, where a larger model reviews open and needs_review sites.")
    add_review_arguments(check)
    add_checker_arguments(check)

    review = commands.add_parser("review-open-calls",
                                 help="Review open and needs_review sites in a check-open-calls CSV with a larger model.")
    review.add_argument("--input", required=True, help="A results CSV from check-open-calls.")
    review.add_argument("-o", "--output", help="Output CSV path (default: <input>_reviewed.csv).")
    review.add_argument("--ignore-robots", action="store_true", help="Fetch pages even if robots.txt disallows it.")
    review.add_argument("--no-reuse", action="store_true", help="Review sites again even if the same model already did.")
    add_date_argument(review)
    add_review_arguments(review)

    site = commands.add_parser("check-site", help="Check a single website for an open call.")
    site.add_argument("-u", "--url", required=True, help="The website, e.g. http://fogstudio.ch/")
    site.add_argument("-o", "--output", help="Write the full JSON report to this file.")
    add_checker_arguments(site)
    return parser


def run_scrape(args):
    from .festivals import identify_festivals
    from .scraper import scrape_city, scrape_continent, scrape_country

    if args.start > args.end:
        raise ValueError("--start must be on or before --end")
    if args.city:
        if "/" not in args.city:
            raise ValueError("--city must be <country>/<city>, e.g. ch/zurich")
        country_code, city = args.city.split("/", 1)
        paths = scrape_city(country_code, city, args.start, args.end, args.data_dir, save_raw=args.save_raw)
    elif args.country:
        paths = scrape_country(args.country, args.start, args.end, args.data_dir, save_raw=args.save_raw)
    else:
        paths = scrape_continent(args.continent.lower(), args.start, args.end, args.data_dir, save_raw=args.save_raw)

    if args.festivals:
        # Only the CSVs just scraped, so other date ranges of the same cities are left alone
        identify_festivals(args.data_dir, events_paths=paths)
    print(f"Wrote {len(paths)} event CSV files to {scraped_dir(args.data_dir)}")


def run_festivals(args):
    from .festivals import identify_festivals

    if args.city and not args.country:
        raise ValueError("--city requires --country")
    paths = identify_festivals(args.data_dir, args.country, args.city, args.min_hours)
    if not paths:
        raise FileNotFoundError(f"No event CSVs found in {scraped_dir(args.data_dir)}. Run the scrape command first.")
    print(f"Wrote {len(paths)} festival CSV files")


def run_check_open_calls(args):
    from .open_call.batch import check_open_calls

    if args.city and not args.country:
        raise ValueError("--city requires --country")
    path = check_open_calls(args.data_dir, args.country, args.city, args.festivals_only, args.limit, args.ignore_robots,
                            args.model_path, args.max_pages, args.max_llm_pages, not args.no_reuse, args.output,
                            args.backend, args.llm_slots, args.crawl_workers, args.constrained_json, args.min_page_date)
    print(f"Small model results: {path}")
    if not args.no_review:
        # check_open_calls has stopped the small model, so the larger model has the GPU to itself
        from .open_call.review import review_open_calls

        reviewed_path = review_open_calls(path, model_path=args.review_model_path, llm_slots=args.review_slots,
                                          ignore_robots=args.ignore_robots, log_dir=logs_dir(args.data_dir),
                                          min_page_date=args.min_page_date, reuse_reviews=not args.no_reuse,
                                          data_dir=args.data_dir)
        print(f"Reviewed results: {reviewed_path}")


def run_review_open_calls(args):
    from .open_call.review import review_open_calls

    path = review_open_calls(args.input, args.output, args.review_model_path, args.review_slots,
                             ignore_robots=args.ignore_robots, log_dir=logs_dir(args.data_dir),
                             min_page_date=args.min_page_date, reuse_reviews=not args.no_reuse, data_dir=args.data_dir)
    print(f"Reviewed results: {path}")


def run_check_site(args):
    from .open_call.checker import SiteChecker
    from .open_call.classifier import LocalClassifier

    classifier = LocalClassifier(args.model_path)
    try:
        report = SiteChecker(args.url, classifier, args.max_pages, args.max_llm_pages, args.ignore_robots,
                             min_page_date=args.min_page_date).run()
    finally:
        classifier.close()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file:
            json.dump(report, file, indent=2, ensure_ascii=False)

    print(f"Site: {report['site']}")
    print(f"Status: {report['status']} ({report['status_reason']})")
    for label, value in [("Deadline", report["deadline"]), ("Pages skipped as too old",
                         len(report["coverage"]["skipped_by_date"]) or ""), ("Announcement", ", ".join(report["announcement_urls"])),
                         ("Open call", ", ".join(report["open_call_urls"])),
                         ("Types", ", ".join(report["opportunity_types"]))]:
        if value:
            print(f"{label}: {value}")
    for call in report["call_pages"]:
        print(f"  {call['status']} ({call['confidence']} confidence): {call['url']}")
        for quote in call["verified_quotes"]:
            print(f"    \"{quote}\"")
    coverage = report["coverage"]
    print(f"Coverage: {coverage['discovery']}; {coverage['pages_visited']} pages visited "
          f"({coverage['js_rendered_pages']} JS-rendered), {coverage['pages_classified']} classified")
    for document in coverage["unchecked_documents"]:
        print(f"Unchecked document: {document['url']} ({document['link_text']})")
    for warning in report["warnings"]:
        print(f"Warning: {warning}")
    for error in report["errors"]:
        print(f"Error [{error['stage']}]: {error['url']}: {error['error']}")


COMMANDS = {"scrape": run_scrape, "festivals": run_festivals, "check-open-calls": run_check_open_calls,
            "review-open-calls": run_review_open_calls, "check-site": run_check_site}
# Commands worth recording as a run for the dashboard, and what kind of run they are
RUN_KINDS = {"scrape": "scrape", "festivals": "scrape", "check-open-calls": "open_calls",
             "review-open-calls": "open_calls"}


def run_label(args):
    """
    :param args: The parsed command line arguments.
    :return: What the run is, e.g. "ch, 2026-09-16 to 2026-09-18".
    """
    where = getattr(args, "city", None) or getattr(args, "country", None) or getattr(args, "continent", None) or \
        getattr(args, "input", None) or "everything scraped"
    dates = f", {args.start} to {args.end}" if getattr(args, "start", None) else ""
    return f"{where}{dates}"


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop_on_sigterm()
    arguments = {name: value for name, value in vars(args).items() if value not in (None, False)}
    try:
        with progress_logging(), runs.run(RUN_KINDS[args.command], args.data_dir, run_label(args), arguments) \
                if args.command in RUN_KINDS else contextlib.nullcontext():
            COMMANDS[args.command](args)
    except (ValueError, FileNotFoundError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Results saved so far are kept; run the same command again to continue.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
