/*
The runs shown beside the scraper and model forms: what each run did, how far it got, and a window with its figures
and errors when you click it. Runs this dashboard started can be stopped from here.
*/

const RUN_VIEWS = {scraper: "scrape", openCalls: "open_calls"};
let runsByView = {};
let openRunId = null;

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function progressBar(run) {
  const bar = element('div', {className: `progress ${run.state}`},
    [element('div', {className: 'progress-fill'})]);
  bar.firstChild.style.width = `${run.percent}%`;
  const progress = run.progress || {};
  const counted = progress.total ? `${progress.done}/${progress.total} ${progress.unit}` : '';
  return element('div', {className: 'progress-row'}, [bar,
    element('span', {className: 'progress-label', textContent: run.state === 'running' ? `${run.percent}% · ${counted}`
      : `${run.state}${counted ? ` · ${counted}` : ''}`})]);
}

function runNode(run) {
  const stats = run.stats.slice(0, 4)
    .map(stat => `${Number(stat.value).toLocaleString()} ${stat.label.toLowerCase()}`).join(' · ');
  const node = element('div', {className: `run${run.id === openRunId ? ' open-run' : ''}`,
    title: 'Click for this run\'s summary and errors'}, [
    element('div', {className: 'run-head'}, [
      element('strong', {textContent: run.label}),
      element('span', {className: `state ${run.state}`, textContent: run.state}),
    ]),
    element('div', {className: 'run-meta', textContent:
      `${run.started.replace('T', ' ')} · ${duration(run.seconds)}${run.source === 'log' ? ' · from its log' : ''}`}),
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
        'Newest first. Click a run for its summary and errors; runs started here can be stopped.'}),
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

async function runLog(run) {
  /* Only a run this dashboard started has its output in memory */
  if (!run.job_id) return null;
  try {
    const {lines} = await api(`/api/jobs/log?id=${run.job_id}`);
    return lines.slice(-60).join('\n');
  } catch (error) {
    return null;
  }
}

function statTable(run) {
  return element('div', {className: 'stat-grid small'}, run.stats.map(stat => element('div', {className: 'stat-card'}, [
    element('div', {className: 'label', textContent: stat.label}),
    element('div', {className: 'value', textContent: Number(stat.value).toLocaleString()}),
  ])));
}

function updateRunWindow(run) {
  if (!run) return;
  document.getElementById('run-window-kind').textContent = run.kind === 'scrape' ? 'RA scraper run' : 'Open call run';
  document.getElementById('run-window-title').textContent = run.label;
  document.getElementById('run-window-when').textContent =
    `${run.started.replace('T', ' ')} → ${(run.finished || 'running').replace('T', ' ')} · ${duration(run.seconds)}` +
    (run.source === 'log' ? ' · rebuilt from its log file' : '');

  const body = document.getElementById('run-window-body');
  const parts = [progressBar(run), statTable(run)];

  if (Object.keys(run.arguments || {}).length) {
    parts.push(element('h3', {textContent: 'Options'}),
      element('div', {className: 'run-arguments', textContent:
        Object.entries(run.arguments).map(([name, value]) => `${name}: ${value}`).join(' · ')}));
  }
  parts.push(element('h3', {textContent: `Errors (${run.errors.length})`}));
  parts.push(run.errors.length
    ? element('ul', {className: 'run-error-list'}, run.errors.map(error => element('li', {textContent: error})))
    : element('p', {className: 'panel-note', textContent: 'None'}));

  runLog(run).then(log => {
    if (log && openRunId === run.id) {
      body.append(element('h3', {textContent: 'Output'}), element('div', {className: 'job-log', textContent: log}));
    }
  });

  if (run.job_id) {
    const stop = help(element('button', {className: 'button', textContent: 'Stop this run'}),
      'Stop this run. Everything finished so far is kept, and starting the same run again continues from there.');
    stop.onclick = async () => {
      stop.disabled = true;
      try {
        await api('/api/jobs/stop', {id: run.job_id});
        toast('Stopping. Everything finished so far is saved.');
      } catch (error) {
        toast(error.message, true);
      }
    };
    parts.push(element('div', {className: 'window-actions'}, [stop]));
  }
  body.replaceChildren(...parts);
}

function showRun(runId) {
  const run = Object.values(runsByView).flat().find(item => item.id === runId);
  if (!run) return;
  openRunId = runId;
  updateRunWindow(run);
  document.getElementById('run-window').hidden = false;
}

function closeRunWindow() {
  openRunId = null;
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
