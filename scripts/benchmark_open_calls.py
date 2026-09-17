"""
Benchmark open call checking on the promoter websites in one events or festivals CSV.

1. Crawl every site once with parallel crawlers, recording per-site crawl time, and cache the crawled pages.
2. Classify the same cached pages with each classifier configuration, timing the whole batch and comparing verdicts
   with the baseline (in-process, grammar-constrained: the original behaviour).
3. Estimate end-to-end throughput for a full run from the crawl and classification rates.

Usage:
    python scripts/benchmark_open_calls.py --csv data/ch/zurich/festivals_2025-01-01_2026-12-31.csv
    python scripts/benchmark_open_calls.py --csv ... --modes local-constrained server-8 --reuse-crawl
"""
import argparse
import copy
import json
import logging
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from open_call_agent.open_call.batch import classify_prepared, collect_websites, prepare_site
from open_call_agent.open_call.classifier import LlamaServer, LocalClassifier, ServerClassifier
from open_call_agent.ra.events import read_rows

logger = logging.getLogger("benchmark")

# name -> (backend, server slots, always constrained)
MODES = {
    "local-constrained": ("local", 1, True),
    "local-unconstrained": ("local", 1, False),
    "server-1-constrained": ("server", 1, True),
    "server-1": ("server", 1, False),
    "server-4": ("server", 4, False),
    "server-8": ("server", 8, False),
    "server-8-constrained": ("server", 8, True),
}
BASELINE = "local-constrained"


def crawl(websites, crawl_workers, ignore_robots, cache_path, reuse):
    """
    Crawl sites in parallel, or load a previous crawl from the cache.

    :return: A tuple of (list of (website, prepared SiteChecker or None, crawl seconds), crawl wall seconds).
    """
    if reuse and cache_path.exists():
        logger.info("Loading cached crawl from %s", cache_path)
        cached = pickle.loads(cache_path.read_bytes())
        return cached["sites"], cached["wall_seconds"]

    def prepare(website):
        try:
            checker, seconds = prepare_site(website, ignore_robots=ignore_robots)
            logger.info("Crawled %s: %d pages, %d to classify, %.0fs", website, len(checker.pages),
                        len(checker.selected_pages), seconds)
            return website, checker, seconds
        except Exception:
            logger.exception("Crawl failed for %s", website)
            return website, None, 0

    started = time.perf_counter()
    with ThreadPoolExecutor(crawl_workers) as pool:
        sites = list(pool.map(prepare, websites))
    wall_seconds = time.perf_counter() - started
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(pickle.dumps({"sites": sites, "wall_seconds": wall_seconds}))
    return sites, wall_seconds


def run_mode(name, sites, model_path, log_dir):
    """
    Classify every crawled site with one configuration, all sites submitted at once so the classifier is saturated.

    :return: A results dict.
    """
    backend, slots, constrained = MODES[name]
    server = None
    load_started = time.perf_counter()
    if backend == "local":
        classifier = LocalClassifier(model_path, constrained)
    else:
        server = LlamaServer(model_path, slots, log_dir / "llama-server.log")
        classifier = ServerClassifier(server, constrained)
    load_seconds = time.perf_counter() - load_started

    prepared = [(website, copy.deepcopy(checker)) for website, checker, _ in sites if checker is not None]
    chunks = sum(len(checker.chunk_texts(page)) for _, checker in copy.deepcopy(prepared)
                 for page in checker.selected_pages)
    logger.info("%s: classifying %d chunks from %d sites", name, chunks, len(prepared))

    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(classifier.max_concurrency) as classify_pool, ThreadPoolExecutor(len(prepared)) as site_pool:
            futures = {website: site_pool.submit(classify_prepared, checker, classifier, classify_pool)
                       for website, checker in prepared}
            reports = {website: future.result()[0] for website, future in futures.items()}
        wall_seconds = time.perf_counter() - started
    finally:
        if server is not None:
            server.close()
        else:
            classifier.close()

    llm_errors = sum(1 for report in reports.values() for error in report["errors"] if error["stage"] == "llm")
    return {
        "mode": name, "backend": backend, "slots": slots, "constrained": constrained,
        "load_seconds": round(load_seconds, 1), "wall_seconds": round(wall_seconds, 1), "chunks": chunks,
        "seconds_per_chunk": round(wall_seconds / max(chunks, 1), 2), "llm_errors": llm_errors,
        **classifier.stats.as_dict(),
        "verdicts": {website: report["status"] for website, report in reports.items()},
        "page_statuses": {page["url"]: (page.get("classification") or {}).get("status")
                          for report in reports.values() for page in report["pages"]},
    }


def write_summary(results, sites, crawl_wall_seconds, crawl_workers, csv_path, output_dir, stamp):
    """
    Write the benchmark results as JSON and a Markdown summary.

    :return: The Markdown summary Path.
    """
    crawled = [(website, checker, seconds) for website, checker, seconds in sites if checker is not None]
    crawl_seconds = [seconds for _, _, seconds in crawled]
    baseline = next((result for result in results if result["mode"] == BASELINE), None)

    lines = [f"# Open call benchmark: {csv_path}", "",
             f"{len(sites)} websites, {len(crawled)} crawled, {datetime.now():%Y-%m-%d %H:%M}", "",
             "## Crawl", "",
             f"- {crawl_workers} parallel crawlers: {crawl_wall_seconds:.0f}s wall",
             f"- Sequential (sum of per-site times): {sum(crawl_seconds):.0f}s",
             f"- Per site: mean {sum(crawl_seconds) / max(len(crawl_seconds), 1):.0f}s, max {max(crawl_seconds, default=0):.0f}s",
             "", "## Classification (same cached pages for every mode)", "",
             "| Mode | Wall s | Chunks | s/chunk | Speedup | Prompt tok | Output tok | Grammar retries | LLM errors | Same site verdicts | Same page statuses |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for result in results:
        speedup = f"{baseline['wall_seconds'] / result['wall_seconds']:.1f}x" if baseline and result["wall_seconds"] else ""
        if baseline:
            same_sites = sum(result["verdicts"].get(site) == verdict for site, verdict in baseline["verdicts"].items())
            same_pages = sum(result["page_statuses"].get(url) == status for url, status in baseline["page_statuses"].items())
            agreement = (f"{same_sites}/{len(baseline['verdicts'])}", f"{same_pages}/{len(baseline['page_statuses'])}")
        else:
            agreement = ("", "")
        lines.append(f"| {result['mode']} | {result['wall_seconds']} | {result['chunks']} | {result['seconds_per_chunk']} | "
                     f"{speedup} | {result['prompt_tokens']} | {result['completion_tokens']} | "
                     f"{result['grammar_fallbacks']} | {result['llm_errors']} | {agreement[0]} | {agreement[1]} |")

    if baseline:
        lines += ["", "## Verdict differences from baseline", ""]
        for result in results:
            differences = [f"{site}: {verdict} -> {result['verdicts'].get(site)}"
                           for site, verdict in baseline["verdicts"].items() if result["verdicts"].get(site) != verdict]
            lines.append(f"- {result['mode']}: {'; '.join(differences) or 'none'}")

    lines += ["", "## Site verdicts (baseline)", ""]
    lines += [f"- {site}: {verdict}" for site, verdict in (baseline or results[0])["verdicts"].items()] if results else []

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"benchmark_{stamp}.json").write_text(json.dumps(
        {"csv": str(csv_path), "crawl_workers": crawl_workers, "crawl_wall_seconds": crawl_wall_seconds,
         "crawl_seconds": {website: seconds for website, _, seconds in sites}, "results": results}, indent=2))
    summary_path = output_dir / f"benchmark_{stamp}.md"
    summary_path.write_text("\n".join(lines) + "\n")
    return summary_path


def main():
    parser = argparse.ArgumentParser(description="Benchmark open call checking on one CSV.")
    parser.add_argument("--csv", required=True, type=Path, help="An events or festivals CSV.")
    parser.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    parser.add_argument("--crawl-workers", type=int, default=8)
    parser.add_argument("--ignore-robots", action="store_true")
    parser.add_argument("--reuse-crawl", action="store_true", help="Reuse the cached crawl for this CSV.")
    parser.add_argument("--model-path")
    parser.add_argument("--output-dir", type=Path, default=Path("data/benchmarks"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    websites = list(dict.fromkeys(site["website"] for site in collect_websites([args.csv]).values()))
    logger.info("%d websites from %d rows in %s", len(websites), len(read_rows(args.csv)), args.csv)

    cache_path = args.output_dir / f"crawl_cache_{args.csv.parent.parent.name}_{args.csv.parent.name}_{args.csv.stem}.pkl"
    sites, crawl_wall_seconds = crawl(websites, args.crawl_workers, args.ignore_robots, cache_path, args.reuse_crawl)

    results = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name in args.modes:
        results.append(run_mode(name, sites, args.model_path, args.output_dir))
        logger.info("%s: %.0fs for %d chunks", name, results[-1]["wall_seconds"], results[-1]["chunks"])
        summary_path = write_summary(results, sites, crawl_wall_seconds, args.crawl_workers, args.csv, args.output_dir, stamp)

    print(summary_path.read_text())
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
