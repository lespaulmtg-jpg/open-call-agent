# dashboard/static/

The page. Plain HTML, CSS and JavaScript — no framework, no build step, no bundler, no npm. Edit a file, reload the
browser. Keep it that way; the whole UI is ~1,300 lines.

| File | Role |
|---|---|
| [index.html](index.html) | The four views, the run window, the tooltip and toast nodes. |
| [api.js](api.js) | `api()`, DOM helpers, tooltips, and `settings` (localStorage). Loaded first. |
| [runs.js](runs.js) | The run list, progress bars and the run window. |
| [table.js](table.js) | The festivals table: columns, filters, sorting, dragging, expanding. |
| [jobs.js](jobs.js) | The forms, built from what `/api/config` declares. |
| [datasets.js](datasets.js) | The datasets on disk, and browsing one in the festivals table. |
| [app.js](app.js) | Sidebar, view switching, startup. Loaded last, calls `start()`. |

**Scripts are plain `<script>` tags, not modules**, so everything shares one global scope and load order is the
dependency order (`index.html` bottom). A function defined in `api.js` is simply available in `table.js`. Don't add
`type="module"` for one file.

## Conventions

- Build DOM with `element(tag, properties, children)` and `link()` / `badge()`; no template strings of HTML, no
  `innerHTML`.
- `settings.read/write` wraps localStorage in try/catch, because private browsing throws. Column order, sort, filters,
  the chosen source and the last view are remembered; nothing else.
- Errors reach the user through `toast(message, true)`. Polling failures stay silent — the next tick retries.
- `renderStats(containerId, stats)` renders whatever `/api/stats/*` returns, so a new figure is a server-side change
  only.

## Help boxes

Hovering for `HELP_DELAY` (1000 ms) over anything carrying `data-help` shows the tooltip. Static elements declare it in
`index.html`; parts built in JS use `help(node, text)` with the texts in `COLUMN_HELP` (api.js) and `FIELD_HELP`
(jobs.js). **Data points never get one** — a cell saying "Switzerland" explains itself. Headings, controls, buttons and
form options do.

## Adding a table column

Add an entry to `COLUMNS` in table.js (`key`, `label`, optional `sortKey`, `type: 'link'`, `className`), a line in
`COLUMN_HELP`, and make sure `to_row` in [../sources.py](../sources.py) puts that key in the row. Everything else —
order, dragging, sorting, remembering — follows. Keep the row itself small: thousands of festivals are sent at once,
which is why full details live under `row.details` and are only rendered when a row is expanded.

## The run window is patched, never rebuilt

It refreshes every two seconds. Replacing its contents moves the page under the reader and loses their place in the
output, which made a running job's log unreadable. So `buildRunWindow` creates the nodes once and `updateRunWindow`
writes into them; each part has a `shown` signature and is only redrawn when that changes. The output is fetched by
offset and appended, following the end only while the reader is already at the end. The festivals table refreshes the
same way while a check runs: `loadFestivals(source, true)` keeps the scroll position, the expanded rows and the page
of rows already shown.

## Gotchas

- **A filter the new dataset has no values for stays in its dropdown**, labelled "(none here)". Silently dropping it
  looked like a bug: switching to a dataset with no open calls hid all 7,887 rows while the control showed nothing
  selected. The empty table says how many rows the filters are hiding and offers to clear them.
- `[hidden] { display: none !important; }` at the top of [style.css](style.css) is load-bearing: the run window's
  `display: flex` otherwise beat `hidden` and an invisible overlay swallowed every click.
- Runs are re-fetched every 2 s while a view is open, and the figures are refreshed once more after the last run ends —
  otherwise "1 running" stayed on screen after a stop.
- The server sends `Cache-Control: no-cache` for these files. If a change appears not to work, confirm with a hard
  reload before assuming the code is wrong; that mistake has been made here before.
- The table renders 100 rows at a time and adds more as you scroll, so measure any change against a full Europe
  dataset rather than the Swiss test data.
