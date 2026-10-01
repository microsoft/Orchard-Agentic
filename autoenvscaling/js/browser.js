// Environment browser (#env-browser): a filter bar, a list of generated Harbor tasks, and a detail
// pane with tabs for the instruction, files, checks, notes, and screenshot. Deep link: #env=<id>.
// Model-written text only ever goes in through textContent or renderMarkdown, never raw HTML.
import { loadJSON, el, fmtInt, hashParam, setHash } from './util.js';
import { renderMarkdown } from './md.js';

const DEFAULT_ID = 'case-03-pytorch-model-recovery__claude-opus-5__sample-01'; // the paper's example
const NARROW = '(max-width: 899px)';

const DOMAINS = [
  ['terminal', 'Terminal'],
  ['browser', 'Browser'],
  ['desktop', 'Computer use (desktop)'],
  ['gpu-kernel', 'GPU kernel'],
  ['professional', 'Professional work'],
  ['swe', 'Software engineering'],
  ['tool', 'Tool use'],
];
const DOMAIN_LABEL = Object.fromEntries(DOMAINS);
const SOURCES = [['all', 'All'], ['failure-study', 'Failure study'], ['cross-domain', 'Cross-domain']];
const SOURCE_LABEL = Object.fromEntries(SOURCES.slice(1));

// Admission gates: the short label on list cards, and what runs, for the Checks table.
const GATES = {
  oracle: { chip: 'oracle', name: 'Oracle', runs: 'The reference solution' },
  noop: { chip: 'no-op', name: 'No-op', runs: 'Nothing: the tests run on the untouched environment' },
  'reset-oracle': { chip: 'reset', name: 'Reset-oracle', runs: 'The reference solution, on a fresh instance' },
};

const NOT_ADMITTED = 'It was reviewed by hand for build safety, so it has no gate rewards.';

// Module state (the page has one browser).
const S = {
  tasks: [],
  byId: new Map(),
  hay: new Map(), // id -> lower-case search text
  about: null,
  filters: { q: '', domain: 'all', source: 'all', model: 'all' },
  selected: null,
  tab: 'instruction', // last tab the user chose; kept across tasks when available
  tabs: [],
  token: 0, // guards against a slow detail load landing after a newer selection
};
const ui = {};

export async function init(root) {
  const [tasks, about] = await Promise.all([
    loadJSON('tasks/index.json'),
    loadJSON('tasks/about.json').catch(() => null),
  ]);
  S.tasks = tasks;
  S.about = about;
  for (const t of tasks) {
    S.byId.set(t.id, t);
    S.hay.set(t.id, [t.title, t.case_title, t.model, t.harness, DOMAIN_LABEL[t.domain], t.domain]
      .filter(Boolean).join(' ').toLowerCase());
  }
  root.replaceChildren(build());

  const want = hashParam('env');
  const start = S.byId.has(want) ? want : S.byId.has(DEFAULT_ID) ? DEFAULT_ID : tasks[0]?.id;
  if (start) reveal(start);
  else applyFilters();
  if (want) scrollToSection(true);

  window.addEventListener('hashchange', () => {
    const id = hashParam('env');
    if (!id || !S.byId.has(id)) return;
    reveal(id);
    scrollToSection(false);
  });
}

/* ---------- Layout ---------- */

function build() {
  // Filter bar
  ui.search = el('input', {
    type: 'search', class: 'eb-input', placeholder: 'Search title, case, or model',
    autocomplete: 'off', spellcheck: 'false', oninput: onSearch,
  });
  ui.sourceBtns = SOURCES.map(([key, label]) => el('button', {
    type: 'button', 'data-key': key, 'aria-pressed': 'false', onclick: () => setFilter('source', key),
  }, label));
  const models = [...new Set(S.tasks.map((t) => t.model).filter(Boolean))].sort();
  ui.model = el('select', { class: 'select', onchange: (e) => setFilter('model', e.target.value) },
    el('option', { value: 'all' }, 'All models'),
    models.map((m) => el('option', { value: m }, m)));
  ui.domainBtns = [['all', 'All'], ...DOMAINS]
    .filter(([key]) => key === 'all' || S.tasks.some((t) => t.domain === key))
    .map(([key, label]) => el('button', {
      type: 'button', class: 'eb-dom', 'data-key': key, 'data-label': label, 'aria-pressed': 'false',
      onclick: () => setFilter('domain', key),
    }, label, el('span', { class: 'eb-n', 'aria-hidden': 'true' })));
  ui.count = el('p', { class: 'eb-count', 'aria-live': 'polite' });

  const filters = el('div', { class: 'eb-filters' },
    el('div', { class: 'eb-row' },
      el('label', { class: 'eb-search' }, el('span', { class: 'visually-hidden' }, 'Search environments'), ui.search),
      el('div', { class: 'seg', role: 'group', 'aria-label': 'Source' }, ui.sourceBtns),
      el('label', { class: 'eb-model' }, el('span', { class: 'visually-hidden' }, 'Proposer model'), ui.model)),
    el('div', { class: 'eb-domains', role: 'group', 'aria-label': 'Domain' }, ui.domainBtns),
    ui.count);

  // List (a listbox of buttons with a roving tabindex)
  ui.cards = S.tasks.map(card);
  ui.cardById = new Map(ui.cards.map((c) => [c.dataset.id, c]));
  ui.list = el('div', { class: 'eb-list', role: 'listbox', 'aria-label': 'Environments', onkeydown: onListKey }, ui.cards);
  ui.empty = el('div', { class: 'eb-empty', hidden: true },
    el('p', {}, 'No environments match these filters.'),
    el('button', { type: 'button', class: 'eb-clear', onclick: clearFilters }, 'Clear filters'));

  // Detail
  ui.head = el('div', { class: 'eb-dhead' });
  ui.tabs = el('div', { class: 'eb-tabs', role: 'tablist', 'aria-label': 'Environment contents', onkeydown: onTabKey });
  ui.panel = el('div', { class: 'eb-panel', id: 'eb-panel', role: 'tabpanel', tabindex: '0' },
    el('p', { class: 'loading' }, 'Loading…'));
  ui.detail = el('article', { class: 'eb-detail', 'aria-label': 'Selected environment' }, ui.head, ui.tabs, ui.panel);

  return el('div', { class: 'eb' }, filters,
    el('div', { class: 'eb-layout' }, el('div', { class: 'eb-master' }, ui.list, ui.empty), ui.detail));
}

/** One list card. */
function card(t) {
  const status = t.source === 'cross-domain'
    ? (t.checks || []).map((c) => el('span', { class: 'eb-gate' }, `${GATES[c.name]?.chip || c.name} ${fmtReward(c.reward)}`))
    : [el('span', { class: 'eb-tag eb-tag-muted' }, 'not admitted'),
      t.case_title ? el('span', { class: 'eb-case' }, t.case_title) : null];
  return el('button', {
    type: 'button', role: 'option', class: 'eb-card', 'data-id': t.id, 'aria-selected': 'false', tabindex: '-1',
    onclick: () => pick(t.id),
  },
  el('span', { class: 'eb-card-title' }, titleNodes(t.title || t.id)),
  el('span', { class: 'eb-card-meta' }, [DOMAIN_LABEL[t.domain] || t.domain, t.model, t.harness].filter(Boolean).join(' · ')),
  el('span', { class: 'eb-card-status' }, status));
}

/* ---------- Filters ---------- */

let searchTimer;
function onSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => setFilter('q', ui.search.value.trim()), 180);
}

function setFilter(key, value) {
  if (S.filters[key] === value) return;
  S.filters[key] = value;
  applyFilters(true);
}

function clearFilters() {
  S.filters = { q: '', domain: 'all', source: 'all', model: 'all' };
  applyFilters(true);
}

function matchesQuery(id, q) {
  if (!q) return true;
  const hay = S.hay.get(id) || '';
  return q.toLowerCase().split(/\s+/).every((w) => hay.includes(w));
}

/** Does task t pass the filters? `skip` ignores one filter (for the domain counts). */
function matches(t, f, skip) {
  return (skip === 'domain' || f.domain === 'all' || t.domain === f.domain)
    && (f.source === 'all' || t.source === f.source)
    && (f.model === 'all' || t.model === f.model)
    && matchesQuery(t.id, f.q);
}

/** Show the cards that pass the filters and sync every control to the filter state. */
function applyFilters(user = false) {
  const f = S.filters;
  let shown = 0;
  let last = null;
  for (const c of ui.cards) {
    c.hidden = !matches(S.byId.get(c.dataset.id), f);
    if (!c.hidden) { shown++; last = c; }
  }
  for (const c of ui.cards) c.classList.toggle('is-last', c === last); // no hairline under the last row
  ui.count.textContent = `${shown} environment${shown === 1 ? '' : 's'}`;
  ui.list.hidden = shown === 0;
  ui.empty.hidden = shown > 0;

  // Controls can also change from a deep link, so set them from the state.
  if (ui.search.value.trim() !== f.q) ui.search.value = f.q;
  ui.model.value = f.model;
  for (const b of ui.sourceBtns) b.setAttribute('aria-pressed', String(b.dataset.key === f.source));
  for (const b of ui.domainBtns) {
    const key = b.dataset.key;
    const n = S.tasks.filter((t) => (key === 'all' || t.domain === key) && matches(t, f, 'domain')).length;
    const on = key === f.domain;
    b.querySelector('.eb-n').textContent = n;
    b.setAttribute('aria-pressed', String(on));
    b.setAttribute('aria-label', `${b.dataset.label} (${n})`);
    b.disabled = n === 0 && !on;
  }

  // If the user filtered the selection away, follow the list with its first match.
  const sel = ui.cardById.get(S.selected);
  if (user && shown && (!sel || sel.hidden)) {
    const first = ui.cards.find((c) => !c.hidden);
    ui.list.scrollTop = 0;
    select(first.dataset.id, { user: true });
  } else {
    updateCards();
  }
}

/* ---------- Selection ---------- */

/** A click on a card. On narrow screens the detail sits below the list, so bring it into view. */
function pick(id) {
  select(id, { user: true });
  if (window.matchMedia(NARROW).matches) ui.detail.scrollIntoView({ block: 'start' });
}

function select(id, { user = false } = {}) {
  if (!S.byId.has(id)) return;
  if (user) setHash('env', id);
  if (id === S.selected) { updateCards(); return; }
  S.selected = id;
  updateCards();
  showDetail(S.byId.get(id));
}

/** Mark the selected card; exactly one visible card is in the tab order. */
function updateCards() {
  const sel = ui.cardById.get(S.selected);
  for (const c of ui.cards) c.setAttribute('aria-selected', String(c === sel));
  const stop = sel && !sel.hidden ? sel : ui.cards.find((c) => !c.hidden);
  for (const c of ui.cards) c.tabIndex = c === stop ? 0 : -1;
}

/** Select a task from a deep link: clear any filter that would hide it, then show it. */
function reveal(id) {
  const t = S.byId.get(id);
  const f = S.filters;
  if (f.domain !== 'all' && f.domain !== t.domain) f.domain = 'all';
  if (f.source !== 'all' && f.source !== t.source) f.source = 'all';
  if (f.model !== 'all' && f.model !== t.model) f.model = 'all';
  if (!matchesQuery(id, f.q)) f.q = '';
  applyFilters();
  select(id);
  scrollWithin(ui.list, ui.cardById.get(id), true);
}

function onListKey(e) {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
  const vis = ui.cards.filter((c) => !c.hidden);
  if (!vis.length) return;
  e.preventDefault();
  const i = vis.indexOf(e.target.closest('.eb-card'));
  const j = e.key === 'Home' ? 0 : e.key === 'End' ? vis.length - 1 : i + (e.key === 'ArrowDown' ? 1 : -1);
  const next = vis[Math.max(0, Math.min(vis.length - 1, j))];
  select(next.dataset.id, { user: true });
  next.focus({ preventScroll: true });
  scrollWithin(ui.list, next);
}

function scrollToSection(initial) {
  const target = document.getElementById('environments') || ui.detail;
  const go = () => target.scrollIntoView({ block: 'start' });
  requestAnimationFrame(go);
  // Sections above load their own data; land again once the page has settled.
  if (initial && document.readyState !== 'complete') window.addEventListener('load', go, { once: true });
}

/* ---------- Detail ---------- */

async function showDetail(t) {
  const token = ++S.token;
  ui.detail.style.minHeight = `${ui.detail.offsetHeight}px`; // no jump while the new task loads
  ui.detail.classList.add('is-loading');
  renderHead(t);
  ui.tabs.replaceChildren();
  ui.panel.removeAttribute('aria-labelledby');
  ui.panel.replaceChildren(el('p', { class: 'loading' }, 'Loading files…'));

  let d = null;
  try {
    d = await loadJSON(`tasks/${encodeURIComponent(t.id)}.json`);
  } catch (err) {
    console.error('environment', t.id, err);
  }
  if (token !== S.token) return;
  ui.detail.classList.remove('is-loading');
  ui.detail.style.minHeight = '';
  if (!d) {
    ui.panel.replaceChildren(el('p', { class: 'loading' }, 'Could not load this environment.'));
    return;
  }
  renderTabs(buildTabs(d));
}

function renderHead(t) {
  const failure = t.source === 'failure-study';
  const status = failure
    ? `Written from a failed Terminal-Bench run${t.case_title ? ` (${t.case_title})` : ''}. ${sentence(t.status)}`
    : sentence(t.status);
  const tag = (key, value, extra = '') => el('span', { class: `eb-tag ${extra}` },
    el('span', { class: 'eb-k' }, key), ' ', value);
  ui.head.replaceChildren(...[
    el('h3', { class: 'eb-dtitle' }, titleNodes(t.title || t.id)),
    el('div', { class: 'eb-tags' },
      tag('domain', DOMAIN_LABEL[t.domain] || t.domain),
      tag('source', SOURCE_LABEL[t.source] || t.source),
      t.model ? tag('model', t.model) : null,
      t.harness ? tag('harness', t.harness) : null),
    status.trim() ? el('p', { class: 'eb-status' }, status) : null,
    failure && t.episode_id
      ? el('a', { class: 'more eb-episode', href: `#replay=${encodeURIComponent(t.episode_id)}` }, 'Watch the episode that wrote it')
      : null,
  ].filter(Boolean)); // replaceChildren() would print a null as "null"
}

/** The tabs this task has something to show for. */
function buildTabs(d) {
  const files = d.files || [];
  const inGroup = (g) => files.filter((f) => f.group === g);
  const readme = files.find((f) => f.path === 'README.md' && f.text);
  const notes = readme ? readme.text : d.notes; // the README file is the untruncated copy of notes
  const other = files.filter((f) => f.group === 'other' && f !== readme);
  const env = [...(d.task_toml ? [{ path: 'task.toml', text: d.task_toml }] : []), ...inGroup('environment')];
  const sol = inGroup('solution');
  const tests = inGroup('tests');
  const gate = inGroup('gate');
  const shots = (d.screenshots || []).filter((s) => /^assets\//.test(s));

  const tabs = [];
  if (d.instruction && d.instruction.trim()) {
    tabs.push({ key: 'instruction', label: 'Instruction', render: () => renderMarkdown(stripTitle(d.instruction, d.title)) });
  }
  if (env.length) tabs.push({ key: 'environment', label: 'Environment', count: env.length, render: () => fileView(env, ['Dockerfile', 'task.toml']) });
  if (sol.length) tabs.push({ key: 'solution', label: 'Solution', count: sol.length, render: () => fileView(sol, ['solve.sh']) });
  if (tests.length) tabs.push({ key: 'tests', label: 'Tests', count: tests.length, render: () => fileView(tests, ['test_outputs.py', 'test.sh']) });
  tabs.push({ key: 'checks', label: 'Checks', render: () => checksView(d, gate) });
  if ((notes && notes.trim()) || other.length) tabs.push({ key: 'notes', label: 'Notes', render: () => notesView(notes, other) });
  if (shots.length) tabs.push({ key: 'screenshot', label: 'Screenshot', render: () => shotView(d, shots) });
  return tabs;
}

function renderTabs(tabs) {
  S.tabs = tabs;
  ui.tabs.replaceChildren(...tabs.map((tab) => el('button', {
    type: 'button', role: 'tab', id: `eb-tab-${tab.key}`, 'data-key': tab.key,
    'aria-controls': 'eb-panel', 'aria-selected': 'false', tabindex: '-1',
    'aria-label': tab.count ? `${tab.label} (${tab.count} file${tab.count === 1 ? '' : 's'})` : null,
    onclick: () => { S.tab = tab.key; showTab(tab.key); },
  }, tab.label, tab.count ? el('span', { class: 'eb-tabn' }, String(tab.count)) : null)));
  showTab(tabs.some((t) => t.key === S.tab) ? S.tab : tabs[0].key);
}

function showTab(key) {
  for (const b of ui.tabs.children) {
    const on = b.dataset.key === key;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  }
  ui.panel.setAttribute('aria-labelledby', `eb-tab-${key}`);
  ui.panel.replaceChildren(S.tabs.find((t) => t.key === key).render());
}

function onTabKey(e) {
  const btns = [...ui.tabs.children];
  const i = btns.indexOf(e.target);
  if (i < 0) return;
  const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: btns.length - 1 }[e.key];
  if (j === undefined) return;
  e.preventDefault();
  const next = btns[(j + btns.length) % btns.length];
  next.focus();
  next.click();
}

/* ---------- Tab contents ---------- */

/** A file tree (grouped by folder) beside a code viewer. */
function fileView(files, prefer) {
  const viewer = el('div', { class: 'eb-viewer' });
  const nav = el('div', { class: 'eb-files', role: 'group', 'aria-label': 'Files' });
  const buttons = new Map();
  for (const [dir, list] of byFolder(files)) {
    if (dir) nav.append(el('div', { class: 'eb-dir' }, `${dir}/`));
    for (const f of list) {
      const b = el('button', { type: 'button', class: dir ? 'eb-file in-dir' : 'eb-file', title: f.path, onclick: () => open(f) },
        el('span', { class: 'eb-fname' }, basename(f.path)),
        el('span', { class: 'eb-fsize' }, sizeLabel(f)));
      buttons.set(f, b);
      nav.append(b);
    }
  }
  function open(f) {
    for (const [g, b] of buttons) {
      if (g === f) b.setAttribute('aria-current', 'true');
      else b.removeAttribute('aria-current');
    }
    viewer.replaceChildren(codeView(f));
  }
  const first = pickDefault(files, prefer);
  open(first);
  if (files.length < 2) return el('div', { class: 'eb-fileview is-single' }, viewer);
  requestAnimationFrame(() => scrollWithin(nav, buttons.get(first)));
  return el('div', { class: 'eb-fileview' }, nav, viewer);
}

/** Files grouped by folder: root files first, then folders in path order. */
function byFolder(files) {
  const groups = new Map();
  for (const f of files) {
    const cut = f.path.lastIndexOf('/');
    const dir = cut < 0 ? '' : f.path.slice(0, cut);
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(f);
  }
  return [...groups].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

function pickDefault(files, prefer) {
  for (const name of prefer) {
    const hit = files.find((f) => basename(f.path) === name && !f.binary);
    if (hit) return hit;
  }
  return files.find((f) => !f.binary) || files[0];
}

/** Monospace viewer with line numbers; it scrolls inside itself, never the page. */
function codeView(f) {
  const head = el('div', { class: 'eb-code-head' },
    el('span', { class: 'eb-code-path' }, f.path),
    el('span', { class: 'eb-code-size' }, sizeLabel(f)));
  if (f.binary || f.text == null) {
    const note = f.note ? f.note[0].toUpperCase() + f.note.slice(1) : 'Binary file';
    return el('div', { class: 'eb-code-box' }, head, el('p', { class: 'eb-code-msg' }, `${note}. Contents not shown.`));
  }
  if (f.text === '') return el('div', { class: 'eb-code-box' }, head, el('p', { class: 'eb-code-msg' }, 'Empty file.'));
  const text = f.text.endsWith('\n') ? f.text.slice(0, -1) : f.text;
  const n = text.split('\n').length;
  const nums = Array.from({ length: n }, (_, i) => i + 1).join('\n');
  return el('div', { class: 'eb-code-box' }, head,
    el('div', { class: 'eb-code', tabindex: '0', role: 'region', 'aria-label': `Contents of ${f.path}` },
      el('pre', { class: 'eb-gutter', 'aria-hidden': 'true' }, nums),
      el('pre', { class: 'eb-src' }, el('code', {}, text))));
}

function checksView(d, gateFiles) {
  const out = el('div', { class: 'eb-checks' });
  const checks = d.checks || [];
  if (checks.length) {
    out.append(
      el('p', {}, `${d.source === 'cross-domain' ? 'Admitted. ' : ''}Rewards measured by the admission gates:`),
      el('div', { class: 'eb-table-wrap' },
        el('table', { class: 'eb-table' },
          el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, 'Gate'), el('th', { scope: 'col' }, 'What runs'),
            el('th', { scope: 'col', class: 'num' }, 'Reward'))),
          el('tbody', {}, checks.map((c) => el('tr', {},
            el('th', { scope: 'row' }, GATES[c.name]?.name || c.name),
            el('td', {}, GATES[c.name]?.runs || ''),
            el('td', { class: 'num' }, el('span', { class: 'eb-gate' }, fmtReward(c.reward)))))))));
    const caveat = d.source === 'cross-domain' ? S.about?.cross_domain?.caveat : null;
    if (caveat) out.append(el('p', { class: 'eb-muted' }, el('b', {}, 'Caveat.'), ' ', caveat));
  } else {
    out.append(el('p', { class: 'eb-callout' }, el('b', {}, 'Not run through admission. '), NOT_ADMITTED));
    if (d.source === 'failure-study') {
      out.append(el('p', { class: 'eb-label' }, 'Hand review'));
      out.append(d.review ? renderMarkdown(d.review) : el('p', { class: 'eb-muted' }, 'No review text is included for this task.'));
    }
  }
  if (gateFiles.length) {
    out.append(el('p', { class: 'eb-label' }, 'Output from the oracle gate'), fileView(gateFiles, ['test-stdout.txt']));
  }
  return out;
}

function notesView(notes, other) {
  return el('div', {},
    notes && notes.trim() ? renderMarkdown(notes) : null,
    other.length ? [el('p', { class: 'eb-label' }, 'Other files in the task'), fileView(other, [])] : null);
}

/** Screenshot of the final state; for cross-domain tasks `review` holds its caption. */
function shotView(d, shots) {
  const caption = d.source === 'cross-domain' ? d.review : null;
  return el('div', { class: 'eb-shots' }, shots.map((src) => el('figure', { class: 'eb-shot' },
    el('a', { href: src, target: '_blank', rel: 'noopener' },
      el('img', { src, loading: 'lazy', alt: `Final state of the environment after the reference solution ran: ${d.title || d.id}` })),
    caption ? el('figcaption', {}, caption) : null)));
}

/* ---------- Helpers ---------- */

/** Render `code` spans in a title as <code>, the rest as text. */
function titleNodes(text) {
  const parts = String(text).split('`');
  if (parts.length < 3 || parts.length % 2 === 0) return String(text);
  return parts.map((p, i) => (i % 2 ? el('code', {}, p) : p)).filter((p) => p !== '');
}

/** Drop a leading "# Title" line that repeats the header. */
function stripTitle(md, title) {
  const m = md.match(/^\s*#\s+(.+)\n?/);
  return m && title && m[1].trim() === title.trim() ? md.slice(m[0].length) : md;
}

function sentence(s) {
  if (!s) return '';
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

const fmtReward = (r) => (r == null ? 'n/a' : Number(r).toFixed(1));
const basename = (p) => p.slice(p.lastIndexOf('/') + 1);

function lineCount(text) {
  if (!text) return 0;
  const n = text.split('\n').length;
  return text.endsWith('\n') ? n - 1 : n;
}

function sizeLabel(f) {
  if (f.binary || f.text == null) return 'binary';
  const n = lineCount(f.text);
  return `${fmtInt(n)} line${n === 1 ? '' : 's'}`;
}

/** Scroll a list so `child` is visible, without moving the page. `box` must be positioned. */
function scrollWithin(box, child, center = false) {
  if (!box || !child || child.hidden) return;
  const top = child.offsetTop;
  const bottom = top + child.offsetHeight;
  if (center) box.scrollTop = top - (box.clientHeight - child.offsetHeight) / 2;
  else if (top < box.scrollTop) box.scrollTop = top - 4;
  else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight + 4;
}
