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
  wrap.append(h('div', { class: 'toolbar' }, opts.toolbar || null, search, count), h('div', { class: 'table-wrap' }, tbl), pager);

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
    const body = shown.map(r => h('tr', { class: opts.rowClass ? opts.rowClass(r) : null },
      r.map((c, i) => h('td', { class: numericCols[i] ? 'num' : null, text: c }))));
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

// --- CSV downloads, made in the browser: the file goes to the browser's Downloads folder ----------------------

function toCsv(header, rows) {
  const cell = v => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return [header, ...rows].map(r => r.map(cell).join(',')).join('\r\n') + '\r\n';
}

function downloadCsv(filename, header, rows) {
  // The byte-order mark makes Excel read the file as UTF-8, like the toolkit's own CSV files.
  const url = URL.createObjectURL(new Blob(['\ufeff', toCsv(header, rows)], { type: 'text/csv;charset=utf-8' }));
  const a = h('a', { href: url, download: filename, hidden: true });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
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
    ['Depends on account', async () => h('div', {},
      h('p', { class: 'muted small' },
        'Descriptions you categorise differently depending on the account, for example interest on two home loans ',
        'that belong to two properties. HomeBank rules cannot be limited to one account, so a rule can only tell ',
        'the accounts apart when the descriptions differ, for example by a policy or loan number. Where a text was ',
        'found it is suggested as an account rule (kind "account" on the Suggested rules tab). Where the outcome is ',
        '"no rule text tells this account\'s transactions apart", the descriptions are the same in each account and ',
        'no HomeBank rule can categorise them correctly: categorise those by hand.'),
      await fileView(current.run, 'account_dependent.csv'))],
    ['Overlapping rules', () => fileView(current.run, 'overlaps.csv')],
    ['Back-test', async () => h('div', {}, h('h3', { text: 'Overall' }), await fileView(current.run, 'backtest.csv'),
      h('h3', { text: 'Per category' }), await fileView(current.run, 'backtest_by_category.csv'))],
    ['Set aside', () => fileView(current.run, 'rejected_candidates.csv')],
  ].filter(([name]) => (name !== 'Back-test' || current.files.includes('backtest.csv')) &&
    (name !== 'Depends on account' || current.files.includes('account_dependent.csv')));
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
    text: get(r, 'rule_text'), category: get(r, 'category'), kind: get(r, 'kind'), raw: r,
    forAccount: get(r, 'for_account'), otherAccounts: get(r, 'other_accounts_differ'),
    matched: Number(get(r, 'transactions_matched')) || 0, correct: get(r, 'correct_pct'),
    fill: Number(get(r, 'would_fill_uncategorised')) || 0, backtest: get(r, 'backtest'),
    btMatched: get(r, 'backtest_matched'), btCorrect: get(r, 'backtest_correct_pct'),
    coveredBy: get(r, 'covered_by'), conflicts: get(r, 'conflicts_with_existing_rules'),
    already: get(r, 'existing_rules_already_right_pct'), notes: get(r, 'notes'), examples: get(r, 'examples'),
  }));
  app.dirty = false;

  const datalist = h('datalist', { id: 'category-list' }, categories.map(c => h('option', { value: c })));
  const filters = { text: '', kind: '', hideCovered: false, conflictsOnly: false, approvedOnly: false };
  let lastDrawn = [];
  let sortKey = 'matched', sortDir = -1;
  const counter = h('span', { class: 'muted small' });
  const tbl = h('table', { class: 'data' });
  const result = h('div');

  const visible = () => rules.filter(r =>
    (!filters.text || `${r.text} ${r.category} ${r.examples} ${r.notes}`.toLowerCase().includes(filters.text)) &&
    (!filters.kind || r.kind === filters.kind) &&
    (!filters.hideCovered || !r.coveredBy) &&
    (!filters.conflictsOnly || r.conflicts || r.otherAccounts) &&
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
    ['approve', 'Approve'], ['text', 'Rule text'], ['category', 'Category'], ['group', 'Group'], ['kind', 'Kind'],
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
    lastDrawn = list;
    const head = h('tr', {}, columns.map(([key, label]) => h('th', {
      class: key === sortKey ? (sortDir > 0 ? 'sorted-asc' : 'sorted-desc') : null,
      onclick: () => {
        if (key === 'group') return;
        sortDir = sortKey === key ? -sortDir : (['matched', 'fill', 'correct', 'btCorrect'].includes(key) ? -1 : 1); sortKey = key; draw();
      },
    }, label)));
    const body = list.map(r => {
      const tr = h('tr', { class: r.approve ? 'approved' : null });
      const check = h('input', { type: 'checkbox', checked: r.approve, title: 'Approve this rule',
        onchange: e => { r.approve = e.target.checked; tr.classList.toggle('approved', r.approve); markDirty(); } });
      const text = h('input', { type: 'text', value: r.text, oninput: e => { r.text = e.target.value; markDirty(); } });
      const cat = h('input', { type: 'text', value: r.category, list: 'category-list',
        class: catSet.has(r.category.toLowerCase()) ? null : 'invalid',
        oninput: e => { r.category = e.target.value; e.target.classList.toggle('invalid', !catSet.has(r.category.trim().toLowerCase())); markDirty(); } });
      const kind = h('span', { class: `pill ${r.kind === 'pattern' ? '' : r.kind}`, text: r.kind, title: r.coveredBy ? `Also covered by merged rule ${r.coveredBy}` : null });
      const bt = r.backtest === 'tested' ? `${r.btCorrect}% of ${r.btMatched}` : (r.backtest === 'not run' ? '–' : 'new');
      const ex = String(r.examples).split(' | ');
      tr.append(
        h('td', {}, check), h('td', {}, text), h('td', {}, cat),
        h('td', {}, h('button', { type: 'button', class: 'compact', title: 'Show the transactions this rule matches',
          onclick: () => showGroup(r) }, 'View')),
        h('td', {}, kind, r.coveredBy ? h('div', { class: 'small muted', text: `in ${r.coveredBy}` }) : null,
          r.forAccount ? h('div', { class: 'small muted', text: `for ${r.forAccount}` }) : null),
        h('td', { class: 'num', text: r.matched }), h('td', { class: 'num', text: r.correct }),
        h('td', { class: 'num', text: r.fill || '' }),
        h('td', { class: 'num', text: bt, title: r.backtest === 'tested' ? 'correct in the back-test years' : 'not found when rules were built from the older years' }),
        h('td', {}, r.conflicts ? h('span', { class: 'pill bad', text: r.conflicts }) : '',
          r.otherAccounts ? h('span', { class: 'pill warn', text: 'another account differs', title: r.otherAccounts }) : ''),
        h('td', { class: 'notes', text: r.notes }),
        h('td', { class: 'examples', title: ex.join('\n'), text: ex[0] + (ex.length > 1 ? `  (+${ex.length - 1} more)` : '') }));
      return tr;
    });
    tbl.replaceChildren(h('thead', {}, head), h('tbody', {}, body));
    updateCounter();
  }

  const edits = () => rules.map(r => ({ rule_id: r.id, approve: r.approve, rule_text: r.text, category: r.category }));

  // Download the rules as shown (filters and sort order), with your approvals and edits, in the review.csv layout.
  const download = h('button', { type: 'button', title: 'Save the rules shown, with your ticks and edits, as a CSV file' }, 'Download shown (CSV)');
  download.addEventListener('click', () => {
    if (!lastDrawn.length) { toast('No rules are shown.', true); return; }
    const at = name => data.header.indexOf(name);
    const rows = lastDrawn.map(r => {
      const row = r.raw.slice();
      if (at('approve') >= 0) row[at('approve')] = r.approve ? 'y' : '';
      if (at('rule_text') >= 0) row[at('rule_text')] = r.text;
      if (at('category') >= 0) row[at('category')] = r.category;
      return row;
    });
    downloadCsv(`suggested-rules_${run.split('/').pop()}.csv`, data.header, rows);
    toast(`${rows.length} rule(s) downloaded as a CSV file (see your browser's downloads).`);
  });

  // The transactions a rule matches, to check that they belong together.
  const dialog = h('dialog', { class: 'rule-dialog', onclose: () => draw() });
  function showGroup(r) {
    const pos = lastDrawn.indexOf(r);
    const body = h('div');
    const check = h('input', { type: 'checkbox', checked: r.approve, onchange: e => { r.approve = e.target.checked; markDirty(); } });
    const text = h('input', { type: 'text', class: 'wide', value: r.text, oninput: e => { r.text = e.target.value; markDirty(); } });
    const cat = h('input', { type: 'text', class: `wide ${catSet.has(r.category.trim().toLowerCase()) ? '' : 'invalid'}`, value: r.category, list: 'category-list',
      oninput: e => { r.category = e.target.value; e.target.classList.toggle('invalid', !catSet.has(r.category.trim().toLowerCase())); markDirty(); } });
    async function load() {
      body.replaceChildren(h('p', { class: 'muted', text: 'Finding the transactions…' }));
      try {
        const q = `run=${encodeURIComponent(run)}&text=${encodeURIComponent(r.text)}&category=${encodeURIComponent(r.category)}`;
        body.replaceChildren(matchesView(await api(`/api/review/matches?${q}`), r));
      } catch (err) { body.replaceChildren(h('div', { class: 'notice bad', text: err.message })); }
    }
    const nav = (step, label) => h('button', { type: 'button', disabled: pos < 0 || !lastDrawn[pos + step],
      onclick: () => showGroup(lastDrawn[pos + step]) }, label);
    dialog.replaceChildren(
      h('div', { class: 'dialog-head' },
        h('h2', { text: `Rule ${r.id}` }),
        h('span', { class: `pill ${r.kind === 'pattern' ? '' : r.kind}`, text: r.kind }),
        r.forAccount ? h('span', { class: 'muted small', text: `for ${r.forAccount}` }) : null,
        h('span', { class: 'muted small', text: pos >= 0 ? `${pos + 1} of ${lastDrawn.length} shown` : '' }),
        h('span', { class: 'spacer' }),
        nav(-1, '‹ Previous rule'), nav(1, 'Next rule ›'),
        h('button', { type: 'button', class: 'primary', onclick: () => dialog.close() }, 'Close')),
      h('div', { class: 'dialog-form' },
        h('label', { text: 'Rule text' }), h('div', { class: 'row' }, text,
          h('button', { type: 'button', onclick: load, title: 'Show what the text matches now' }, 'Show matches')),
        h('label', { text: 'Category' }), cat,
        h('span'), h('label', {}, check, ' Approve this rule')),
      body);
    if (!dialog.open) dialog.showModal();
    load();
  }
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
    h('option', { value: '' }, 'All kinds'), h('option', { value: 'pattern' }, 'Pattern rules'), h('option', { value: 'merged' }, 'Merged rules'),
    h('option', { value: 'account' }, 'Account rules'));
  const toggle = (label, key) => h('label', { class: 'small' },
    h('input', { type: 'checkbox', onchange: e => { filters[key] = e.target.checked; draw(); } }), ' ', label);

  draw();
  return h('div', {},
    datalist,
    h('p', { class: 'muted small' },
      'Tick the rules you want. You can change the rule text or pick another category. ',
      'New rules match when the memo contains the text (ignoring case) and set the category only when it is empty. ',
      'Merged rules cover several descriptions at once; the rules they cover are marked "in N…". ',
      'Account rules pick out one account\'s transactions by a text only that account\'s descriptions contain. ',
      'Click View to see every transaction a rule matches.'),
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: 'Search rules, categories, examples…', oninput: e => { filters.text = e.target.value.toLowerCase(); draw(); } }),
      kindSel, toggle('Hide rules covered by a merged rule', 'hideCovered'),
      toggle('Only conflicts (existing rules or other accounts)', 'conflictsOnly'), toggle('Only approved', 'approvedOnly')),
    h('div', { class: 'toolbar' },
      h('button', { type: 'button', onclick: () => setShown(true) }, 'Approve all shown'),
      h('button', { type: 'button', onclick: () => setShown(false) }, 'Clear all shown'),
      counter, h('span', { class: 'spacer' }), download, save, apply),
    h('div', { class: 'table-wrap' }, tbl),
    result, dialog);
}

const RESULT_OTHER = 'other category', RESULT_FILL = 'no category: would be filled';

function matchesView(res, rule) {
  const at = Object.fromEntries(res.header.map((name, i) => [name, i]));
  const rows = res.rows;
  const n = value => rows.filter(x => x[at.result] === value).length;
  const fact = (value, label) => h('div', { class: 'fact' }, h('b', { text: value }), h('span', { text: label }));
  const parts = [h('div', { class: 'facts' },
    fact(rows.length, 'transactions match'), fact(n('same category'), 'have this category'),
    fact(n(RESULT_OTHER), 'have another category'), fact(n(RESULT_FILL), 'have none: the rule would fill them'))];
  if (res.file_changed) parts.push(h('div', { class: 'notice warn', text: 'Your HomeBank file has changed since these results were made. This list is from the file as it is now.' }));
  if (!res.category_found) parts.push(h('div', { class: 'notice warn', text: 'That category does not exist in the file, so no transaction counts as having it.' }));
  parts.push(h('p', { class: 'muted small', text: `Matched the HomeBank way: the ${res.field} contains the rule text, ignoring case. ` +
    'Void, split and (unless chosen in rule mining) transfer transactions are left out.' }));
  if (!rows.length) {
    parts.push(h('div', { class: 'notice', text: 'This text matches no transactions.' }));
    return h('div', {}, parts);
  }

  // Description groups: transactions with the same cleaned description (card numbers, dates and references removed).
  const groups = new Map();
  for (const x of rows) {
    const key = x[at.pattern];
    let g = groups.get(key);
    if (!g) groups.set(key, g = { n: 0, cats: new Map(), accounts: new Set(), first: x[at.date], last: x[at.date], example: x[at.memo] });
    g.n++;
    const c = x[at.category] || '(none)';
    g.cats.set(c, (g.cats.get(c) || 0) + 1);
    g.accounts.add(x[at.account]);
    if (x[at.date] < g.first) g.first = x[at.date];
    if (x[at.date] > g.last) g.last = x[at.date];
  }
  const groupRows = [...groups].map(([pattern, g]) => [pattern, g.n,
    [...g.cats].sort((a, b) => b[1] - a[1]).map(([c, k]) => `${c} (${k})`).join(' · '),
    [...g.accounts].join(' · '), g.first, g.last, g.example]).sort((a, b) => b[1] - a[1]);
  const txnCols = ['date', 'account', 'memo', 'payee', 'category', 'amount', 'result'];
  const money = v => Number(v).toFixed(2);
  let only = '';
  const txnArea = h('div');
  const drawTxns = () => {
    const list = rows.filter(x => !only || x[at.result] === only);
    txnArea.replaceChildren(dataTable(txnCols, list.map(x => txnCols.map(c => c === 'amount' ? money(x[at.amount]) : x[at[c]])),
      { pageSize: 200, rowClass: r => (r[6] === RESULT_OTHER ? 'warn-row' : null), toolbar: onlySel }));
  };
  const onlySel = h('select', { onchange: e => { only = e.target.value; drawTxns(); } },
    h('option', { value: '' }, 'All transactions'), h('option', { value: RESULT_OTHER }, 'Only with another category'),
    h('option', { value: RESULT_FILL }, 'Only without a category'));
  const area = h('div');
  const tabBar = h('div', { class: 'tabs' });
  const tabs = [
    [`Description groups (${groupRows.length})`, () => h('div', {},
      h('p', { class: 'muted small', text: 'Transactions with the same description once card numbers, dates and references are removed.' }),
      dataTable(['description_group', 'transactions', 'categories', 'accounts', 'first', 'last', 'example'], groupRows, { pageSize: 100 }))],
    [`Transactions (${rows.length})`, () => { drawTxns(); return txnArea; }],
  ];
  const show = i => {
    [...tabBar.children].forEach((b, j) => b.classList.toggle('active', i === j));
    area.replaceChildren(tabs[i][1]());
  };
  tabs.forEach(([label], i) => tabBar.append(h('button', { type: 'button', onclick: () => show(i) }, label)));
  tabBar.append(h('span', { class: 'spacer' }), h('button', { type: 'button', class: 'compact',
    onclick: () => downloadCsv(`rule_${rule.id}_transactions.csv`, res.header, rows) }, 'Download these transactions (CSV)'));
  show(groupRows.length > 1 ? 0 : 1);
  return h('div', {}, parts, tabBar, area);
}

// --- reports -----------------------------------------------------------------------------------------------

const INSTALL_CMD = '.venv\\Scripts\\python -m pip install --no-index --find-links vendor\\wheels -r requirements.txt';

async function openReport(path) {
  try { await api('/api/open', { path, file: true }); } catch (err) { toast(err.message, true); }
}

async function viewReports() {
  if (!app.status || !app.status.selected) { await needFile(); return; }
  const info = await api('/api/reports');
  const st = info.settings;
  const tax = st && st.year_mode === 'tax';
  const parts = [
    h('h1', { text: 'Reports' }),
    h('p', { class: 'lead', text: 'Each report is one page saved in the reports folder. It opens in your browser, works offline and can be printed.' }),
  ];
  if (!info.plotly) {
    parts.push(h('div', { class: 'notice warn' },
      'Plotly is not installed yet, so reports with charts cannot be made. Close this window, then in PowerShell in the project folder run once (no internet needed): ',
      h('code', { text: INSTALL_CMD }), ' and start the toolkit again.'));
  }
  if (st.error) {
    parts.push(h('div', { class: 'notice bad', text: `Settings file problem: ${st.error}` }));
  } else {
    parts.push(h('div', { class: 'card' },
      h('h2', { text: 'Settings' }),
      st.path ? h('p', {}, 'Using ', h('code', { text: st.path }))
        : h('p', { text: 'No settings file found, so the defaults are used (calendar years, no excluded accounts, no groups). Copy settings.example.toml to data\\settings.toml and edit it to set tax years, groups and inflation.' }),
      h('p', { class: 'small muted', text: `Year mode: ${tax ? 'SA tax year (1 March – end February)' : 'calendar year'} · excluded accounts: ${st.excluded_accounts.length} · category groups: ${st.groups.length} · CPI table: ${st.cpi_file ? 'yes' : 'no'}` })));
  }

  const yearHelp = tax ? 'Tax years are given by the year in which they end, e.g. 2026 for 2025/26.' : 'Calendar years, e.g. 2025.';
  for (const r of info.reports) {
    const yearInputs = r.period_kind === 'single'
      ? [['year', 'Year', 'latest finished year']]
      : [['from', 'From year', 'first year'], ['to', 'To year', 'last year']];
    const years = yearInputs.map(([key, label, placeholder]) => {
      const input = h('input', { type: 'number', step: 1, placeholder, id: `r-${r.name}-${key}` });
      return { key, input, node: h('div', { class: 'field' }, h('label', { for: input.id, text: label }), h('div', {}, input)) };
    });
    const inputs = r.params.map(paramInput);
    const result = h('div');
    const make = h('button', { type: 'button', class: 'primary', disabled: r.needs_charts && !info.plotly }, 'Make report');
    make.addEventListener('click', () => busy(make, 'Making', async () => {
      const body = { report: r.name, params: Object.fromEntries(r.params.map((p, i) => [p.name, inputs[i].read()])) };
      for (const y of years) body[y.key] = y.input.value;
      const res = await api('/api/report/run', body);
      result.replaceChildren(summaryView(res.summary), h('div', { class: 'row' },
        h('button', { type: 'button', class: 'primary', onclick: () => openReport(res.output) }, 'Open report'),
        h('button', { type: 'button', onclick: () => openFolder(res.output) }, 'Show in folder')));
      toast(`${r.name} is ready.`);
    }));
    parts.push(h('div', { class: 'card' },
      h('h2', { text: r.name }), h('p', { class: 'muted', text: r.description }),
      years.map(y => y.node), h('p', { class: 'small muted', text: `${yearHelp} Leave empty for ${r.period_kind === 'single' ? 'the latest finished year' : 'all years'}.` }),
      inputs.length ? h('details', {}, h('summary', { text: 'Options' }), inputs.map(i => i.node)) : null,
      h('div', { class: 'row' }, make), result));
  }

  if (info.files.length) {
    parts.push(h('h2', { text: 'Reports made earlier' }), h('div', { class: 'card' },
      h('table', { class: 'data' }, h('tbody', {}, info.files.map(f => h('tr', {},
        h('td', { text: f.name }), h('td', { class: 'num muted small', text: fmtDate(f.modified) }),
        h('td', {}, h('button', { type: 'button', onclick: () => openReport(f.path) }, 'Open'))))))));
  }
  main(parts);
}

// --- imports -----------------------------------------------------------------------------------------------

function previousMonth() {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function shiftMonth(month, step) {
  let [y, m] = month.split('-').map(Number);
  m += step;
  while (m < 1) { m += 12; y--; }
  while (m > 12) { m -= 12; y++; }
  return `${y}-${String(m).padStart(2, '0')}`;
}

function monthTitle(month) {
  const [y, m] = month.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString('en-ZA', { month: 'long', year: 'numeric' });
}

async function fileToBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function viewImports() {
  if (!app.status || !app.status.selected) { await needFile(); return; }
  app.importMonth = app.importMonth || previousMonth();
  const month = app.importMonth;
  const [info, categories] = await Promise.all([
    api(`/api/import/month?month=${encodeURIComponent(month)}`),
    app.categories ? Promise.resolve(app.categories) : api('/api/categories'),
  ]);
  app.categories = categories;
  const title = monthTitle(month);
  const accounts = info.accounts;
  const toMonth = m => { app.importMonth = m; viewImports(); };

  // month
  const monthInput = h('input', { type: 'month', value: month, 'aria-label': 'Month',
    onchange: e => { if (/^\d{4}-\d{2}$/.test(e.target.value)) toMonth(e.target.value); } });
  const monthRow = h('div', { class: 'row' }, h('span', { class: 'muted', text: 'Month' }),
    h('button', { type: 'button', title: 'Previous month', onclick: () => toMonth(shiftMonth(month, -1)) }, '‹'),
    monthInput,
    h('button', { type: 'button', title: 'Next month', onclick: () => toMonth(shiftMonth(month, 1)) }, '›'),
    info.files.length ? h('button', { type: 'button', onclick: () => openFolder(info.folder) }, 'Show folder') : null);

  // accounts that matter this month
  const accTable = h('table', { class: 'data' });
  const accCount = h('span', { class: 'muted small' });
  let showAll = !!app.importShowAll;
  function drawAccounts() {
    const list = accounts.filter(a => showAll || a.expected || a.files.length);
    const expected = accounts.filter(a => a.expected);
    accCount.textContent = `${expected.filter(a => a.files.length).length} of ${expected.length} accounts you import monthly have a file for ${title}.`;
    accTable.replaceChildren(
      h('thead', {}, h('tr', {}, ['Monthly', 'Account', 'Type', 'Latest in HomeBank', `In HomeBank for ${title}`, 'File'].map(t => h('th', { text: t })))),
      h('tbody', {}, list.map(a => h('tr', {},
        h('td', {}, h('input', { type: 'checkbox', checked: a.expected, title: 'I import this account every month (remembered)',
          onchange: e => saveExpected(a, e.target.checked) })),
        h('td', { text: a.name + (a.closed ? ' (closed)' : '') }),
        h('td', { class: 'muted', text: a.type }),
        h('td', { text: a.last_date }),
        h('td', { class: 'num', text: a.in_month || '' }),
        h('td', {}, a.files.length
          ? a.files.map(f => h('div', {}, h('span', { class: `pill ${f.prepared ? 'ok' : ''}`, text: f.prepared ? 'prepared' : 'linked' }), ' ', f.name))
          : (a.expected ? h('span', { class: 'muted small', text: 'no file yet' }) : ''))))));
  }
  async function saveExpected(a, value) {
    a.expected = value;
    try { await api('/api/import/expected', { accounts: accounts.filter(x => x.expected).map(x => x.key) }); }
    catch (err) { toast(err.message, true); }
    drawAccounts();
  }
  drawAccounts();
  const accountsCard = h('div', { class: 'card' },
    h('h2', { text: `Accounts for ${title}` }),
    h('p', { class: 'small muted', text: info.expected_chosen
      ? 'Ticked accounts are the ones you import every month.'
      : 'Ticked for now: open accounts with transactions in this month or the three before. Change the ticks to choose the accounts you import every month; they are remembered.' }),
    h('div', { class: 'toolbar' }, accCount, h('span', { class: 'spacer' }),
      h('label', { class: 'small' }, h('input', { type: 'checkbox', checked: showAll,
        onchange: e => { showAll = app.importShowAll = e.target.checked; drawAccounts(); } }), ' Show all accounts')),
    h('div', { class: 'table-wrap' }, accTable));

  // files
  const fileInput = h('input', { type: 'file', multiple: true, accept: '.qif,.csv,.txt', hidden: true,
    onchange: e => upload([...e.target.files]) });
  const addBtn = h('button', { type: 'button', class: 'primary', onclick: () => fileInput.click() }, 'Add files…');
  const drop = h('div', { class: 'dropzone' }, h('span', { text: 'Drop QIF or CSV files from your bank here, or ' }), addBtn, fileInput);
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); upload([...e.dataTransfer.files]); });
  async function upload(files) {
    let added = 0;
    for (const file of files) {
      try {
        await api('/api/import/upload', { month, name: file.name, content: await fileToBase64(file) });
        added++;
      } catch (err) { toast(`${file.name}: ${err.message}`, true); }
    }
    if (added) { toast(`${added} file(s) added for ${title}.`); viewImports(); }
  }
  const accountOptions = selected => {
    const expected = accounts.filter(a => a.expected), others = accounts.filter(a => !a.expected);
    const opt = a => h('option', { value: a.key, selected: a.key === selected }, a.name + (a.closed ? ' (closed)' : ''));
    return [h('option', { value: '' }, '— choose the account —'),
      expected.length ? h('optgroup', { label: 'Imported monthly' }, expected.map(opt)) : null,
      others.length ? h('optgroup', { label: expected.length ? 'Other accounts' : 'Accounts' }, others.map(opt)) : null];
  };
  const previewArea = h('div');
  const fileRows = info.files.map(f => {
    const sel = h('select', { onchange: async e => {
      try {
        await api('/api/import/link', { month, file: f.name, account: e.target.value || null });
        viewImports();
      } catch (err) { toast(err.message, true); }
    } }, accountOptions(f.account || f.suggested));
    const why = f.account ? 'linked by you' : (f.suggested ? `suggested: ${f.reason}` : f.reason);
    const prepare = h('button', { type: 'button', class: f.prepared ? null : 'primary' }, f.prepared ? 'Prepare again…' : 'Prepare…');
    prepare.addEventListener('click', () => {
      if (!sel.value) { toast('Choose the account first.', true); return; }
      busy(prepare, 'Loading', () => showPreview(f.name, Number(sel.value)));
    });
    const remove = h('button', { type: 'button', title: 'Remove this file from the month (your own copy is not touched)' }, 'Remove');
    remove.addEventListener('click', () => {
      if (!confirm(`Remove ${f.name} from ${title}? The toolkit's copy and its prepared file are deleted; your own download is not touched.`)) return;
      busy(remove, 'Removing', async () => { await api('/api/import/remove', { month, file: f.name }); viewImports(); });
    });
    return h('tr', {},
      h('td', {}, h('b', { text: f.name }),
        f.problems.length ? h('div', { class: 'small warn-text', text: `HomeBank may not read line(s) ${f.problems.slice(0, 5).join(', ')}${f.problems.length > 5 ? '…' : ''}` }) : null),
      h('td', { text: f.format.toUpperCase() }),
      h('td', { class: 'num', text: f.transactions }),
      h('td', { class: 'nowrap', text: f.first ? `${f.first} – ${f.last}` : '' }),
      h('td', {}, sel, h('div', { class: `small ${f.account ? 'muted' : 'suggested'}`, text: why }),
        f.warning ? h('div', { class: 'small bad-text', text: `Check: ${f.warning}` }) : null),
      h('td', {}, f.prepared ? h('div', { class: 'row' }, h('span', { class: 'pill ok', text: 'prepared' }),
        h('button', { type: 'button', class: 'compact', onclick: () => openFolder(f.prepared) }, 'Show in folder')) : null),
      h('td', {}, h('div', { class: 'row' }, prepare, remove)));
  });
  const filesCard = h('div', { class: 'card' },
    h('h2', { text: `Files for ${title}` }),
    drop,
    info.unreadable.map(u => h('div', { class: 'notice bad', text: `${u.name}: ${u.error}` })),
    info.files.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['File', 'Kind', 'Transactions', 'Dates', 'Account', 'Prepared', ''].map(t => h('th', { text: t })))),
      h('tbody', {}, fileRows))) : h('p', { class: 'muted', text: 'No files added for this month yet.' }));

  async function showPreview(file, account) {
    previewArea.replaceChildren(await importPreview(month, file, account, () => viewImports()));
    previewArea.scrollIntoView({ behavior: 'smooth' });
  }

  const record = info.record.length ? [h('h2', { text: 'Import record' }), h('div', { class: 'card' },
    h('p', { class: 'small muted', text: 'Which file went to which account, every month. File names seen before are linked to the same account next time.' }),
    dataTable(['month', 'file', 'account', 'transactions', 'categories_written', 'prepared', 'linked_on'],
      info.record.map(x => [x.month, x.file, x.account_name || '', x.transactions ?? '', x.categorised ?? '',
        x.prepared ? 'yes' : 'no', String(x.linked || '').replace('T', ' ')]), { pageSize: 50 }))] : [];

  main(h('h1', { text: 'Prepare imports' }),
    h('p', { class: 'lead', text: 'Add the files you downloaded from your banks for a month. Each file is linked to its account, and categories ' +
      'are filled in from that account\'s own history, which HomeBank\'s rules cannot do. The prepared copy is what you import in HomeBank. ' +
      'Your downloads are never changed.' }),
    monthRow, accountsCard, filesCard, previewArea, record);
}

async function importPreview(month, file, account, onSaved) {
  const q = `month=${encodeURIComponent(month)}&file=${encodeURIComponent(file)}&account=${encodeURIComponent(account)}`;
  const res = await api(`/api/import/preview?${q}`);
  const catSet = new Set((app.categories || []).map(c => c.toLowerCase()));
  const rows = res.rows.map(r => ({ ...r, value: r.skip ? '' : r.suggested }));
  const facts = h('div', { class: 'facts' });
  const fact = (value, label) => h('div', { class: 'fact' }, h('b', { text: value }), h('span', { text: label }));
  function drawFacts() {
    const open = rows.filter(r => !r.skip);
    const filled = open.filter(r => r.value.trim()).length;
    const byHomeBank = open.filter(r => !r.value.trim() && r.homebank).length;
    facts.replaceChildren(
      fact(rows.length, 'transactions'),
      fact(filled, 'get a category from this file'),
      fact(byHomeBank, 'left to your HomeBank rules'),
      fact(open.length - filled - byHomeBank, 'stay without a category'),
      fact(rows.filter(r => r.duplicate).length, 'may already be in HomeBank'));
  }
  const body = rows.map(r => {
    // An overwrite rule only matters when it would put another category than the one written.
    const pill = r.overwritten_by ? h('span', { class: 'pill bad', text: `rule ${r.overwritten_by} overwrites`,
      title: `Your overwrite rule ${r.overwritten_by} will replace the category with ${r.overwrite_category || 'its own'} on import` }) : null;
    const tr = h('tr');
    const checkOverwrite = () => {
      const clash = !!pill && !!r.value.trim() && r.value.trim().toLowerCase() !== (r.overwrite_category || '').toLowerCase();
      if (pill) pill.hidden = !clash;
      tr.classList.toggle('warn-row', clash);
    };
    let cell;
    if (r.skip) {
      cell = h('span', { class: 'muted', text: r.skip === 'already has a category' ? `${r.in_file} (in the file)` : r.skip });
    } else {
      cell = h('input', { type: 'text', value: r.value, list: 'category-list', placeholder: r.homebank ? `HomeBank: ${r.homebank}` : '',
        class: r.value && !catSet.has(r.value.toLowerCase()) ? 'invalid' : null,
        oninput: e => {
          r.value = e.target.value;
          e.target.classList.toggle('invalid', !!r.value.trim() && !catSet.has(r.value.trim().toLowerCase()));
          checkOverwrite();
          drawFacts();
        } });
    }
    let why = '';
    if (r.suggested) why = r.evidence;
    else if (r.homebank) why = `HomeBank will set ${r.homebank} (${r.homebank_by})`;
    else if (!r.skip) why = 'nothing to go on';
    tr.append(
      h('td', { class: 'nowrap', text: r.date }), h('td', { text: r.memo }), h('td', { text: r.payee }),
      h('td', { class: 'num', text: r.amount === null ? '' : Number(r.amount).toFixed(2) }),
      h('td', {}, cell),
      h('td', { class: `small ${r.source === 'all accounts' ? 'warn-text' : 'muted'}`, text: why }),
      h('td', {},
        r.duplicate ? h('span', { class: 'pill warn', text: 'already in HomeBank?', title: 'Same date, amount and description already in this account' }) : null,
        pill));
    checkOverwrite();
    return tr;
  });
  drawFacts();
  const result = h('div');
  const save = h('button', { type: 'button', class: 'primary' }, 'Save prepared file');
  save.addEventListener('click', () => {
    const bad = rows.filter(r => !r.skip && r.value.trim() && !catSet.has(r.value.trim().toLowerCase())).length;
    if (bad) { toast(`${bad} category name(s) do not exist in your HomeBank file. Pick one from the list or leave it empty.`, true); return; }
    busy(save, 'Saving', async () => {
      const categories = Object.fromEntries(rows.filter(r => !r.skip && r.value.trim()).map(r => [r.index, r.value.trim()]));
      const out = await api('/api/import/prepare', { month, file, account, categories });
      result.replaceChildren(h('div', { class: 'notice' },
        h('p', { text: `Saved with ${out.categorised} categor${out.categorised === 1 ? 'y' : 'ies'}: ${out.path}` }),
        h('p', { text: `In HomeBank: File > Import…, choose this prepared file and import it into "${out.account_name}". ` +
          'Keep automatic assignment switched on so your rules fill the rest.' }),
        h('button', { type: 'button', onclick: () => openFolder(out.path) }, 'Show in folder')));
      toast('Prepared file saved.');
      setTimeout(onSaved, 4000);
    });
  });
  return h('div', { class: 'card' },
    h('h2', { text: `${file} → ${res.account_name}` }),
    h('p', { class: 'small muted', text: 'Categories are filled in from this account\'s latest transactions with the same description (or, failing that, from all accounts, shown in orange). ' +
      'Change or clear any of them. HomeBank keeps a category from the file; only an "overwrite" rule replaces it. Transfers, splits and rows that already have a category are left as they are.' }),
    res.problems.length ? h('div', { class: 'notice warn', text: `Lines HomeBank may not read as transactions: ${res.problems.join(', ')}.` }) : null,
    facts,
    h('datalist', { id: 'category-list' }, (app.categories || []).map(c => h('option', { value: c }))),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['Date', 'Description', 'Payee', 'Amount', 'Category to write', 'Why', ''].map(t => h('th', { text: t })))),
      h('tbody', {}, body))),
    h('div', { class: 'toolbar' }, h('span', { class: 'spacer' }), save),
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

const VIEWS = { file: viewFile, analyse: viewAnalyse, review: viewReview, imports: viewImports, reports: viewReports, results: viewResults, export: viewExport };

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
