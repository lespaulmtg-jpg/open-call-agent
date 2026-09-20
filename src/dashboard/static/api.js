/* Talking to the dashboard server, and the small helpers every view uses. */

async function api(path, body) {
  const options = body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({error: `${response.status} ${response.statusText}`}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function element(tag, properties = {}, children = []) {
  const node = Object.assign(document.createElement(tag), properties);
  for (const child of [].concat(children)) node.append(child);
  return node;
}

function link(url, text) {
  if (!url) return element('span', {className: 'muted', textContent: '—'});
  return element('a', {href: url, target: '_blank', rel: 'noopener', textContent: text || url});
}

function badge(status, extraClass = '') {
  return element('span', {className: `badge ${status || 'none'} ${extraClass}`, textContent: (status || 'none').replace(/_/g, ' ')});
}

function toast(message, isError = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = `toast${isError ? ' error' : ''}`;
  node.hidden = false;
  clearTimeout(node.timer);
  node.timer = setTimeout(() => {node.hidden = true;}, isError ? 8000 : 3500);
}

function renderStats(containerId, stats) {
  const container = document.getElementById(containerId);
  container.replaceChildren(...stats.map(stat => element('div', {className: 'stat-card'}, [
    element('div', {className: 'label', textContent: stat.label}),
    element('div', {className: 'value', textContent: Number(stat.value).toLocaleString()}),
    element('div', {className: 'note', textContent: stat.note || ''}),
  ])));
}

const HELP_DELAY = 1000;   /* Hover this long before the help box appears */

/* Help boxes. An element explains itself through data-help, or through the help texts below for the parts of the
   table that are built in JavaScript. Data cells never have one: they say what they are already. */
const COLUMN_HELP = {
  festival: 'The festival, with its editions and repeat listings merged into one row.',
  month: 'When the festival takes place. Sorting uses the exact start date.',
  status: 'What the models decided: open, closed, needs review or none. A second badge is the status you set.',
  open_call_url: 'Where to apply: the application form, or the page announcing the call if the form no longer works.',
  festival_url: "The festival's own website, or its promoter's when RA has no link for the festival itself.",
  source_url: 'The ra.co listing this row was scraped from.',
  country: 'Country of the venue.',
  city: 'City of the venue, as ra.co names it.',
  deadline: 'The application deadline the models found, as YYYY-MM-DD.',
};

function help(element, text) {
  element.dataset.help = text;
  return element;
}

function setUpTooltips() {
  const tooltip = document.getElementById('tooltip');
  let timer = null;

  document.addEventListener('mouseover', event => {
    const target = event.target.closest('[data-help]');
    clearTimeout(timer);
    if (!target) {
      tooltip.hidden = true;
      return;
    }
    timer = setTimeout(() => {
      tooltip.textContent = target.dataset.help;
      tooltip.hidden = false;
      const box = target.getBoundingClientRect();
      const width = tooltip.offsetWidth;
      tooltip.style.left = `${Math.max(12, Math.min(box.left, window.innerWidth - width - 12))}px`;
      /* Above the element when there is no room below */
      const below = box.bottom + 8;
      tooltip.style.top = below + tooltip.offsetHeight > window.innerHeight
        ? `${box.top - tooltip.offsetHeight - 8}px` : `${below}px`;
    }, HELP_DELAY);
  });

  document.addEventListener('mouseout', event => {
    if (!event.relatedTarget || !event.relatedTarget.closest('[data-help]')) {
      clearTimeout(timer);
      tooltip.hidden = true;
    }
  });
}

/* Remembers table settings (column order, sort, filters) between visits. */
const settings = {
  read(key, fallback) {
    try {
      const value = localStorage.getItem(`dashboard.${key}`);
      return value === null ? fallback : JSON.parse(value);
    } catch (error) {
      return fallback;
    }
  },
  write(key, value) {
    try {
      localStorage.setItem(`dashboard.${key}`, JSON.stringify(value));
    } catch (error) {
      /* Private browsing or a full store: the dashboard works without remembering settings. */
    }
  },
};
