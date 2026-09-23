# open_call_agent/open_call/

Deciding whether a website currently has an open call. This is where the project's judgement lives, and where a wrong
answer is most expensive — a false "open" sends the user to apply to something that closed months ago.

| Module | Role |
|---|---|
| [checker.py](checker.py) | Crawl one site: discover, fetch, shortlist, classify, report. The biggest file here. |
| [classifier.py](classifier.py) | The models, the prompt, the schema, and `verify()` — the evidence rules. |
| [batch.py](batch.py) | Run the checker over every website in the scraped CSVs, on one shared model. |
| [review.py](review.py) | Second pass: the 7B model re-reads what the 3B model found. |
| [links.py](links.py) | Check every URL and email afterwards, and open application forms in a browser. |
| [dates.py](dates.py) | Deadlines in any language, and how old a page is. |
| [text.py](text.py) | `normalize_text`, alone in its own module to break an import cycle — leave it there. |

## The rules

- **A positive needs evidence.** `classifier.verify` drops a status to `unclear` when the model's quote isn't in the
  text verbatim (normalised, at least `MIN_QUOTE_CHARS` = 15), and to `closed` when the deadline it read has passed.
  Both passes call it — `checker.combine_chunk_results` and `review.review_row`. Never re-implement it in one pass.
- **The form outranks the page.** Organisers leave "applications are open" up for months. `links.check_urls_in_browser`
  opens application links in headless Chromium and reports `redirected (<path>)` or `form closed`; a call whose forms
  are all closed becomes `closed` (`check_rows`), and `review.form_state` does the same during the second pass.
- **One model on the GPU at a time**, enforced by `acquire_gpu`/`release_gpu`. The 3B server is stopped before the 7B
  one starts, which is why `review.py` fetches *all* pages before loading its model. `LlamaServer.__init__` releases
  the GPU on any failure, including Ctrl+C while the model loads.
- **Reuse is keyed by the model's file name**, not the date: a site is skipped if `reports/<site>.json` or
  `reviews/<site>.json` was written by the same GGUF. Changing models re-checks everything, which is intended. A site
  the model never actually classified is not marked as checked, so unreachable sites are retried.
- **`STATUSES` in batch.py is the order of positivity** (`open`, `needs_review`, `closed`, `none`) and `summary.py`
  extends it. `classifier.STATUS_RANK` is the *page-level* order and includes `not_yet_open` and `unclear`, which never
  reach a CSV. Don't merge the two.
- **"Couldn't check" is never reported as "no".** `checker.build_report` turns a blocked homepage, a failed
  classification or an exhausted crawl budget into `needs_review` with the reason attached.

## Crawling, in the order it happens

robots.txt and sitemaps (following indexes, gzip, ignoring lastmod dates that are all identical) → a priority-first
crawl (strong keyword hit 3, likely page 2, shallow 1) → static fetch, re-rendered in Chromium when the HTML looks
thin, blocked or JS-driven → `remove_boilerplate`, which drops lines appearing on 60% of pages so a menu item labelled
"Open Call" stops counting as a hit on every page → `select_pages_for_llm` → chunks of `CHUNK_CHARS` with overlap,
`MAX_CHUNKS_PER_PAGE` of them.

`MAX_CRAWL_SECONDS` exists because one JavaScript site took 500 s. Unvisited promising pages are reported as a warning
rather than silently dropped.

## Regexes have one home

`STRONG_RE` (EN/DE/FR/IT call wording), `LIKELY_PAGE_RE`, `FORM_URL_RE` (hosted form services) and `FORM_CLOSED_RE`
live in `checker.py` and are imported by `links.py` and `review.py`. `FORM_CLOSED_RE` allows the form's own name
between the words, because Google Forms writes "this form <name> is now closed" — a version requiring the words to be
adjacent missed real closed forms.

## Costs

The 3B pass classifies every site; the 7B pass only sites found `open` or `needs_review`. A Europe-wide check runs for
hours on the GPU. Benchmark numbers for the backends are in the `classifier.py` docstring — 4 server slots was the
fastest; 8 gave nothing back.

## Tests

`test_open_calls.py` (fake model, fake pages), `test_dates.py`, `test_stopping.py`. `pytest -m llm` runs both models
end to end, ~2 minutes, and needs the GPU.
