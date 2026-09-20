/* Wires the sidebar to the three views and loads each view's data. */

let config = {statuses: [], job_types: {}, today: new Date().toISOString().slice(0, 10)};

async function showView(view) {
  for (const button of document.querySelectorAll('.nav-button')) {
    button.classList.toggle('active', button.dataset.view === view);
  }
  for (const section of document.querySelectorAll('.view')) {
    section.hidden = section.id !== `view-${view}`;
  }
  settings.write('view', view);
  watchRuns(view);

  try {
    if (view === 'festivals' && !table.rows.length) await loadFestivals(document.getElementById('source-select').value);
    if (view === 'scraper') renderStats('stats-scraper', (await api('/api/stats/scrape')).stats);
    if (view === 'openCalls') renderStats('stats-openCalls', (await api('/api/stats/llm')).stats);
  } catch (error) {
    toast(error.message, true);
  }
}

function setUpSources() {
  const select = document.getElementById('source-select');
  select.replaceChildren(...config.sources.map(source =>
    element('option', {value: source.id, textContent: `${source.label} · ${source.updated.slice(0, 10)}`})));
  if (!config.sources.length) select.replaceChildren(element('option', {textContent: 'No results yet'}));

  const remembered = settings.read('source', null);
  select.value = config.sources.some(source => source.id === remembered) ? remembered : (config.default_source || '');

  select.onchange = async () => {
    settings.write('source', select.value);
    try {
      await loadFestivals(select.value);
    } catch (error) {
      toast(error.message, true);
    }
  };
  document.getElementById('reload-festivals').onclick = async () => {
    try {
      await loadFestivals(select.value);
      toast('Reloaded from disk');
    } catch (error) {
      toast(error.message, true);
    }
  };
}

function setUpTabs() {
  const tabs = document.getElementById('open-call-tabs');
  tabs.onclick = event => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    for (const other of tabs.querySelectorAll('.tab')) other.classList.toggle('active', other === tab);
    for (const kind of Object.keys(config.job_types)) {
      const form = document.getElementById(`form-${kind}`);
      if (form && kind.startsWith('check')) form.hidden = kind !== tab.dataset.kind;
    }
  };
}

async function start() {
  try {
    config = await api('/api/config');
  } catch (error) {
    toast(`Could not reach the dashboard server: ${error.message}`, true);
    return;
  }
  document.getElementById('data-dir').textContent = config.data_dir;
  setUpSources();
  setUpTable();
  setUpTabs();
  setUpTooltips();
  setUpRunWindow();
  for (const kind of Object.keys(config.job_types)) buildJobForm(kind);
  for (const button of document.querySelectorAll('.nav-button')) {
    button.onclick = () => showView(button.dataset.view);
  }
  await showView(settings.read('view', 'festivals'));
}

start();
