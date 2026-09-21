"""
Second pass: a larger model reviews the sites the small model found open or needing review.

For each such row in a first-pass results CSV, the announcement pages and open call (form) pages are fetched again and
classified by the large model (Qwen2.5-7B-Instruct by default), which decides the final status: open, closed,
needs_review or none. All pages are fetched before the model loads, and the model is only loaded once the small model
has released the GPU (see classifier.acquire_gpu), so only one model occupies the GPU at a time.
"""
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from .. import runs
from ..progress import progress
from ..storage import read_json, write_json_atomic
from ..paths import DEFAULT_DATA_DIR, logs_dir, reviews_dir, safe_filename
from ..ra.events import read_rows, split_links, write_rows
from .batch import RESULT_COLUMNS, STATUSES, check_open_calls
from ..urls import website_key
from .checker import FORM_CLOSED_RE, FORM_URL_RE, SiteChecker
from .text import normalize_text
from .dates import MIN_PAGE_DATE, format_date, is_too_old, normalize_deadline_column
from .classifier import LARGE_MODEL, MAX_CHUNKS_PER_PAGE, LlamaServer, most_positive, split_text, verify, \
    ServerClassifier, resolve_model_path

logger = logging.getLogger(__name__)

REVIEW_STATUSES = ("open", "needs_review")
REVIEW_COLUMNS = RESULT_COLUMNS + ["small_model_status", "large_model_status", "review_reason", "review_quotes",
                                   "reviewed_on"]
REVIEW_CONTEXT_TOKENS = 16384  # Announcement chunk plus form pages
MAX_FORM_PAGES = 3
MAX_FORM_CHARS = 4000
# Fields the large model decides, saved per site in open_calls/reviews/ so a site isn't reviewed twice by the same model
REVIEW_FIELDS = ["status", "deadline", "deadline_text", "open_call_url", "announcement_url", "opportunity_types",
                 "how_to_apply", "large_model_status", "review_reason", "review_quotes", "reviewed_on"]
def fetch_review_page(url, ignore_robots=False, min_page_date=MIN_PAGE_DATE):
    """
    Fetch one page for review, respecting robots.txt unless told otherwise.

    :param url: The page URL.
    :param ignore_robots: Fetch the page even if robots.txt disallows it.
    :param min_page_date: Don't process the page if it was last modified or created before this date.
    :return: A tuple of (Page or None, error message or None). A page that is too old returns None and an error
        starting with "Too old:".
    """
    checker = SiteChecker(url, ignore_robots=ignore_robots, min_page_date=min_page_date)
    try:
        if not ignore_robots:
            checker.load_robots()
            if not checker.allowed_by_robots(checker.start_url):
                return None, f"robots.txt disallows {url}"
        page = checker.fetch_page(checker.start_url)
        if page is None:
            errors = "; ".join(error["error"] for error in checker.errors)
            return None, f"Could not fetch {url}{': ' + errors if errors else ''}"
        too_old = is_too_old(page.dates, min_page_date)
        if too_old:
            return None, (f"Too old: {url} was last modified or created {format_date(too_old[1])} ({too_old[0]}), "
                          f"before {format_date(min_page_date)}")
        return page, None
    finally:
        checker.close()


def page_status(result, has_evidence, form_state=None):
    """
    Map a page classification to a site status. Whether applications are accepted today is taken from the linked
    hosted form when there is one: the 7B model often labelled calls for a future edition "not yet open" even when
    their form was accepting submissions.

    :param result: A classification dict with a verified status.
    :param has_evidence: Whether the page has keyword hits, a form link or a fetched open call page.
    :param form_state: "accepting" or "closed" for a fetched hosted form, or None.
    :return: One of STATUSES.
    """
    describes_call = result["status"] != "none" and result["verified_quotes"]
    if describes_call and form_state == "closed":
        return "closed"
    if result["status"] in ("open", "not_yet_open") and describes_call and form_state == "accepting":
        return "open"
    if result["status"] == "open":
        return "open" if has_evidence else "needs_review"
    if result["status"] in ("not_yet_open", "unclear"):
        return "needs_review"
    return result["status"]


def form_state(form_pages):
    """
    :param form_pages: Fetched open call Pages.
    :return: "accepting" if any hosted form accepts responses, "closed" if all hosted forms are closed, else None.
    """
    hosted = [page for page in form_pages if FORM_URL_RE.search(page.url)]
    if not hosted:
        return None
    return "closed" if all(FORM_CLOSED_RE.search(page.text) for page in hosted) else "accepting"


def build_chunks(announcement, form_pages):
    """
    Build the texts to classify for one announcement page: its text in chunks, each followed by the open call pages.

    :param announcement: The announcement Page.
    :param form_pages: Fetched open call Pages.
    :return: A list of texts.
    """
    forms = "".join(f"\n\n--- Open call page: {page.url} ---\n{page.text[:MAX_FORM_CHARS]}" for page in form_pages)
    return [f"{chunk}{forms}" for chunk in split_text(announcement.text)[:MAX_CHUNKS_PER_PAGE]]


def fetch_evidence(row, ignore_robots, min_page_date=MIN_PAGE_DATE):
    """
    Fetch a row's announcement and open call pages. Pages last modified or created before min_page_date are left out.

    :param row: A first-pass results row.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param min_page_date: Don't process pages last modified or created before this date.
    :return: A dict with announcement pages, form pages, fetch errors (announcement_errors counts those for
        announcement pages), and pages left out as too old.
    """
    announcements, forms, errors, too_old = [], [], [], []
    for url in split_links(row["announcement_url"]):
        page, error = fetch_review_page(url, ignore_robots, min_page_date)
        if page:
            announcements.append(page)
        else:
            (too_old if error.startswith("Too old:") else errors).append(error)
    announcement_errors = len(errors)
    for url in split_links(row["open_call_url"])[:MAX_FORM_PAGES]:
        page, error = fetch_review_page(url, ignore_robots, min_page_date)
        if page:
            forms.append(page)
        else:
            (too_old if error.startswith("Too old:") else errors).append(error)
    return {"announcements": announcements, "forms": forms, "errors": errors, "too_old": too_old,
            "announcement_errors": announcement_errors}


def review_row(row, evidence, results_by_page):
    """
    Decide a row's final status from the large model's page results.

    :param row: The first-pass results row.
    :param evidence: The fetched pages from fetch_evidence.
    :param results_by_page: A dict of announcement URL -> list of chunk classification dicts (None if a chunk failed).
    :return: The reviewed row.
    """
    reviewed = {**row, "small_model_status": row["status"], "large_model_status": "", "review_quotes": "",
                "reviewed_on": date.today().isoformat()}
    if not split_links(row["announcement_url"]):
        return {**reviewed, "review_reason": f"Not reviewed: no announcement page ({row['status_reason']})"}
    if not evidence["announcements"]:
        # Every announcement page was fetched but is older than the cutoff: not processed, so no current call
        if evidence["too_old"] and not evidence["announcement_errors"]:
            return {**reviewed, "status": "none", "large_model_status": "", "deadline": "", "deadline_text": "",
                    "open_call_url": "", "announcement_url": "", "opportunity_types": "", "how_to_apply": "",
                    "review_reason": f"Not processed: {'; '.join(evidence['too_old'])}"}
        return {**reviewed, "status": "needs_review",
                "review_reason": f"Not reviewed: {'; '.join(evidence['errors'] + evidence['too_old'])}"}

    evidence_text = normalize_text("\n".join(page.text for page in evidence["announcements"] + evidence["forms"]))
    form_urls = [page.url for page in evidence["forms"]]
    forms = form_state(evidence["forms"])
    page_results = []
    for page in evidence["announcements"]:
        chunk_results = results_by_page.get(page.url) or [None]
        if None in chunk_results:
            page_results.append((page, None, "needs_review"))
            continue
        best = most_positive([verify(result, evidence_text, where="the pages") for result in chunk_results])
        has_evidence = bool(page.keyword_hits or page.form_links or form_urls)
        page_results.append((page, best, page_status(best, has_evidence, forms)))

    status = min((status for _, _, status in page_results), key=STATUSES.index)
    decided = [(page, result) for page, result, page_result_status in page_results
               if page_result_status == status and result is not None]
    failed = sum(result is None for _, result, _ in page_results)
    reasons = [f"{page.url}: {result['reasoning']}" for page, result in decided]
    if failed:
        reasons.append(f"{failed} announcement page(s) failed classification")
    if forms and status in ("open", "closed"):
        reasons.append(f"Linked form is {forms}")
    if evidence["errors"]:
        reasons.append(f"Fetch errors: {'; '.join(evidence['errors'])}")
    if evidence["too_old"]:
        reasons.append(f"Not processed: {'; '.join(evidence['too_old'])}")

    reviewed.update({"status": status, "large_model_status": status, "review_reason": " | ".join(reasons),
                     "review_quotes": " | ".join(quote for _, result in decided for quote in result["verified_quotes"])})
    if status == "none":
        reviewed.update({"deadline": "", "deadline_text": "", "open_call_url": "", "announcement_url": "",
                         "opportunity_types": "", "how_to_apply": ""})
        return reviewed

    # Keep the first pass's open call links (menu links were already filtered there) plus hosted forms found now
    new_forms = [link for page, _ in decided for link, _ in page.form_links if FORM_URL_RE.search(link)]
    deadlines = sorted({result["deadline"] for _, result in decided if result["deadline"]})
    deadline_texts = [result["deadline_text"] for _, result in decided if result["deadline_text"]]
    reviewed.update({
        "announcement_url": ", ".join(page.url for page, _ in decided) or row["announcement_url"],
        "open_call_url": ", ".join(dict.fromkeys(split_links(row["open_call_url"]) + new_forms)),
        "deadline": " | ".join(deadlines) or row["deadline"],
        "deadline_text": " | ".join(dict.fromkeys(deadline_texts)) or row.get("deadline_text", ""),
        "opportunity_types": ", ".join(sorted({kind for _, result in decided for kind in result["opportunity_types"]}))
                             or row["opportunity_types"],
        "how_to_apply": " | ".join(dict.fromkeys(result["how_to_apply"] for _, result in decided
                                                 if result["how_to_apply"])) or row["how_to_apply"],
    })
    return reviewed


def review_path(directory, website):
    """
    :param directory: Where saved reviews live.
    :param website: The website URL.
    :return: The Path of that site's saved review.
    """
    return directory / f"{safe_filename(website_key(website))}.json"


def plan_reviews(rows, directory, model_name, reuse_reviews):
    """
    Work out which sites the large model has to read, and start the reviewed rows off from the first pass.

    A site is skipped when the small model found nothing worth reviewing, when it has no announcement page to read, or
    when the same model has reviewed it before, in which case the saved review is used.

    :param rows: The first-pass result rows.
    :param directory: Where saved reviews live.
    :param model_name: The large model's file name, which saved reviews record.
    :param reuse_reviews: Use saved reviews instead of reviewing again.
    :return: A tuple of (reviewed rows, indexes to review, number of reviews reused).
    """
    reviewed_rows, candidates, reused = [], [], 0
    for index, row in enumerate(rows):
        reviewed_rows.append({**row, "small_model_status": row["status"]})
        if row["status"] not in REVIEW_STATUSES:
            continue
        if not split_links(row["announcement_url"]):
            reviewed_rows[index].update({"reviewed_on": date.today().isoformat(),
                                         "review_reason": f"Not reviewed: no announcement page ({row['status_reason']})"})
            continue
        saved = read_json(review_path(directory, row["website"])) if reuse_reviews else None
        if saved and saved.get("model") == model_name and all(field in saved["review"] for field in REVIEW_FIELDS):
            reviewed_rows[index].update({field: saved["review"][field] for field in REVIEW_FIELDS})
            reused += 1
            continue
        reviewed_rows[index]["review_reason"] = "Review pending"
        candidates.append(index)
    return reviewed_rows, candidates, reused


def fetch_all_evidence(rows, candidates, ignore_robots, min_page_date, fetch_workers):
    """
    Fetch every candidate's pages before the model is loaded, so the large model only holds the GPU while classifying.

    :param rows: The first-pass result rows.
    :param candidates: Indexes to fetch for.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param min_page_date: Don't process pages last modified or created before this date.
    :param fetch_workers: Pages fetched at the same time.
    :return: A dict of index -> evidence.
    """
    evidence = {}
    with ThreadPoolExecutor(fetch_workers, thread_name_prefix="review-fetch") as pool, \
            progress(total=len(candidates), desc="Large model: fetching pages", unit="site") as bar:
        futures = {pool.submit(fetch_evidence, rows[index], ignore_robots, min_page_date): index for index in candidates}
        try:
            for future in as_completed(futures):
                evidence[futures[future]] = future.result()
                bar.update(1)
        finally:
            for future in futures:
                future.cancel()
    return evidence


def review_open_calls(input_path, output_path=None, model_path=None, llm_slots=2, fetch_workers=4,
                      ignore_robots=False, log_dir=None, min_page_date=MIN_PAGE_DATE, reuse_reviews=True,
                      data_dir=DEFAULT_DATA_DIR):
    """
    Review first-pass results with the large model.

    :param input_path: A first-pass results CSV from check_open_calls.
    :param output_path: Optional output CSV Path; defaults to <input>_reviewed.csv.
    :param model_path: Optional path to a GGUF model; defaults to LARGE_MODEL.
    :param llm_slots: Parallel sequences on the large model's server.
    :param fetch_workers: Pages fetched at the same time.
    :param ignore_robots: Fetch pages even if robots.txt disallows it.
    :param log_dir: Optional directory for the llama-server log.
    :param min_page_date: Don't process pages last modified or created before this date (pages without a date are
        processed); None processes all.
    :param reuse_reviews: Don't review a site again if the same model has reviewed it before (on any day).
    :param data_dir: The data directory; reviews are saved per site in <data_dir>/open_calls/reviews/.
    :return: The output CSV Path.
    """
    input_path = Path(input_path)
    reviews = reviews_dir(data_dir)
    reviews.mkdir(parents=True, exist_ok=True)
    # Identifies the model without loading it
    model_name = Path(resolve_model_path(model_path, LARGE_MODEL)).name

    output_path = Path(output_path or input_path.with_name(f"{input_path.stem}_reviewed.csv"))
    # Deadlines from older result files may be free text; store them all as YYYY-MM-DD
    rows = [{**row, "deadline_text": row.get("deadline_text") or row["deadline"],
             "deadline": normalize_deadline_column(row["deadline"])} for row in read_rows(input_path)]

    # The reviewed rows start as the small model's results (or earlier reviews) and are updated site by site. They are
    # written to the CSV after every reviewed site, so a stopped run keeps everything reviewed so far.
    reviewed_rows, candidates, reused = plan_reviews(rows, reviews, model_name, reuse_reviews)
    write_rows(reviewed_rows, output_path, REVIEW_COLUMNS)
    logger.info("Reviewing %d of %d sites with the large model (%d already reviewed by %s)", len(candidates), len(rows),
                reused, model_name)

    def finish(index, results_by_page):
        """
        Decide a site's final status, save its review and update the reviewed CSV.
        """
        row = rows[index]
        reviewed = review_row(row, evidence[index], results_by_page)
        reviewed_rows[index] = reviewed
        logger.info("%s: %s -> %s", row["website"], row["status"], reviewed["status"])
        runs.report(reviewed=1, **{f"reviewed_{reviewed['status']}": 1})
        # Only a site the model actually classified counts as reviewed; fetch failures are retried next time
        if reviewed["large_model_status"]:
            write_json_atomic(review_path(reviews, row["website"]),
                              {"website": row["website"], "model": model_name,
                               "review": {field: reviewed[field] for field in REVIEW_FIELDS}})
        write_rows(reviewed_rows, output_path, REVIEW_COLUMNS)

    evidence = fetch_all_evidence(rows, candidates, ignore_robots, min_page_date, fetch_workers)
    jobs = [(index, page, chunk, text) for index in candidates for page in evidence[index]["announcements"]
            for chunk, text in enumerate(build_chunks(page, evidence[index]["forms"]))]
    # Sites with nothing to classify (every page failed to fetch or was too old) are decided without the model
    for index in candidates:
        if not evidence[index]["announcements"]:
            finish(index, {})

    if jobs:
        log_path = Path(log_dir) / "llama-server-review.log" if log_dir else None
        with LlamaServer(model_path, llm_slots, log_path, model=LARGE_MODEL,
                         context_tokens=REVIEW_CONTEXT_TOKENS) as server:
            classifier = ServerClassifier(server)

            def classify(job):
                index, page, _, text = job
                try:
                    return classifier.classify(page.url, page.title, text)
                except Exception as error:
                    logger.error("Review classification failed for %s: %s", page.url, error)
                    return None

            remaining = Counter(index for index, _, _, _ in jobs)
            chunk_results = defaultdict(dict)
            with ThreadPoolExecutor(llm_slots, thread_name_prefix="review-classify") as pool, \
                    progress(total=len(jobs), desc="Large model", unit="chunk") as bar:
                futures = {pool.submit(classify, job): job for job in jobs}
                try:
                    for future in as_completed(futures):
                        index, page, chunk, _ = futures[future]
                        chunk_results[index][(page.url, chunk)] = future.result()
                        bar.update(1)
                        remaining[index] -= 1
                        if remaining[index] == 0:
                            # All of this site's chunks are classified: save it now, in page and chunk order
                            results_by_page = {}
                            for (url, _), result in sorted(chunk_results.pop(index).items()):
                                results_by_page.setdefault(url, []).append(result)
                            finish(index, results_by_page)
                finally:
                    # When stopped early, don't send queued chunks to a server that is shutting down
                    for future in futures:
                        future.cancel()
            logger.info("Large model stats: %s", classifier.stats.as_dict())

    logger.info("Reviewed results -> %s", output_path)
    return output_path


def check_and_review_open_calls(data_dir, review_model_path=None, review_slots=2, **check_options):
    """
    Run the small-model check on every site, then, once its model has been unloaded, the large-model review.

    :param data_dir: The data directory.
    :param review_model_path: Optional path to the large GGUF model.
    :param review_slots: Parallel sequences on the large model's server.
    :param check_options: Keyword arguments for check_open_calls.
    :return: A tuple of (first-pass CSV Path, reviewed CSV Path).
    """
    first_pass_path = check_open_calls(data_dir, **check_options)
    reviewed_path = review_open_calls(first_pass_path, model_path=review_model_path, llm_slots=review_slots,
                                      ignore_robots=check_options.get("ignore_robots", False),
                                      log_dir=logs_dir(data_dir),
                                      min_page_date=check_options.get("min_page_date", MIN_PAGE_DATE),
                                      reuse_reviews=check_options.get("reuse_reports", True), data_dir=data_dir)
    return first_pass_path, reviewed_path
