/*
The runs shown beside the scraper and model forms: what each run did, how far it got, and a window with its figures,
its errors and its output when you click it. A run can be stopped from here, and started again with the same options.

The window is built once and then patched in place, because it refreshes every couple of seconds: replacing its
contents would move the page under the reader and throw away where they had scrolled to in the output.
*/

const RUN_VIEWS = {scraper: "scrape", openCalls: "open_calls"};
const MAX_LOG_BATCHES = 400;   /* Output kept in the window; older lines are dropped as new ones arrive */
let runsByView = {};
let openRunId = null;

/* The open window: its nodes, how much of the job's output it already has, and which errors are expanded. */
const runWindow = {nodes: null, logOffset: 0, shown: {}, openErrors: new Set()};

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function setProgress(row, run) {
  const bar = row.firstChild;
  const progress = run.progress || {};
  const counted = progress.total ? `${progress.done}/${progress.total} ${progress.unit}` : '';
  bar.className = `progress ${run.state}`;
  bar.firstChild.style.width = `${run.percent}%`;
  row.lastChild.textContent = run.state === 'running'
    ? `${run.percent}% · ${counted}` : `${run.state}${counted ? ` · ${counted}` : ''}`;
}

function progressBar(run) {
  const row = element('div', {className: 'progress-row'}, [
    element('div', {className: 'progress'}, [element('div', {className: 'progress-fill'})]),
    element('span', {className: 'progress-label'}),
  ]);
  setProgress(row, run);
  return row;
}

function runNode(run) {
  const stats = run.stats.slice(0, 4)
    .map(stat => `${Number(stat.value).toLocaleString()} ${stat.label.toLowerCase()}`).join(' · ');
  const node = element('div', {className: `run${run.id === openRunId ? ' open-run' : ''}`,
    title: 'Click for this run\'s summary, errors and output'}, [
    element('div', {className: 'run-head'}, [
      element('strong', {textContent: run.label}),
      element('span', {className: `state ${run.state}`, textContent: run.state}),
    ]),
    element('div', {className: 'run-meta', textContent:
      `${run.started.replace('T', ' ')} · ${duration(run.seconds)}${run.source === 'log' ? ' · from its log' : ''}`}),
    element('div', {className: 'run-id mono', textContent: run.id}),
    progressBar(run),
    element('div', {className: 'run-stats', textContent: stats || 'nothing recorded yet'}),
  ]);
  if (run.errors.length) {
    node.append(element('div', {className: 'run-errors', textContent:
      `${run.errors.length} error${run.errors.length > 1 ? 's' : ''}`}));
  }
  node.onclick = () => showRun(run.id);
  return node;
}

function renderRuns(view) {
  const panel = document.getElementById(`runs-${view}`);
  if (!panel) return;
  const runs = runsByView[view] || [];
  const isScraper = view === 'scraper';
  panel.replaceChildren(element('div', {className: 'panel-head'}, [
    element('div', {}, [
      element('h2', {textContent: isScraper ? 'Scraper runs' : 'Check runs'}),
      element('p', {className: 'panel-note', textContent:
        'Newest first. Click a run for its summary, errors and output; a run can be stopped or started again.'}),
    ]),
  ]));
  panel.append(...(runs.length ? runs.map(runNode)
    : [element('div', {className: 'empty', textContent: 'No runs yet'})]));
}

async function refreshRuns(view) {
  const kind = RUN_VIEWS[view];
  if (!kind) return;
  try {
    const {runs} = await api(`/api/runs?kind=${kind}`);
    runsByView[view] = runs;
    renderRuns(view);
    if (openRunId) updateRunWindow(runs.find(run => run.id === openRunId));
  } catch (error) {
    /* The server may be busy; the next tick tries again. */
  }
}

let runsTimer = null;

function watchRuns(view) {
  clearInterval(runsTimer);
  if (!RUN_VIEWS[view]) return;
  refreshRuns(view);
  let wasRunning = false;
  runsTimer = setInterval(async () => {
    await refreshRuns(view);
    const running = (runsByView[view] || []).some(run => run.state === 'running');
    /* Once more after the last run ends, so the figures at the top settle */
    if (running || wasRunning) {
      const stats = await api(view === 'scraper' ? '/api/stats/scrape' : '/api/stats/llm');
      renderStats(`stats-${view}`, stats.stats);
    }
    wasRunning = running;
  }, 2000);
}

/* One error, with its id and kind on the line and everything known about it underneath when you click it. */
function errorNode(error) {
  const detail = element('div', {className: 'error-detail'});
  detail.hidden = !runWindow.openErrors.has(error.id);

  const facts = [['Error id', error.id], ['Type', error.type], ['When', error.time],
    ['Reported by', error.source], ['Site', error.site]].filter(([, value]) => value);
  const rows = facts.flatMap(([label, value]) => [
    element('dt', {textContent: label}),
    element('dd', {}, [label === 'Site' ? link(value) : document.createTextNode(value)]),
  ]);
  const report = error.report;
  if (report) {
    rows.push(element('dt', {textContent: 'Site status'}),
      element('dd', {textContent: `${report.status}${report.status_reason ? ` — ${report.status_reason}` : ''}`}));
    if (report.errors.length) {
      rows.push(element('dt', {textContent: 'What its report says'}),
        element('dd', {}, [element('div', {className: 'mono', textContent: report.errors.join('\n')})]));
    }
    rows.push(element('dt', {textContent: 'Report'}), element('dd', {className: 'mono', textContent: report.path}));
  }
  detail.append(element('dl', {className: 'detail-grid'}, rows),
    element('div', {className: 'error-line mono', textContent: error.line}));

  const head = element('div', {className: 'error-head'}, [
    element('span', {className: 'error-id mono', textContent: error.label}),
    element('span', {className: 'error-type', textContent: error.type}),
    element('span', {className: 'error-message', textContent: error.message}),
  ]);
  head.onclick = () => {
    detail.hidden = !detail.hidden;
    if (detail.hidden) runWindow.openErrors.delete(error.id);
    else runWindow.openErrors.add(error.id);
  };
  return help(element('div', {className: 'run-error'}, [head, detail]),
    'Click to see this error\'s id, what kind it is, and what the site\'s own report recorded.');
}

/* The window's parts, made once. updateRunWindow then only changes what has actually changed. */
function buildRunWindow() {
  const nodes = {
    note: element('p', {className: 'panel-note warn'}),
    progress: progressBar({state: 'running', percent: 0, progress: {}}),
    stats: element('div', {className: 'stat-grid small'}),
    optionsHead: element('h3', {textContent: 'Options'}),
    options: element('div', {className: 'run-arguments'}),
    errorsHead: element('h3', {textContent: 'Errors'}),
    errors: element('div', {className: 'run-error-list'}),
    logHead: element('h3', {textContent: 'Output'}),
    log: element('div', {className: 'job-log'}),
    actions: element('div', {className: 'window-actions'}),
  };
  document.getElementById('run-window-body').replaceChildren(
    nodes.note, nodes.progress, nodes.stats, nodes.optionsHead, nodes.options,
    nodes.errorsHead, nodes.errors, nodes.logHead, nodes.log, nodes.actions);
  runWindow.nodes = nodes;
  runWindow.logOffset = 0;
  runWindow.shown = {};
}

function updateStats(run) {
  const {nodes, shown} = runWindow;
  const signature = JSON.stringify(run.stats);
  if (shown.stats === signature) return;
  shown.stats = signature;
  nodes.stats.replaceChildren(...run.stats.map(stat => element('div', {className: 'stat-card'}, [
    element('div', {className: 'label', textContent: stat.label}),
    element('div', {className: 'value', textContent: Number(stat.value).toLocaleString()}),
  ])));
}

function updateErrors(run) {
  const {nodes, shown} = runWindow;
  nodes.errorsHead.textContent = `Errors (${run.errors.length})`;
  const signature = run.errors.map(error => error.id).join();
  if (shown.errors === signature) return;
  shown.errors = signature;
  nodes.errors.replaceChildren(...(run.errors.length ? run.errors.map(errorNode)
    : [element('p', {className: 'panel-note', textContent: 'None'})]));
}

function updateActions(run) {
  const {nodes, shown} = runWindow;
  const signature = `${run.can_stop}-${run.can_restart}-${run.state}`;
  if (shown.actions === signature) return;
  shown.actions = signature;

  const buttons = [];
  if (run.can_stop) {
    const stop = help(element('button', {className: 'button', textContent: 'Stop this run'}),
      'Stop this run. Everything finished so far is kept, and starting it again carries on from there.');
    stop.onclick = () => runAction(stop, '/api/runs/stop', run.id, 'Stopping. Everything finished so far is saved.');
    buttons.push(stop);
  }
  if (run.can_restart && run.state !== 'running') {
    const again = help(element('button', {className: 'button primary', textContent: 'Start again'}),
      'Start this run again with the same options. Websites already checked by the same model are reused, so a '
      + 'stopped run carries on rather than starting over.');
    again.onclick = () => runAction(again, '/api/runs/restart', run.id, 'Started again. It is at the top of the list.');
    buttons.push(again);
  }
  nodes.actions.replaceChildren(...buttons);
}

async function runAction(button, path, runId, message) {
  button.disabled = true;
  try {
    await api(path, {id: runId});
    toast(message);
    runWindow.shown.actions = null;
    await refreshRuns(settings.read('view', 'festivals'));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

/* Only the output that is new since the last refresh is added, so the reader keeps their place in it. */
async function updateLog(run) {
  const {nodes} = runWindow;
  const hasLog = Boolean(run.job_id);
  nodes.logHead.hidden = nodes.log.hidden = !hasLog;
  if (!hasLog) return;
  try {
    const {lines, offset} = await api(`/api/jobs/log?id=${run.job_id}&offset=${runWindow.logOffset}`);
    if (!lines.length || openRunId !== run.id) return;
    const first = runWindow.logOffset === 0;
    runWindow.logOffset = offset;
    /* Follow the output only while the reader is already at the end of it */
    const atEnd = nodes.log.scrollHeight - nodes.log.scrollTop - nodes.log.clientHeight < 40;
    nodes.log.append(document.createTextNode(`${lines.join('\n')}\n`));
    while (nodes.log.childNodes.length > MAX_LOG_BATCHES) nodes.log.firstChild.remove();
    if (first || atEnd) nodes.log.scrollTop = nodes.log.scrollHeight;
  } catch (error) {
    /* The job may have just ended; the next refresh settles it. */
  }
}

function updateRunWindow(run) {
  if (!run || !runWindow.nodes) return;
  const {nodes} = runWindow;
  document.getElementById('run-window-kind').textContent = run.kind === 'scrape' ? 'RA scraper run' : 'Open call run';
  document.getElementById('run-window-title').textContent = run.label;
  document.getElementById('run-window-id').textContent = run.id;
  document.getElementById('run-window-when').textContent =
    `${run.started.replace('T', ' ')} → ${(run.finished || 'running').replace('T', ' ')} · ${duration(run.seconds)}` +
    (run.source === 'log' ? ' · rebuilt from its log file' : '');

  nodes.note.textContent = run.state_note || '';
  nodes.note.hidden = !run.state_note;
  setProgress(nodes.progress, run);
  updateStats(run);

  const options = Object.entries(run.arguments || {}).map(([name, value]) => `${name}: ${value}`).join(' · ');
  nodes.options.textContent = options;
  nodes.optionsHead.hidden = nodes.options.hidden = !options;

  updateErrors(run);
  updateActions(run);
  updateLog(run);
}

async function showRun(runId) {
  const listed = Object.values(runsByView).flat().find(item => item.id === runId);
  if (!listed) return;
  openRunId = runId;
  runWindow.openErrors.clear();
  buildRunWindow();
  updateRunWindow(listed);
  document.getElementById('run-window').hidden = false;
  try {
    /* The full run also carries what each error's site recorded, which the list leaves out */
    const {run} = await api(`/api/runs/detail?id=${encodeURIComponent(runId)}`);
    if (openRunId === runId) {
      runWindow.shown.errors = null;
      updateRunWindow(run);
    }
  } catch (error) {
    /* The summary from the list is already shown. */
  }
}

function closeRunWindow() {
  openRunId = null;
  runWindow.nodes = null;
  document.getElementById('run-window').hidden = true;
}

function setUpRunWindow() {
  document.getElementById('run-window-close').onclick = closeRunWindow;
  document.getElementById('run-window').onclick = event => {
    if (event.target.id === 'run-window') closeRunWindow();
  };
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeRunWindow();
  });
}
