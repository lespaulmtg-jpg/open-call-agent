/*
The forms that start a scraper or a check, built from the options the server declares. What a run then did is shown
by the run list in runs.js.
*/

/* What each option does, shown when you hover over it for a second */
const FIELD_HELP = {
  where: 'How much to cover: one city, one country, or a whole continent.',
  start: 'First date to scrape, included.',
  end: 'Last date to scrape, included.',
  festivals: 'Also write a festivals CSV per city: the events lasting more than 24 hours.',
  save_raw: "Keep RA's full answer for each festival as JSON in data/raw_events, so a column added later can be filled "
    + 'without scraping again.',
  festivals_only: 'Only check the websites of festivals, not of every event.',
  limit: 'Stop after this many websites. Useful for a trial run.',
  no_review: 'Run only the 3B model and skip the 7B review of what it found.',
  no_reuse: 'Check websites again even if the same model already checked them. Slower, but picks up changed pages.',
  ignore_robots: "Fetch pages even where robots.txt asks automated visitors not to. Some festival sites block "
    + 'everything by default.',
  min_page_date: 'Skip pages last changed before this date. Pages without a date are read anyway.',
  crawl_workers: 'How many websites are crawled at the same time. Crawling is waiting on networks, so more is faster.',
  llm_slots: 'How many page chunks the 3B model works on at once.',
  review_slots: 'How many page chunks the 7B model works on at once. It is bigger, so fewer fit.',
  period: 'The scrape period in the festival CSV names, e.g. 2025-01-01_2026-12-31.',
  summary_only: 'Do not check anything; only rebuild the summary tables from the latest results.',
};

function formField(field) {
  if (field.type === 'checkbox') {
    const input = element('input', {type: 'checkbox', checked: Boolean(field.default), id: `field-${field.name}`});
    input.dataset.field = field.name;
    return help(element('label', {className: 'field check'}, [input, element('span', {textContent: field.label})]),
      FIELD_HELP[field.name] || field.label);
  }

  if (field.type === 'choice') {
    const select = element('select', {}, field.options.map(option =>
      element('option', {value: option.value, textContent: option.label, selected: option.value === field.default})));
    select.dataset.field = field.name;
    const wrapper = help(element('label', {className: 'field'}, [element('span', {textContent: field.label}), select]),
      FIELD_HELP[field.name] || field.label);

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
  return help(element('label', {className: 'field'}, [element('span', {textContent: field.label}), input]),
    FIELD_HELP[field.name] || field.label);
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
  const start = help(element('button', {className: 'button primary', type: 'submit', textContent: `Start: ${type.label}`}),
    `Start this run now. It appears at the top of the list on the right, with a progress bar.${type.gpu ?
      ' Only one model run can use the GPU at a time.' : ''}`);
  form.replaceChildren(...type.fields.map(formField),
    element('div', {className: 'form-actions'}, [start]),
    type.gpu ? element('p', {className: 'panel-note', textContent: 'Uses the GPU: only one model job runs at a time.'}) : '');

  form.onsubmit = async event => {
    event.preventDefault();
    start.disabled = true;
    try {
      const {job} = await api('/api/jobs/start', {kind, options: readForm(form)});
      toast(`Started ${job.label}. It is at the top of the list, with a progress bar.`);
      await refreshRuns(kind === 'scrape' ? 'scraper' : 'openCalls');
    } catch (error) {
      toast(error.message, true);
    } finally {
      start.disabled = false;
    }
  };
}
