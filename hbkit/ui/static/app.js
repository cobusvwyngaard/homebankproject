'use strict';
// HomeBank toolkit — browser interface. Talks only to the local hbkit server.
// All text from the file is inserted with textContent, never as HTML.

const TOKEN = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const app = { view: null, status: null, analyses: null, categories: null, reviewRun: null, resultsRun: null, dirty: false };

// --- helpers ------------------------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'value') e.value = v;
    else if (k === 'checked') e.checked = !!v;
    else if (v === true) e.setAttribute(k, '');
    else e.setAttribute(k, String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(path, body) {
  const opts = { headers: { 'X-Hbkit-Token': TOKEN } };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch { data = { error: `The toolkit answered with error ${res.status}.` }; }
  if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
  return data;
}

let toastTimer;
function toast(message, isError) {
  const t = document.getElementById('toast');
  t.textContent = message;
  t.className = 'toast' + (isError ? ' error' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 9000 : 4000);
}

// Run an async action with a button showing that it is busy.
async function busy(button, label, fn) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = label;
  button.classList.add('busy');
  try { return await fn(); }
  catch (err) { toast(err.message, true); return undefined; }
  finally { button.disabled = false; button.textContent = old; button.classList.remove('busy'); }
}

function main(...children) {
  const m = document.getElementById('main');
  m.replaceChildren(...children.flat());
  return m;
}

function fmtDate(epoch) {
  return new Date(epoch * 1000).toLocaleString('en-ZA', { dateStyle: 'medium', timeStyle: 'short' });
}
function fmtSize(n) { return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`; }
function stampText(stamp) { return stamp.replace('_', ' ').replace(/(\d{2})(\d{2})(\d{2})(_\d+)?$/, '$1:$2:$3'); }

async function openFolder(path) {
  try { await api('/api/open', { path }); } catch (err) { toast(err.message, true); }
}

function confirmLeave() {
  return !app.dirty || confirm('You have unsaved approvals on the rule review page. Leave without saving?');
}

// --- summary (the same content the terminal shows) --------------------------------------------------

function summaryView(summary) {
  const rows = [];
  let lastNote = null;
  for (const r of summary.rows) {
    if (r.kind === 'note' && lastNote) { lastNote.textContent += ' ' + r.label; continue; }
    lastNote = null;
    const indent = /^\s+/.test(r.label) ? 'indent' : null;
    if (r.kind === 'heading') rows.push(h('tr', { class: 'section' }, h('td', { colspan: 3, text: r.label })));
    else if (r.kind === 'note') {
      lastNote = h('td', { class: 'note', colspan: 3, text: r.label });
      rows.push(h('tr', {}, lastNote));
    }
    else if (r.kind === 'output') {
      rows.push(h('tr', {}, h('td', { class: 'label', text: r.label }), h('td', { class: 'mono small path', text: r.text }),
        h('td', {}, h('button', { type: 'button', onclick: () => openFolder(r.path) }, 'Show in folder'))));
    } else if (r.kind === 'percent') {
      const fill = h('div');
      fill.style.width = `${Math.max(0, Math.min(100, r.percent))}%`;
      rows.push(h('tr', {}, h('td', { class: `label ${indent || ''}`, text: r.label.trim() }), h('td', { class: 'value', text: r.text }),
        h('td', {}, h('div', { class: 'bar' }, fill))));
    } else {
      rows.push(h('tr', {}, h('td', { class: `label ${indent || ''}`, text: r.label.trim() }), h('td', { class: 'value', text: r.text }), h('td')));
    }
  }
  return h('div', { class: 'card' }, h('h2', { text: summary.title }), h('table', { class: 'summary' }, h('tbody', {}, rows)));
}

// --- generic sortable, filterable table -------------------------------------------------------------------

function isNumeric(values) {
  const filled = values.filter(v => v !== '' && v !== null && v !== undefined);
  return filled.length > 0 && filled.every(v => !isNaN(Number(v)));
}

function dataTable(header, rows, opts = {}) {
  const pageSize = opts.pageSize || 250;
  const numericCols = header.map((_, i) => isNumeric(rows.slice(0, 200).map(r => r[i])));
  let sortCol = -1, sortDir = 1, filter = '', page = 0;
  const wrap = h('div');
  const count = h('span', { class: 'muted small' });
  const search = h('input', { type: 'search', placeholder: 'Filter rows…', oninput: e => { filter = e.target.value.toLowerCase(); page = 0; draw(); } });
  const pager = h('div', { class: 'pager' });
  const tbl = h('table', { class: 'data' });
  wrap.append(h('div', { class: 'toolbar' }, search, count), h('div', { class: 'table-wrap' }, tbl), pager);

  function draw() {
    let list = filter ? rows.filter(r => r.some(c => String(c).toLowerCase().includes(filter))) : rows.slice();
    if (sortCol >= 0) {
      const num = numericCols[sortCol];
      list.sort((a, b) => {
        const x = a[sortCol], y = b[sortCol];
        if (num) return ((x === '' ? -Infinity : Number(x)) - (y === '' ? -Infinity : Number(y))) * sortDir;
        return String(x).localeCompare(String(y), 'en-ZA') * sortDir;
      });
    }
    const pages = Math.max(1, Math.ceil(list.length / pageSize));
    page = Math.min(page, pages - 1);
    const shown = list.slice(page * pageSize, (page + 1) * pageSize);
    count.textContent = `${list.length} of ${rows.length} rows`;
    const head = h('tr', {}, header.map((name, i) => h('th', {
      class: i === sortCol ? (sortDir > 0 ? 'sorted-asc' : 'sorted-desc') : null,
      title: 'Click to sort',
      onclick: () => { sortDir = sortCol === i ? -sortDir : 1; sortCol = i; draw(); },
    }, name.replaceAll('_', ' '))));
    const body = shown.map(r => h('tr', {}, r.map((c, i) => h('td', { class: numericCols[i] ? 'num' : null, text: c }))));
    tbl.replaceChildren(h('thead', {}, head), h('tbody', {}, body));
    pager.replaceChildren();
    if (pages > 1) {
      pager.append(
        h('button', { type: 'button', disabled: page === 0, onclick: () => { page--; draw(); } }, '‹ Previous'),
        h('span', { class: 'muted small', text: `Page ${page + 1} of ${pages}` }),
        h('button', { type: 'button', disabled: page >= pages - 1, onclick: () => { page++; draw(); } }, 'Next ›'));
    }
  }
  draw();
  return wrap;
}

async function fileView(run, name) {
  const data = await api(`/api/table?run=${encodeURIComponent(run)}&name=${encodeURIComponent(name)}`);
  if (data.text !== undefined) return h('pre', { class: 'report', text: data.text });
  return dataTable(data.header, data.rows);
}

// --- 1. file ----------------------------------------------------------------------------------------------

async function viewFile() {
  const status = app.status = await api('/api/status');
  updateHeader();
  const picks = h('div');
  let chosen = status.selected ? status.selected.path : (status.data_files[0] || {}).path;
  for (const f of status.data_files) {
    picks.append(h('label', { class: 'choice' },
      h('input', { type: 'radio', name: 'xhb', value: f.path, checked: f.path === chosen, onchange: () => { chosen = f.path; custom.value = ''; } }),
      h('span', {}, h('b', { text: f.name }), ' ', h('span', { class: 'muted small', text: `${fmtSize(f.size)} · changed ${fmtDate(f.modified)}` }))));
  }
  const custom = h('input', { type: 'text', class: 'wide', placeholder: 'or paste the full path of a .xhb file, e.g. C:\\Users\\me\\Documents\\finances.xhb' });
  const use = h('button', { type: 'button', class: 'primary' }, 'Use this file');
  use.addEventListener('click', () => busy(use, 'Reading', async () => {
    const path = custom.value.trim() || chosen;
    if (!path) throw new Error('Choose a file first.');
    await api('/api/select', { path });
    app.categories = null;
    toast('File loaded.');
    await viewFile();
  }));

  const parts = [
    h('h1', { text: 'Your HomeBank file' }),
    h('p', { class: 'lead', text: 'The toolkit only reads this file. Changes are always written to a new copy in the output folder.' }),
    h('div', { class: 'card' },
      h('h2', { text: 'Choose the file' }),
      status.data_files.length ? picks : h('p', { class: 'muted', text: 'No .xhb file in the data folder yet.' }),
      h('p', { class: 'small muted' }, 'Tip: close HomeBank and copy your file into ', h('code', { text: status.data_dir }), '.'),
      h('div', { class: 'row' }, custom, use)),
  ];
  if (status.selected) {
    const info = await api('/api/structure').catch(err => { toast(err.message, true); return null; });
    if (info) {
      const f = info.facts;
      const fact = (value, label) => h('div', { class: 'fact' }, h('b', { text: value ?? '–' }), h('span', { text: label }));
      parts.push(h('div', { class: 'card' },
        h('h2', { text: info.file.name }),
        h('div', { class: 'facts' },
          fact(f.written_by, 'written by HomeBank'), fact(f.transactions.toLocaleString('en-ZA'), 'transactions'),
          fact(f.first_year && `${f.first_year}–${f.last_year}`, 'years'), fact(f.accounts, 'accounts'),
          fact(f.categories, 'categories'), fact(f.rules, 'assignment rules')),
        f.warnings || f.unknown_elements
          ? h('div', { class: 'notice warn', text: `The reader noted ${f.warnings} warning(s). See the structure report below.` })
          : h('div', { class: 'notice', text: 'The toolkit understands everything in this file.' }),
        h('div', { class: 'row' },
          h('button', { type: 'button', class: 'primary', onclick: () => go('analyse') }, 'Next: analyse →')),
        h('details', {}, h('summary', { text: 'Structure report (the same as .\\hb structure)' }),
          h('pre', { class: 'report', text: info.report.join('\n') }))));
    }
  }
  main(parts);
}

// --- 2. analyse --------------------------------------------------------------------------------------------

function paramInput(p) {
  const id = `p-${p.name}`;
  let input;
  if (p.type === 'bool') input = h('input', { type: 'checkbox', id, checked: p.default });
  else if (p.choices) input = h('select', { id }, p.choices.map(c => h('option', { value: c, selected: c === p.default }, c)));
  else if (p.type === 'int' || p.type === 'float') input = h('input', { type: 'number', id, value: p.default, step: p.type === 'int' ? 1 : 'any' });
  else input = h('input', { type: 'text', id, value: p.default });
  const read = () => p.type === 'bool' ? input.checked : input.value;
  const label = p.name.replaceAll('_', ' ');
  return { read, node: h('div', { class: 'field' }, h('label', { for: id, text: label[0].toUpperCase() + label.slice(1) }), h('div', {}, input), h('div', { class: 'help', text: p.help })) };
}

async function viewAnalyse() {
  if (!app.status || !app.status.selected) { await needFile(); return; }
  app.analyses = app.analyses || await api('/api/analyses');
  const cards = app.analyses.map(a => {
    const inputs = a.params.map(paramInput);
    const result = h('div');
    const run = h('button', { type: 'button', class: 'primary' }, 'Run');
    run.addEventListener('click', () => busy(run, 'Running', async () => {
      const params = Object.fromEntries(a.params.map((p, i) => [p.name, inputs[i].read()]));
      const res = await api('/api/run', { analysis: a.name, params });
      const actions = [];
      if (res.run) {
        if (a.name === 'rule-mining') actions.push(h('button', { type: 'button', class: 'primary', onclick: () => { app.reviewRun = res.run; go('review'); } }, 'Review the suggested rules →'));
        actions.push(h('button', { type: 'button', onclick: () => { app.resultsRun = res.run; go('results'); } }, 'See all result files'));
      }
      result.replaceChildren(summaryView(res.summary), h('div', { class: 'row' }, actions));
      toast(`${a.name} finished.`);
    }));
    const options = inputs.length ? h('details', {}, h('summary', { text: 'Options' }), inputs.map(i => i.node)) : null;
    return h('div', { class: 'card' }, h('h2', { text: a.name }), h('p', { class: 'muted', text: a.description }), options,
      h('div', { class: 'row' }, run), result);
  });
  main(h('h1', { text: 'Analyse' }), h('p', { class: 'lead', text: 'Each run writes its detailed results to a new folder in the output folder.' }), cards);
}

async function needFile() {
  main(h('h1', { text: 'Choose a file first' }),
    h('div', { class: 'card' }, h('p', { text: 'Pick your HomeBank file on the File page.' }),
      h('button', { type: 'button', class: 'primary', onclick: () => go('file') }, 'Go to File')));
}

// --- 3. rule review --------------------------------------------------------------------------------------------

const TRUE_VALUES = new Set(['y', 'yes', 'x', '1', 'true', 'ja', 'ok', 'approve', 'approved', '✓', '✔']);

async function viewReview() {
  if (!app.status || !app.status.selected) { await needFile(); return; }
  const runs = (await api('/api/runs')).filter(r => r.analysis === 'rule-mining' && r.files.includes('review.csv'));
  if (!runs.length) {
    main(h('h1', { text: 'Rule review' }), h('div', { class: 'card' },
      h('p', { text: 'There are no rule-mining results yet.' }),
      h('button', { type: 'button', class: 'primary', onclick: () => go('analyse') }, 'Run rule mining')));
    return;
  }
  if (!runs.some(r => r.run === app.reviewRun)) app.reviewRun = runs[0].run;
  const picker = h('select', { onchange: e => { if (confirmLeave()) { app.dirty = false; app.reviewRun = e.target.value; viewReview(); } else e.target.value = app.reviewRun; } },
    runs.map(r => h('option', { value: r.run, selected: r.run === app.reviewRun }, stampText(r.stamp))));
  const current = runs.find(r => r.run === app.reviewRun);
  const area = h('div');
  const tabs = [
    ['Suggested rules', () => suggestedRules(current.run)],
    ['Your existing rules', () => fileView(current.run, 'existing_rules.csv')],
    ['Inconsistent descriptions', () => fileView(current.run, 'inconsistent_patterns.csv')],
    ['Overlapping rules', () => fileView(current.run, 'overlaps.csv')],
    ['Back-test', async () => h('div', {}, h('h3', { text: 'Overall' }), await fileView(current.run, 'backtest.csv'),
      h('h3', { text: 'Per category' }), await fileView(current.run, 'backtest_by_category.csv'))],
    ['Set aside', () => fileView(current.run, 'rejected_candidates.csv')],
  ].filter(([name]) => name !== 'Back-test' || current.files.includes('backtest.csv'));
  const tabBar = h('div', { class: 'tabs' });
  async function show(i) {
    if (i !== 0 && !confirmLeave()) return;
    if (i !== 0) app.dirty = false;
    [...tabBar.children].forEach((b, j) => b.classList.toggle('active', i === j));
    area.replaceChildren(h('p', { class: 'muted', text: 'Loading…' }));
    try { area.replaceChildren(await tabs[i][1]()); } catch (err) { area.replaceChildren(h('div', { class: 'notice bad', text: err.message })); }
  }
  tabs.forEach(([name], i) => tabBar.append(h('button', { type: 'button', onclick: () => show(i) }, name)));
  main(h('h1', { text: 'Rule review' }),
    h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Results from' }), picker,
      h('button', { type: 'button', onclick: () => openFolder(current.run) }, 'Show folder')),
    tabBar, area);
  show(0);
}

async function suggestedRules(run) {
  const [data, categories] = await Promise.all([
    api(`/api/table?run=${encodeURIComponent(run)}&name=review.csv`),
    app.categories ? Promise.resolve(app.categories) : api('/api/categories'),
  ]);
  app.categories = categories;
  const catSet = new Set(categories.map(c => c.toLowerCase()));
  const col = Object.fromEntries(data.header.map((name, i) => [name, i]));
  const get = (r, name) => (col[name] === undefined ? '' : r[col[name]]);
  const rules = data.rows.map(r => ({
    id: get(r, 'rule_id'), approve: TRUE_VALUES.has(String(get(r, 'approve')).toLowerCase()),
    text: get(r, 'rule_text'), category: get(r, 'category'), kind: get(r, 'kind'),
    matched: Number(get(r, 'transactions_matched')) || 0, correct: get(r, 'correct_pct'),
    fill: Number(get(r, 'would_fill_uncategorised')) || 0, backtest: get(r, 'backtest'),
    btMatched: get(r, 'backtest_matched'), btCorrect: get(r, 'backtest_correct_pct'),
    coveredBy: get(r, 'covered_by'), conflicts: get(r, 'conflicts_with_existing_rules'),
    already: get(r, 'existing_rules_already_right_pct'), notes: get(r, 'notes'), examples: get(r, 'examples'),
  }));
  app.dirty = false;

  const datalist = h('datalist', { id: 'category-list' }, categories.map(c => h('option', { value: c })));
  const filters = { text: '', kind: '', hideCovered: false, conflictsOnly: false, approvedOnly: false };
  let sortKey = 'matched', sortDir = -1;
  const counter = h('span', { class: 'muted small' });
  const tbl = h('table', { class: 'data' });
  const result = h('div');

  const visible = () => rules.filter(r =>
    (!filters.text || `${r.text} ${r.category} ${r.examples} ${r.notes}`.toLowerCase().includes(filters.text)) &&
    (!filters.kind || r.kind === filters.kind) &&
    (!filters.hideCovered || !r.coveredBy) &&
    (!filters.conflictsOnly || r.conflicts) &&
    (!filters.approvedOnly || r.approve));

  function markDirty() {
    app.dirty = true;
    updateCounter();
  }
  function updateCounter() {
    const n = rules.filter(r => r.approve).length;
    counter.textContent = `${visible().length} of ${rules.length} shown · ${n} approved${app.dirty ? ' · not saved yet' : ''}`;
  }

  const columns = [
    ['approve', 'Approve'], ['text', 'Rule text'], ['category', 'Category'], ['kind', 'Kind'],
    ['matched', 'Matched'], ['correct', 'Correct %'], ['fill', 'Fills empty'], ['btCorrect', 'Back-test'],
    ['conflicts', 'Conflicts'], ['notes', 'Notes'], ['examples', 'Examples'],
  ];
  function draw() {
    const list = visible();
    list.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      const nx = Number(x), ny = Number(y);
      if (x !== '' && y !== '' && !isNaN(nx) && !isNaN(ny)) return (nx - ny) * sortDir;
      return String(x).localeCompare(String(y), 'en-ZA') * sortDir;
    });
    const head = h('tr', {}, columns.map(([key, label]) => h('th', {
      class: key === sortKey ? (sortDir > 0 ? 'sorted-asc' : 'sorted-desc') : null,
      onclick: () => { sortDir = sortKey === key ? -sortDir : (['matched', 'fill', 'correct', 'btCorrect'].includes(key) ? -1 : 1); sortKey = key; draw(); },
    }, label)));
    const body = list.map(r => {
      const tr = h('tr', { class: r.approve ? 'approved' : null });
      const check = h('input', { type: 'checkbox', checked: r.approve, title: 'Approve this rule',
        onchange: e => { r.approve = e.target.checked; tr.classList.toggle('approved', r.approve); markDirty(); } });
      const text = h('input', { type: 'text', value: r.text, oninput: e => { r.text = e.target.value; markDirty(); } });
      const cat = h('input', { type: 'text', value: r.category, list: 'category-list',
        class: catSet.has(r.category.toLowerCase()) ? null : 'invalid',
        oninput: e => { r.category = e.target.value; e.target.classList.toggle('invalid', !catSet.has(r.category.trim().toLowerCase())); markDirty(); } });
      const kind = h('span', { class: `pill ${r.kind === 'merged' ? 'merged' : ''}`, text: r.kind, title: r.coveredBy ? `Also covered by merged rule ${r.coveredBy}` : null });
      const bt = r.backtest === 'tested' ? `${r.btCorrect}% of ${r.btMatched}` : (r.backtest === 'not run' ? '–' : 'new');
      const ex = String(r.examples).split(' | ');
      tr.append(
        h('td', {}, check), h('td', {}, text), h('td', {}, cat),
        h('td', {}, kind, r.coveredBy ? h('div', { class: 'small muted', text: `in ${r.coveredBy}` }) : null),
        h('td', { class: 'num', text: r.matched }), h('td', { class: 'num', text: r.correct }),
        h('td', { class: 'num', text: r.fill || '' }),
        h('td', { class: 'num', text: bt, title: r.backtest === 'tested' ? 'correct in the back-test years' : 'not found when rules were built from the older years' }),
        h('td', {}, r.conflicts ? h('span', { class: 'pill bad', text: r.conflicts }) : ''),
        h('td', { class: 'notes', text: r.notes }),
        h('td', { class: 'examples', title: ex.join('\n'), text: ex[0] + (ex.length > 1 ? `  (+${ex.length - 1} more)` : '') }));
      return tr;
    });
    tbl.replaceChildren(h('thead', {}, head), h('tbody', {}, body));
    updateCounter();
  }

  const edits = () => rules.map(r => ({ rule_id: r.id, approve: r.approve, rule_text: r.text, category: r.category }));
  const setShown = value => { visible().forEach(r => { r.approve = value; }); markDirty(); draw(); };
  const save = h('button', { type: 'button' }, 'Save approvals');
  save.addEventListener('click', () => busy(save, 'Saving', async () => {
    const res = await api('/api/review/save', { run, edits: edits() });
    app.dirty = false; updateCounter();
    toast(`Saved: ${res.approved} rule(s) approved.`);
  }));
  const apply = h('button', { type: 'button', class: 'primary' }, 'Add approved rules to a new copy…');
  apply.addEventListener('click', () => {
    const n = rules.filter(r => r.approve).length;
    if (!n) { toast('Tick at least one rule first.', true); return; }
    const bad = rules.filter(r => r.approve && !catSet.has(r.category.trim().toLowerCase())).length;
    const extra = bad ? `\n\n${bad} approved rule(s) have a category that does not exist and will be skipped.` : '';
    if (!confirm(`Write ${n} approved rule(s) to a NEW copy of your HomeBank file?\nYour original file will not be changed.${extra}`)) return;
    busy(apply, 'Writing', async () => {
      const res = await api('/api/review/apply', { run, edits: edits() });
      app.dirty = false; updateCounter();
      result.replaceChildren(summaryView(res.summary));
      result.scrollIntoView({ behavior: 'smooth' });
    });
  });

  const kindSel = h('select', { onchange: e => { filters.kind = e.target.value; draw(); } },
    h('option', { value: '' }, 'All kinds'), h('option', { value: 'pattern' }, 'Pattern rules'), h('option', { value: 'merged' }, 'Merged rules'));
  const toggle = (label, key) => h('label', { class: 'small' },
    h('input', { type: 'checkbox', onchange: e => { filters[key] = e.target.checked; draw(); } }), ' ', label);

  draw();
  return h('div', {},
    datalist,
    h('p', { class: 'muted small' },
      'Tick the rules you want. You can change the rule text or pick another category. ',
      'New rules match when the memo contains the text (ignoring case) and set the category only when it is empty. ',
      'Merged rules cover several descriptions at once; the rules they cover are marked "in N…".'),
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: 'Search rules, categories, examples…', oninput: e => { filters.text = e.target.value.toLowerCase(); draw(); } }),
      kindSel, toggle('Hide rules covered by a merged rule', 'hideCovered'),
      toggle('Only conflicts with existing rules', 'conflictsOnly'), toggle('Only approved', 'approvedOnly')),
    h('div', { class: 'toolbar' },
      h('button', { type: 'button', onclick: () => setShown(true) }, 'Approve all shown'),
      h('button', { type: 'button', onclick: () => setShown(false) }, 'Clear all shown'),
      counter, h('span', { class: 'spacer' }), save, apply),
    h('div', { class: 'table-wrap' }, tbl),
    result);
}

// --- results -----------------------------------------------------------------------------------------------

async function viewResults() {
  const runs = await api('/api/runs');
  if (!runs.length) {
    main(h('h1', { text: 'Results' }), h('div', { class: 'card' }, h('p', { text: 'No results yet. Run an analysis first.' })));
    return;
  }
  if (!runs.some(r => r.run === app.resultsRun)) app.resultsRun = runs[0].run;
  const current = runs.find(r => r.run === app.resultsRun);
  const picker = h('select', { onchange: e => { app.resultsRun = e.target.value; viewResults(); } },
    runs.map(r => h('option', { value: r.run, selected: r.run === app.resultsRun }, `${r.analysis} — ${stampText(r.stamp)}`)));
  const area = h('div');
  const tabBar = h('div', { class: 'tabs' });
  async function show(i) {
    [...tabBar.children].forEach((b, j) => b.classList.toggle('active', i === j));
    area.replaceChildren(h('p', { class: 'muted', text: 'Loading…' }));
    try { area.replaceChildren(await fileView(current.run, current.files[i])); } catch (err) { area.replaceChildren(h('div', { class: 'notice bad', text: err.message })); }
  }
  current.files.forEach((f, i) => tabBar.append(h('button', { type: 'button', onclick: () => show(i) }, f)));
  main(h('h1', { text: 'Results' }),
    h('div', { class: 'row' }, picker, h('button', { type: 'button', onclick: () => openFolder(current.run) }, 'Show folder')),
    tabBar, area);
  const first = current.files.indexOf('review.csv');
  if (current.files.length) show(first >= 0 ? first : 0);
}

// --- export -----------------------------------------------------------------------------------------------

async function viewExport() {
  if (!app.status || !app.status.selected) { await needFile(); return; }
  const result = h('div');
  const exportBtn = h('button', { type: 'button', class: 'primary' }, 'Create SQLite database');
  exportBtn.addEventListener('click', () => busy(exportBtn, 'Exporting', async () => {
    const res = await api('/api/export-sqlite', {});
    result.replaceChildren(summaryView(res.summary));
  }));
  main(h('h1', { text: 'Export' }),
    h('div', { class: 'card' },
      h('h2', { text: 'SQLite database' }),
      h('p', { text: 'Turns your file into a database for your own queries (homebank.sqlite in the output folder; each export replaces the previous one). Open it with DB Browser for SQLite. The tables and views are described in docs\\sqlite-schema.md.' }),
      h('div', { class: 'row' }, exportBtn, h('button', { type: 'button', onclick: () => openFolder('') }, 'Show output folder'))),
    result);
}

// --- navigation ---------------------------------------------------------------------------------------------

const VIEWS = { file: viewFile, analyse: viewAnalyse, review: viewReview, results: viewResults, export: viewExport };

function updateHeader() {
  const s = app.status;
  document.getElementById('current-file').textContent = s && s.selected ? `File: ${s.selected.name}` : 'No file chosen';
  document.getElementById('version').textContent = s ? `hbkit ${s.version}` : '';
}

async function go(view) {
  if (app.view === 'review' && view !== 'review' && !confirmLeave()) return;
  if (view !== 'review') app.dirty = false;
  app.view = view;
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  try { await VIEWS[view](); } catch (err) { main(h('div', { class: 'notice bad', text: err.message })); }
}

window.addEventListener('beforeunload', e => { if (app.dirty) { e.preventDefault(); e.returnValue = ''; } });

document.addEventListener('DOMContentLoaded', async () => {
  document.querySelectorAll('#nav button').forEach(b => b.addEventListener('click', () => go(b.dataset.view)));
  if (!TOKEN) {
    main(h('div', { class: 'notice bad', text: 'This page needs the address printed in PowerShell when you started .\\hb ui (it contains a key). Please open that address.' }));
    return;
  }
  try {
    app.status = await api('/api/status');
  } catch (err) {
    main(h('div', { class: 'notice bad', text: err.message }));
    return;
  }
  updateHeader();
  go(app.status.selected ? 'analyse' : 'file');
});
