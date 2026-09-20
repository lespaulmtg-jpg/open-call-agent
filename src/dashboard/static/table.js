/*
The festivals table: sortable and draggable columns, filters, and a row that expands into the festival's details and
your own status and note. Rows are added in pages of 100 as you scroll, so a table of thousands stays quick.
*/

const COLUMNS = [
  {key: 'festival', label: 'Festival', className: 'festival-name'},
  {key: 'month', label: 'Month', sortKey: 'start_date'},
  {key: 'status', label: 'Status'},
  {key: 'open_call_url', label: 'Submissions URL', type: 'link', linkText: 'Submit'},
  {key: 'festival_url', label: 'Festival URL', type: 'link', linkText: 'Website'},
  {key: 'source_url', label: 'Source', type: 'link', linkText: 'RA'},
  {key: 'country', label: 'Country'},
  {key: 'city', label: 'City', className: 'mono-cell'},
  {key: 'deadline', label: 'Submissions Deadline', className: 'mono-cell'},
];
const PAGE_SIZE = 100;

const table = {
  rows: [],
  notes: {},
  visible: [],
  shown: PAGE_SIZE,
  expanded: new Set(),
  source: null,
  today: new Date().toISOString().slice(0, 10),
  order: settings.read('columns', COLUMNS.map(column => column.key)),
  sort: settings.read('sort', {key: 'deadline', direction: 1}),
  filters: settings.read('filters', {search: '', status: '', note: '', deadline: '', country: '', city: ''}),
};

function columnsInOrder() {
  const known = table.order.filter(key => COLUMNS.some(column => column.key === key));
  const missing = COLUMNS.filter(column => !known.includes(column.key)).map(column => column.key);
  return [...known, ...missing].map(key => COLUMNS.find(column => column.key === key));
}

function noteOf(row) {
  return table.notes[row.id] || {status: '', note: ''};
}

function matchesFilters(row) {
  const {search, status, note, deadline, country, city} = table.filters;
  if (country && row.country !== country) return false;
  if (city && row.city !== city) return false;
  if (status && row.status !== status) return false;
  if (note && noteOf(row).status !== note) return false;
  if (deadline === 'upcoming' && !(row.deadline && row.deadline >= table.today)) return false;
  if (deadline === 'past' && !(row.deadline && row.deadline < table.today)) return false;
  if (deadline === 'none' && row.deadline) return false;
  if (search) {
    const text = [row.festival, row.country, row.city, row.month, row.status, row.website, row.promoters, noteOf(row).note]
      .join(' ').toLowerCase();
    if (!text.includes(search.toLowerCase())) return false;
  }
  return true;
}

function sortValue(row, column) {
  const value = row[column.sortKey || column.key] || '';
  return typeof value === 'string' ? value.toLowerCase() : value;
}

function applyFilters(keepShown = false) {
  const column = COLUMNS.find(item => item.key === table.sort.key) || COLUMNS[0];
  table.visible = table.rows.filter(matchesFilters).sort((left, right) => {
    const a = sortValue(left, column);
    const b = sortValue(right, column);
    // Empty deadlines and dates sort last, whichever way the column is sorted
    if (a === '' || b === '') return a === b ? 0 : (a === '' ? 1 : -1);
    return a > b ? table.sort.direction : a < b ? -table.sort.direction : 0;
  });
  if (!keepShown) table.shown = PAGE_SIZE;
  renderTable();
}

function renderHead() {
  const head = document.getElementById('festival-head');
  head.replaceChildren(...columnsInOrder().map(column => {
    const sorted = table.sort.key === column.key;
    const cell = element('th', {draggable: true, className: sorted ? 'sorted' : ''}, [column.label]);
    help(cell, `${COLUMN_HELP[column.key] || ''} Click to sort, drag to move.`.trim());
    if (sorted) cell.append(element('span', {className: 'sort-mark', textContent: table.sort.direction === 1 ? '↑' : '↓'}));
    cell.dataset.key = column.key;

    cell.onclick = () => {
      table.sort = {key: column.key, direction: table.sort.key === column.key ? -table.sort.direction : 1};
      settings.write('sort', table.sort);
      applyFilters();
    };
    cell.ondragstart = event => {
      event.dataTransfer.setData('text/plain', column.key);
      event.dataTransfer.effectAllowed = 'move';
    };
    cell.ondragover = event => {
      event.preventDefault();
      cell.classList.add('drag-over');
    };
    cell.ondragleave = () => cell.classList.remove('drag-over');
    cell.ondrop = event => {
      event.preventDefault();
      cell.classList.remove('drag-over');
      const dragged = event.dataTransfer.getData('text/plain');
      if (!dragged || dragged === column.key) return;
      const keys = columnsInOrder().map(item => item.key).filter(key => key !== dragged);
      keys.splice(keys.indexOf(column.key), 0, dragged);
      table.order = keys;
      settings.write('columns', keys);
      renderTable();
    };
    return cell;
  }));
}

function cellContent(row, column) {
  if (column.type === 'link') return link(row[column.key], row[column.key] ? column.linkText : '');
  if (column.key === 'status') {
    const note = noteOf(row).status;
    const wrapper = element('span', {}, [badge(row.status)]);
    if (note) wrapper.append(element('span', {className: 'badge note', textContent: note.replace(/_/g, ' ')}));
    return wrapper;
  }
  if (!row[column.key]) return element('span', {className: 'muted', textContent: '—'});
  return document.createTextNode(row[column.key]);
}

function detailValue(value) {
  /* Several URLs or texts can share one field, separated by " | " or ", ". */
  const parts = value.split(value.includes(' | ') ? ' | ' : ', ').filter(Boolean);
  if (!parts.every(part => part.startsWith('http'))) return [value];
  return parts.flatMap((part, index) => index ? [element('br'), link(part)] : [link(part)]);
}


function detailRow(row, columns) {
  const details = row.details || {};
  const skip = new Set(['Festival', 'Open Call Status', 'Country Code']);
  const fields = Object.entries(details).filter(([label, value]) => value !== '' && value !== null && !skip.has(label));

  const information = element('div', {}, [
    element('h3', {textContent: 'What the scrapers found'}),
    element('dl', {className: 'detail-grid'}, fields.flatMap(([label, value]) => [
      element('dt', {textContent: label}),
      element('dd', {}, detailValue(String(value))),
    ])),
  ]);

  const note = noteOf(row);
  const select = help(element('select', {}, config.statuses.map(status =>
    element('option', {value: status.value, textContent: status.label, selected: status.value === note.status}))),
    'Your own status for this festival. It is saved against the festival, so it survives new scrapes and editions.');
  const text = element('textarea', {placeholder: 'Your notes: who you contacted, what you sent, ...', value: note.note});
  const saved = element('span', {className: 'note-saved', textContent: note.updated ? `Saved ${note.updated.replace('T', ' ')}` : ''});
  const save = help(element('button', {className: 'button primary', textContent: 'Save'}),
    'Save your status and note to data/dashboard/notes.json.');

  save.onclick = async event => {
    event.stopPropagation();
    save.disabled = true;
    try {
      const result = await api('/api/notes', {id: row.id, status: select.value, note: text.value, source: table.source});
      table.notes = result.notes;
      renderStats('stats-festivals', result.stats);
      saved.textContent = 'Saved just now';
      applyFilters(true);
    } catch (error) {
      toast(error.message, true);
    } finally {
      save.disabled = false;
    }
  };

  const yours = element('div', {}, [
    element('h3', {textContent: 'Your status'}),
    element('div', {className: 'note-status'}, [select, save, saved]),
    text,
  ]);

  const cell = element('td', {className: 'detail-cell', colSpan: columns.length}, [
    element('div', {className: 'detail'}, [yours, information]),
  ]);
  const detail = element('tr', {}, [cell]);
  detail.onclick = event => event.stopPropagation();
  return detail;
}

function renderTable() {
  renderHead();
  const columns = columnsInOrder();
  const body = document.getElementById('festival-body');
  const rows = table.visible.slice(0, table.shown);

  if (!rows.length) {
    body.replaceChildren(element('tr', {}, [element('td', {className: 'empty', colSpan: columns.length,
      textContent: table.rows.length ? 'No festivals match these filters' : 'No data yet: run a scrape and an open call check'})]));
  } else {
    const nodes = [];
    for (const row of rows) {
      const expanded = table.expanded.has(row.id);
      const tr = element('tr', {className: `row${expanded ? ' open-row' : ''}`},
        columns.map(column => element('td', {className: column.className || ''}, [cellContent(row, column)])));
      tr.onclick = () => {
        if (table.expanded.has(row.id)) table.expanded.delete(row.id);
        else table.expanded.add(row.id);
        renderTable();
      };
      nodes.push(tr);
      if (expanded) nodes.push(detailRow(row, columns));
    }
    body.replaceChildren(...nodes);
  }

  document.getElementById('row-count').textContent =
    `${table.visible.length.toLocaleString()} of ${table.rows.length.toLocaleString()} festivals` +
    (table.visible.length > rows.length ? `, showing ${rows.length.toLocaleString()}` : '');
  document.getElementById('load-more').hidden = table.visible.length <= rows.length;
}

function showMore() {
  if (table.shown >= table.visible.length) return;
  table.shown += PAGE_SIZE;
  renderTable();
}

async function loadFestivals(source) {
  const query = source ? `?source=${encodeURIComponent(source)}` : '';
  const data = await api(`/api/festivals${query}`);
  table.rows = data.rows;
  table.notes = data.notes;
  table.source = data.source;
  table.today = config.today;
  renderStats('stats-festivals', data.stats);

  const statusFilter = document.getElementById('status-filter');
  const statuses = [...new Set(data.rows.map(row => row.status))].sort();
  statusFilter.replaceChildren(element('option', {value: '', textContent: 'All statuses'}),
    ...statuses.map(status => element('option', {value: status, textContent: status.replace(/_/g, ' ')})));
  statusFilter.value = table.filters.status;

  const noteFilter = document.getElementById('note-filter');
  noteFilter.replaceChildren(element('option', {value: '', textContent: 'All my statuses'}),
    ...config.statuses.filter(status => status.value).map(status =>
      element('option', {value: status.value, textContent: status.label})));
  noteFilter.value = table.filters.note;
  fillPlaceFilters();

  applyFilters();
}

function fillOptions(select, values, allLabel, selected) {
  select.replaceChildren(element('option', {value: '', textContent: allLabel}),
    ...values.map(value => element('option', {value, textContent: value})));
  select.value = values.includes(selected) ? selected : '';
  return select.value;
}

function fillPlaceFilters() {
  const countrySelect = document.getElementById('country-filter');
  const citySelect = document.getElementById('city-filter');
  const countries = [...new Set(table.rows.map(row => row.country))].filter(Boolean).sort();
  table.filters.country = fillOptions(countrySelect, countries, 'All countries', table.filters.country);

  /* Only the cities of the chosen country, or every city when no country is chosen. */
  const inCountry = table.rows.filter(row => !table.filters.country || row.country === table.filters.country);
  const cities = [...new Set(inCountry.map(row => row.city))].filter(Boolean).sort();
  table.filters.city = fillOptions(citySelect, cities, 'All cities', table.filters.city);
}


function setUpTable() {
  const search = document.getElementById('search');
  const countryFilter = document.getElementById('country-filter');
  const cityFilter = document.getElementById('city-filter');
  const statusFilter = document.getElementById('status-filter');
  const noteFilter = document.getElementById('note-filter');
  const deadlineFilter = document.getElementById('deadline-filter');
  search.value = table.filters.search;
  deadlineFilter.value = table.filters.deadline;

  const update = () => {
    table.filters = {search: search.value, status: statusFilter.value, note: noteFilter.value,
      deadline: deadlineFilter.value, country: countryFilter.value, city: cityFilter.value};
    fillPlaceFilters();
    settings.write('filters', table.filters);
    applyFilters();
  };
  search.oninput = update;
  countryFilter.onchange = () => {
    /* A new country can make the chosen city impossible, so it is cleared */
    cityFilter.value = '';
    update();
  };
  cityFilter.onchange = update;
  statusFilter.onchange = update;
  noteFilter.onchange = update;
  deadlineFilter.onchange = update;

  document.getElementById('reset-table').onclick = () => {
    table.filters = {search: '', status: '', note: '', deadline: '', country: '', city: ''};
    table.order = COLUMNS.map(column => column.key);
    table.sort = {key: 'deadline', direction: 1};
    settings.write('columns', table.order);
    settings.write('sort', table.sort);
    search.value = statusFilter.value = noteFilter.value = deadlineFilter.value = '';
    countryFilter.value = cityFilter.value = '';
    update();
  };

  document.getElementById('load-more').onclick = showMore;
  window.addEventListener('scroll', () => {
    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 400) showMore();
  });
}
