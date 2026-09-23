# open_call_agent/ra/

Everything that knows about ra.co. Three modules, and nothing outside this folder should speak GraphQL or know RA's
field names.

- **[client.py](client.py)** — the GraphQL client: countries, areas, event listings.
- **[areas.py](areas.py)** — which areas to scrape, and which continent a country is in (`continents.json`).
- **[events.py](events.py)** — turning an event into a CSV row, and reading and writing those CSVs.

## client.py

- One second between requests (`DELAY`), four retries with backoff on 429/5xx and connection errors. GraphQL errors
  raise immediately — they don't get better on retry.
- **RA silently caps a query at 10,000 listings** (`LISTINGS_LIMIT`). `_collect_events` notices the cap on page 1 and
  splits the date range in half recursively; only when the range is a single day does it warn that events are missing.
  Don't "simplify" that recursion away.
- A multi-day event is listed once per day, so events are de-duplicated by id while paging.
- `promotionalLinks { title url }` on the Event is what gives a festival its own website. It is the field this project
  overlooked at first, and the reason festivals were showing their promoter's site; everything downstream depends on
  it. RA's Event type has ~82 fields — add one here when you need it rather than scraping a page.

## areas.py

- `root_areas` returns the areas whose parent isn't in the same country. Their listings already include child areas, so
  scraping the roots covers the country exactly once. A country's "All" area becomes the folder `other`
  (`area_folder`), which holds events outside the listed cities.

## events.py

- **`CSV_COLUMNS` is the contract** with the rest of the pipeline. Adding a column means older CSVs lack it, so either
  read it with `row.get(...)` or backfill with `scripts/backfill_event_links.py`, which refills from
  `data/raw_events/` where a raw response was saved and asks RA only for the rest.
- **`get_event_website` ranks the links an organiser wrote by hand**, because the titles are free text:
  1. the title contains `site`, `web` or `link` (`WEBSITE_TITLE_WORDS`) — "Website", "FOG website", "Linktree";
  2. the title or the host is the festival's own name (`names_match`, on letters and digits only, years stripped,
     both names at least 5 characters, either containing the other);
  3. anything else, in RA's order.
  Ties are broken by the link's position, never by URL text. Social media and chat apps (`NON_WEBSITE_DOMAINS`) and
  ticket or merch links (`TICKET_LINK_WORDS`, matched in the title *and* the host) never qualify — but **Linktree and
  other link hubs are kept**, since some festivals publish everything through one. A link to the festival's own ticket
  page is reduced to its domain rather than dropped.
- `write_rows` builds the whole CSV in memory and writes it atomically, so a stop never truncates a file. Keep that.

## Tests

`test_scraper.py` covers the live Swiss scrape, the listing-limit split, raw JSON saving and the link ranking
(`test_event_website_is_picked_from_free_text_titles`); `test_backfill.py` uses a fake RA client, so no network. Both
real bugs in `get_event_website` — an exact-title match missing "FOG website", and a fallback picking a WhatsApp link —
were found by tests, so add a case when you touch the ranking.
