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
