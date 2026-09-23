/*
The datasets on disk: what each one covers, when it was written, and which steps of the pipeline have been run over it.
The steps are the point of this view — a check that was stopped leaves results that look finished until you see that
the link checks and the review never ran.
*/

const KIND_LABELS = {results: 'Check results', summary: 'Summary table', scrape: 'RA scrape'};
const STEP_MARKS = {done: '✓', partial: '◐', none: '·'};

function factChip(fact) {
  return element('span', {className: 'fact'}, [
    element('span', {className: 'fact-label', textContent: fact.label}),
    element('span', {className: 'fact-value',
      textContent: typeof fact.value === 'number' ? fact.value.toLocaleString() : fact.value}),
  ]);
}

function stepNode(step) {
  return element('li', {className: `step ${step.state}`}, [
    element('span', {className: 'step-mark', textContent: STEP_MARKS[step.state] || '·'}),
    element('span', {className: 'step-label', textContent: step.label}),
    element('span', {className: 'step-detail', textContent: step.detail}),
  ]);
}

function datasetNode(dataset) {
  const head = element('div', {className: 'dataset-head'}, [
    element('div', {}, [
      element('div', {className: 'micro-label',
        textContent: `${KIND_LABELS[dataset.kind] || dataset.kind} · ${dataset.updated.replace('T', ' ')}`}),
      element('h3', {textContent: dataset.label}),
      element('div', {className: 'run-id mono', textContent: dataset.path}),
    ]),
  ]);

  if (dataset.source_id) {
    const browse = help(element('button', {className: 'button primary', textContent: 'Browse festivals'}),
      'Show this dataset in the Festivals table. Filters that would hide all of it are cleared.');
    browse.onclick = () => browseDataset(dataset.source_id);
    head.append(browse);
  } else {
    head.append(help(element('span', {className: 'muted', textContent: 'Scraped data'}),
      'The events and festivals the checks are built from. Browse it through a check result instead.'));
  }

  return element('div', {className: 'dataset'}, [
    head,
    element('div', {className: 'dataset-facts'}, dataset.facts.map(factChip)),
    element('ul', {className: 'steps'}, dataset.steps.map(stepNode)),
  ]);
}

/* Open a dataset in the festivals table, clearing filters that would leave it looking empty. */
async function browseDataset(sourceId) {
  const select = document.getElementById('source-select');
  select.value = sourceId;
  settings.write('source', sourceId);
  await showView('festivals');
  try {
    await loadFestivals(sourceId);
    toast('Showing this dataset in the table');
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadDatasets() {
  try {
    const {datasets, stats} = await api('/api/datasets');
    renderStats('stats-datasets', stats);
    const list = document.getElementById('dataset-list');
    list.replaceChildren(...(datasets.length ? datasets.map(datasetNode)
      : [element('div', {className: 'empty', textContent: 'No data yet: run a scrape and an open call check'})]));
  } catch (error) {
    toast(error.message, true);
  }
}
