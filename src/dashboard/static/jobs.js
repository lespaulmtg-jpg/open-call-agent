/*
Running the scrapers from the dashboard: a form built from the options the server declares, and the list of jobs with
their live log. Stopping a job is the safe stop: everything finished so far is kept.
*/

const jobLogs = {};        /* job id -> {offset, lines, open} */
let jobTimer = null;

function formField(field) {
  if (field.type === 'checkbox') {
    const input = element('input', {type: 'checkbox', checked: Boolean(field.default), id: `field-${field.name}`});
    input.dataset.field = field.name;
    return element('label', {className: 'field check'}, [input, element('span', {textContent: field.label})]);
  }

  if (field.type === 'choice') {
    const select = element('select', {}, field.options.map(option =>
      element('option', {value: option.value, textContent: option.label, selected: option.value === field.default})));
    select.dataset.field = field.name;
    const wrapper = element('label', {className: 'field'}, [element('span', {textContent: field.label}), select]);

    /* The value next to the choice, e.g. the country code for "Country", plus a second one for "City". */
    const extras = element('div', {});
    const drawExtras = () => {
      const option = field.options.find(item => item.value === select.value) || {};
      const parts = [option.with, option].filter(part => part && part.flag);
      extras.replaceChildren(...parts.map(part => {
        const input = element('input', {type: 'text', placeholder: part.placeholder || '',
          value: part.placeholder || ''});
        input.dataset.field = part.name || `${field.name}_value`;
        return element('label', {className: 'field'},
          [element('span', {textContent: part.placeholder ? `${part.label || field.label} (${part.placeholder})` : field.label}), input]);
      }));
    };
    select.onchange = drawExtras;
    drawExtras();
    return element('div', {}, [wrapper, extras]);
  }

  const input = element('input', {type: field.type === 'number' ? 'number' : field.type === 'date' ? 'date' : 'text',
    value: field.default === undefined ? '' : String(field.default), placeholder: field.placeholder || ''});
  input.dataset.field = field.name;
  return element('label', {className: 'field'}, [element('span', {textContent: field.label}), input]);
}

function readForm(form) {
  const options = {};
  for (const input of form.querySelectorAll('[data-field]')) {
    options[input.dataset.field] = input.type === 'checkbox' ? input.checked : input.value;
  }
  return options;
}

function buildJobForm(kind) {
  const form = document.getElementById(`form-${kind}`);
  const type = config.job_types[kind];
  const start = element('button', {className: 'button primary', type: 'submit', textContent: `Start: ${type.label}`});
  form.replaceChildren(...type.fields.map(formField),
    element('div', {className: 'form-actions'}, [start]),
    type.gpu ? element('p', {className: 'panel-note', textContent: 'Uses the GPU: only one model job runs at a time.'}) : '');

  form.onsubmit = async event => {
    event.preventDefault();
    start.disabled = true;
    try {
      const {job} = await api('/api/jobs/start', {kind, options: readForm(form)});
      jobLogs[job.id] = {offset: 0, lines: [], open: true};
      toast(`Started ${job.label} (job ${job.id})`);
      await refreshJobs();
    } catch (error) {
      toast(error.message, true);
    } finally {
      start.disabled = false;
    }
  };
}

function jobNode(job) {
  const log = jobLogs[job.id] || (jobLogs[job.id] = {offset: 0, lines: [], open: job.state === 'running'});
  const stop = element('button', {className: 'button', textContent: job.state === 'stopping' ? 'Stopping...' : 'Stop',
    disabled: job.state !== 'running'});
  stop.onclick = async () => {
    stop.disabled = true;
    try {
      await api('/api/jobs/stop', {id: job.id});
      toast('Stopping. Everything finished so far is saved.');
      await refreshJobs();
    } catch (error) {
      toast(error.message, true);
    }
  };

  const toggle = element('button', {className: 'button ghost', textContent: log.open ? 'Hide log' : 'Show log'});
  toggle.onclick = () => {
    log.open = !log.open;
    renderJobs();
  };

  const node = element('div', {className: 'job'}, [
    element('div', {className: 'job-row'}, [
      element('div', {}, [
        element('strong', {textContent: `${job.label} · job ${job.id} `}),
        element('span', {className: `state ${job.state}`, textContent: job.state}),
        element('div', {className: 'job-meta', textContent: `${job.started.replace('T', ' ')}${job.finished ? ` → ${job.finished.replace('T', ' ')}` : ''} · ${job.command}`}),
      ]),
      element('div', {}, [toggle, ' ', stop]),
    ]),
  ]);

  if (log.open) {
    const box = element('div', {className: 'job-log', textContent: log.lines.join('\n') || 'Waiting for output...'});
    node.append(box);
    requestAnimationFrame(() => {box.scrollTop = box.scrollHeight;});
  }
  return node;
}

let jobList = [];

function renderJobs() {
  for (const [view, kinds] of [['scraper', ['scrape']], ['openCalls', ['check-open-calls', 'check-europe']]]) {
    const panel = document.getElementById(`jobs-${view}`);
    const jobs = jobList.filter(job => kinds.includes(job.kind));
    panel.replaceChildren(element('div', {className: 'panel-head'}, [
      element('div', {}, [element('h2', {textContent: 'Jobs'}),
        element('p', {className: 'panel-note', textContent: 'Stopping keeps everything finished so far; start again to continue'})]),
    ]));
    if (!jobs.length) {
      panel.append(element('div', {className: 'empty', textContent: 'No jobs started in this session'}));
    } else {
      panel.append(...jobs.map(jobNode));
    }
  }
}

async function refreshJobs() {
  try {
    const {jobs} = await api('/api/jobs');
    jobList = jobs;
    for (const job of jobs) {
      const log = jobLogs[job.id] || (jobLogs[job.id] = {offset: 0, lines: [], open: job.state === 'running'});
      if (!log.open) continue;
      const update = await api(`/api/jobs/log?id=${job.id}&offset=${log.offset}`);
      log.offset = update.offset;
      log.lines.push(...update.lines);
      log.lines = log.lines.slice(-500);
    }
    renderJobs();
  } catch (error) {
    /* The server may be restarting: try again on the next tick. */
  }
}

function watchJobs(active) {
  clearInterval(jobTimer);
  if (!active) return;
  refreshJobs();
  let wasRunning = false;
  jobTimer = setInterval(async () => {
    await refreshJobs();
    const running = jobList.some(job => job.state === 'running' || job.state === 'stopping');
    /* Also refresh once after the last job ends, so the counts don't keep showing it as running */
    if (running || wasRunning) {
      const view = document.querySelector('.nav-button.active').dataset.view;
      const stats = view === 'scraper' ? await api('/api/stats/scrape') : view === 'openCalls' ? await api('/api/stats/llm') : null;
      if (stats) renderStats(`stats-${view}`, stats.stats);
    }
    wasRunning = running;
  }, 2000);
}
