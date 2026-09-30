// Episode replay: pick a case, an author, and a sample, then step through the proposer's turns
// with a stage timeline, a player, a turn list, and the current turn in full.
import { loadJSON, el, STAGES, fmtInt, hashParam, setHash } from './util.js';
import { renderMarkdown } from './md.js';

const AUTHORS = [['opus', 'Claude Opus 5'], ['qwen', 'Qwen3.6-35B-A3B']];
const SAMPLES = [1, 2, 3];
const SPEEDS = [1, 2, 4];
const STEP_MS = 1600;
const ORDER = ['R', 'B', 'W', 'T'];
const TRUNC = /^… \[truncated \d+ chars\]$/;
const WIDE = window.matchMedia('(min-width: 900px)');
const SOURCE_NOTE = {
  hand: 'Labelled by hand.',
  classifier: 'Labelled automatically.',
};

const stageKey = (s) => (STAGES[s] ? s : 'none');
const stageName = (s) => STAGES[s]?.name || 'Unlabelled';

/** A stage chip. `named` adds the stage name for screen readers; otherwise the chip is hidden. */
function chip(s, named = false) {
  return el('span', { class: `stage s-${stageKey(s)}`, title: stageName(s), 'aria-hidden': named ? null : 'true' },
    STAGES[s] ? s : '–',
    named ? el('span', { class: 'visually-hidden' }, ` (${stageName(s)})`) : null);
}

/** Segmented buttons with aria-pressed; returns the group and a setter for the pressed value. */
function segmented(label, options, onPick) {
  const buttons = options.map(([value, text]) =>
    el('button', { type: 'button', 'aria-pressed': 'false', onclick: () => onPick(value) }, text));
  const group = el('div', { class: 'seg', role: 'group', 'aria-label': label }, buttons);
  const set = (v) => buttons.forEach((b, k) => b.setAttribute('aria-pressed', String(options[k][0] === v)));
  return { group, set };
}

/** First non-empty line of a title, without the truncation marker. */
const firstLine = (s) => (s || '').split('\n').find((l) => l.trim() && !TRUNC.test(l.trim())) || '';

/** Short text for a turn-list row. */
function rowText(t) {
  if (t.kind === 'text') return (t.text || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  let s = firstLine(t.title);
  if (s === t.tool) s = firstLine(t.input);
  else if (s.startsWith(`${t.tool} `)) s = s.slice(t.tool.length + 1);
  return s.replace(/\/workspace\/episode\//g, '').slice(0, 160);
}

/** Title for the terminal header, or '' when it would only repeat the tool or the command. */
function headLabel(t) {
  let s = firstLine(t.title);
  if (s === t.tool) return '';
  if (s.startsWith(`${t.tool} `)) s = s.slice(t.tool.length + 1);
  // Some harnesses use the command itself as the title; the panel already shows it.
  if (t.tool === 'Bash' && (t.input || '').startsWith(s.replace(/…$/, '').trimEnd())) return '';
  return s;
}

/** Lines of monospace text; truncation markers render muted. */
function block(cls, text, content = (l) => [l], lineCls = () => '') {
  return el('div', { class: `rp-block ${cls}` }, text.split('\n').map((l, k) => (TRUNC.test(l.trim())
    ? el('div', { class: 'rp-line rp-trunc' }, l.trim())
    : el('div', { class: `rp-line ${lineCls(l)}`.trim() }, ...content(l || ' ', k)))));
}

function toolView(t) {
  const input = t.input || '';
  const output = t.hidden ? '' : (t.output || '');
  const label = headLabel(t);
  const body = el('div', { class: 'rp-term-body', tabindex: '0', 'aria-label': `Turn ${t.i}, ${t.tool}` });
  if (t.tool === 'Bash') {
    if (input) body.append(block('rp-cmd', input, (l, k) => [k ? '' : el('span', { class: 'rp-prompt', 'aria-hidden': 'true' }, '$ '), l]));
  } else if (t.tool === 'Write') {
    let n = 0;
    if (input) body.append(block('rp-code', input, (l) => [el('span', { class: 'rp-ln', 'aria-hidden': 'true' }, String(++n)), l]));
  } else if (t.tool === 'Edit') {
    if (input) body.append(block('rp-diff', input, undefined, (l) => (l.startsWith('- ') ? 'rp-del' : l.startsWith('+ ') ? 'rp-add' : '')));
  } else if (input) {
    body.append(block('rp-plain', input));
  }
  if (output.trim()) body.append(block('rp-out', output));
  if (!body.childNodes.length && !t.hidden) body.append(el('div', { class: 'rp-line rp-trunc' }, 'No output.'));

  return el('div', { class: 'rp-term' },
    el('div', { class: 'rp-term-head' },
      chip(t.stage, true),
      el('span', { class: 'rp-term-tool' }, t.tool || 'Tool'),
      label ? el('span', { class: 'rp-term-title' }, label) : null),
    body.childNodes.length ? body : null,
    // Hidden outputs carry an explanatory note instead of terminal text.
    t.hidden && t.output ? el('p', { class: 'rp-notice' }, t.output) : null);
}

function textView(t, author) {
  return el('div', { class: `rp-msg is-${author.key}` },
    el('div', { class: 'rp-msg-head' }, chip(t.stage, true), el('b', {}, author.model), el('span', {}, 'says')),
    el('div', { class: 'rp-msg-body', tabindex: '0', 'aria-label': `Turn ${t.i}, message` }, renderMarkdown((t.text || '').trim())));
}

function didList(items) {
  return el('ul', { class: 'rp-did' }, items.map((d) => el('li', { class: d.ok ? 'is-ok' : 'is-bad' },
    el('span', { class: 'rp-mark', 'aria-hidden': 'true' }, d.ok ? '✓' : '✗'),
    el('span', { class: 'visually-hidden' }, d.ok ? 'Done: ' : 'Failed: '),
    el('span', {}, d.text))));
}

/** First sentence of the study's note, for the collapsed view. */
const firstSentence = (t) => (t.match(/^.*?[.!?](?=\s|$)/) || [t])[0];

function seedCard(entry, d) {
  const seed = d.seed || {};
  const summary = seed.solver_summary || [];
  const more = seed.failure || seed.instruction
    ? el('details', { class: 'rp-instr' }, el('summary', {}, 'More about this run'),
      seed.failure ? el('p', { class: 'rp-failure' }, seed.failure) : null,
      seed.instruction ? renderMarkdown(seed.instruction) : null)
    : null;
  return el('div', { class: 'card rp-seed' },
    el('div', { class: 'rp-seed-head' }, el('h3', {}, entry.case_title)),
    entry.seed_task ? el('p', { class: 'rp-seed-task' }, entry.seed_task) : null,
    summary.length
      ? didList(summary)
      : seed.failure ? el('p', { class: 'rp-failure' }, `Why the solver failed: ${firstSentence(seed.failure)}`) : null,
    more);
}

function statsRow(entry, d) {
  const n = (d.turns || []).length;
  const cost = d.cost_usd == null ? 'self-hosted' : `$${Number(d.cost_usd).toFixed(2)}`;
  const wall = !n || d.wall_min == null ? null : d.wall_min < 1 ? 'under 1 min' : `${Math.round(d.wall_min)} min`;
  const task = entry.task_id || d.shipped_task;
  const facts = [`${fmtInt(n)} turns`, wall, cost, d.author?.harness || entry.author.harness].filter(Boolean);
  return el('div', { class: 'rp-statsbar' },
    el('p', { class: 'rp-stats' }, facts.join(' · ')),
    task
      ? el('a', { class: 'rp-shipped', href: `#env=${encodeURIComponent(task)}` }, 'Open the environment it shipped')
      : el('span', { class: 'rp-unshipped' }, 'Shipped no environment'));
}

export async function init(root) {
  const index = await loadJSON('episodes/index.json');
  const byId = new Map(index.map((e) => [e.id, e]));
  const cases = [...new Map(index.map((e) => [e.case, e.case_title]))];
  const find = (c, a, s) => index.find((e) => e.case === c && e.author.key === a && e.sample === s);

  const state = { entry: null, detail: null, cur: 0, playing: false, speed: 1, timer: 0, token: 0 };
  let ui = null; // nodes of the open episode, null when it has no turns

  // A page-wide live region would read out every autoplay step; only the turn counter is live.
  root.removeAttribute('aria-live');
  root.setAttribute('tabindex', '0');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', 'Episode replay. Space plays or pauses; left and right arrows step through turns.');

  const caseSelect = el('select', { class: 'select', id: 'rp-case', onchange: () => pick(caseSelect.value, state.entry.author.key, state.entry.sample) },
    cases.map(([id, title]) => el('option', { value: id }, title)));
  const authorSeg = segmented('Author', AUTHORS, (a) => pick(state.entry.case, a, state.entry.sample));
  const sampleSeg = segmented('Sample', SAMPLES.map((s) => [s, String(s)]), (s) => pick(state.entry.case, state.entry.author.key, s));
  const body = el('div', { class: 'rp-body' });
  root.replaceChildren(
    el('div', { class: 'rp-controls' },
      el('div', { class: 'rp-field' }, el('label', { class: 'rp-label', for: 'rp-case' }, 'Case'), caseSelect),
      el('div', { class: 'rp-field' }, el('span', { class: 'rp-label', 'aria-hidden': 'true' }, 'Author'), authorSeg.group),
      el('div', { class: 'rp-field' }, el('span', { class: 'rp-label', 'aria-hidden': 'true' }, 'Sample'), sampleSeg.group)),
    body);

  const stripRO = new ResizeObserver(() => fitStrip());
  function fitStrip() {
    if (!ui) return;
    const w = ui.strip.clientWidth / ui.cells.length;
    ui.strip.classList.toggle('is-lettered', w >= 14);
    ui.strip.classList.toggle('is-dense', w < 5);
  }

  function pick(c, a, s) {
    const e = find(c, a, s);
    if (!e || e === state.entry) return;
    setHash('replay', e.id);
    open(e);
  }

  async function open(entry) {
    pause();
    state.entry = entry;
    caseSelect.value = entry.case;
    authorSeg.set(entry.author.key);
    sampleSeg.set(entry.sample);
    const token = ++state.token;
    body.classList.add('is-loading');
    body.setAttribute('aria-busy', 'true');
    let detail = null;
    try {
      detail = await loadJSON(`episodes/${entry.id}.json`);
    } catch (err) {
      console.error('episode', err);
    }
    if (token !== state.token) return; // a newer pick won
    body.classList.remove('is-loading');
    body.removeAttribute('aria-busy');
    if (!detail) {
      ui = null;
      body.replaceChildren(el('p', { class: 'loading' }, 'Could not load this episode.'));
      return;
    }
    state.detail = detail;
    render(entry, detail);
  }

  function render(entry, d) {
    const turns = d.turns || [];
    const parts = [seedCard(entry, d), statsRow(entry, d)];
    ui = null;
    state.cur = 0;
    stripRO.disconnect();
    if (!turns.length) {
      parts.push(el('p', { class: 'rp-empty' }, 'This run ended before the proposer produced any output.'));
      body.replaceChildren(...parts);
      return;
    }

    // Stage timeline: one cell per turn; one click listener for the strip.
    const counts = { R: 0, B: 0, W: 0, T: 0, none: 0 };
    for (const t of turns) counts[stageKey(t.stage)]++;
    const cells = turns.map((t, k) => el('button', {
      type: 'button', class: `rp-cell s-${stageKey(t.stage)}`, tabindex: '-1', 'data-turn': k,
      'aria-label': `Turn ${k + 1}, ${stageName(t.stage)}`,
    }, el('span', { 'aria-hidden': 'true' }, STAGES[t.stage] ? t.stage : '')));
    const strip = el('div', { class: 'rp-strip', role: 'group', 'aria-label': `Stage of each turn, ${turns.length} turns` }, cells);
    const timeline = el('div', { class: 'rp-timeline' },
      strip,
      el('p', { class: 'rp-counts' },
        ORDER.map((s) => el('span', { class: 'rp-count' }, chip(s), ` ${STAGES[s].name.toLowerCase()} ${counts[s]}`)),
        counts.none ? el('span', { class: 'rp-count' }, chip(null), ` unlabelled ${counts.none}`) : null,
        SOURCE_NOTE[d.stage_source] ? el('span', { class: 'rp-source' }, SOURCE_NOTE[d.stage_source]) : null));

    // Player.
    const prev = el('button', { type: 'button', class: 'rp-btn', title: 'Previous turn (left arrow)', onclick: () => show(state.cur - 1) },
      el('span', { 'aria-hidden': 'true' }, '‹ '), 'Prev');
    const play = el('button', { type: 'button', class: 'rp-btn rp-play', title: 'Play or pause (space)', onclick: toggle }, 'Play');
    const next = el('button', { type: 'button', class: 'rp-btn', title: 'Next turn (right arrow)', onclick: () => show(state.cur + 1) },
      'Next', el('span', { 'aria-hidden': 'true' }, ' ›'));
    const speedSeg = segmented('Playback speed', SPEEDS.map((s) => [s, `${s}x`]), (s) => {
      state.speed = s;
      speedSeg.set(s);
      if (state.playing) schedule();
    });
    speedSeg.set(state.speed);
    const counter = el('span', { class: 'rp-counter', 'aria-live': 'polite' });
    const player = el('div', { class: 'rp-player' },
      el('div', { class: 'rp-buttons' }, prev, play, next),
      el('div', { class: 'rp-field' }, el('span', { class: 'rp-label', 'aria-hidden': 'true' }, 'Speed'), speedSeg.group),
      counter);

    // Turn list (left on wide screens, behind a disclosure on narrow ones) and the current turn.
    const rows = turns.map((t, k) => el('button', { type: 'button', class: 'rp-row', tabindex: '-1', 'data-turn': k },
      el('span', { class: 'rp-row-num', 'aria-hidden': 'true' }, k + 1),
      chip(t.stage),
      el('span', { class: 'visually-hidden' }, `Turn ${k + 1}, ${stageName(t.stage)}: `),
      el('span', { class: 'rp-row-tool' }, t.kind === 'text' ? 'says' : t.tool),
      el('span', { class: 'rp-row-text' }, rowText(t))));
    const list = el('ol', { class: 'rp-list' }, rows.map((r) => el('li', {}, r)));
    const listWrap = el('details', { class: 'rp-listwrap', open: WIDE.matches },
      el('summary', {}, `All turns (${turns.length})`), list);
    listWrap.addEventListener('toggle', () => { if (listWrap.open) keepRowInView(true); });
    const view = el('div', { class: 'rp-view' });

    const jump = (e) => {
      const c = e.target.closest('[data-turn]');
      if (c) show(+c.dataset.turn);
    };
    strip.addEventListener('click', jump);
    list.addEventListener('click', jump);

    parts.push(timeline, player, el('div', { class: 'rp-grid' }, listWrap, view));
    if (d.notes) {
      parts.push(el('details', { class: 'card rp-notes' },
        el('summary', {}, 'Authoring notes the proposer wrote'), renderMarkdown(d.notes)));
    }
    body.replaceChildren(...parts);

    ui = { strip, cells, rows, list, listWrap, view, counter, play, prev, next };
    stripRO.observe(strip);
    fitStrip();
    show(0);
  }

  /** Show turn k: highlight it in the strip and list, update the counter, render it. */
  function show(k) {
    if (!ui) return;
    const turns = state.detail.turns;
    const n = turns.length;
    k = Math.max(0, Math.min(n - 1, k));
    const active = document.activeElement;
    const follow = active && (ui.cells.includes(active) ? ui.cells : ui.rows.includes(active) ? ui.rows : null);
    for (const nodes of [ui.cells, ui.rows]) {
      const old = nodes[state.cur];
      old.classList.remove('is-current');
      old.setAttribute('tabindex', '-1');
      old.removeAttribute('aria-current');
      nodes[k].classList.add('is-current');
      nodes[k].setAttribute('tabindex', '0');
      nodes[k].setAttribute('aria-current', 'step');
    }
    const far = Math.abs(k - state.cur) > 1;
    state.cur = k;
    if (follow) follow[k].focus({ preventScroll: true });
    keepRowInView(far);

    ui.counter.textContent = `Turn ${k + 1} of ${n}`;
    ui.prev.setAttribute('aria-disabled', String(k === 0));
    ui.next.setAttribute('aria-disabled', String(k === n - 1));
    const t = turns[k];
    ui.view.replaceChildren(t.kind === 'text' ? textView(t, state.detail.author || state.entry.author) : toolView(t));

    if (state.playing) {
      if (k >= n - 1) pause();
      else schedule();
    }
  }

  /** Scroll the list itself (never the page) so the current row is visible. */
  function keepRowInView(center = false) {
    const { list, rows } = ui;
    const row = rows[state.cur];
    if (!list.clientHeight) return; // collapsed
    const lr = list.getBoundingClientRect();
    const rr = row.getBoundingClientRect();
    if (center && (rr.top < lr.top || rr.bottom > lr.bottom)) {
      list.scrollTop += rr.top - lr.top - (list.clientHeight - rr.height) / 2;
    } else if (rr.top < lr.top) {
      list.scrollTop -= lr.top - rr.top + 4;
    } else if (rr.bottom > lr.bottom) {
      list.scrollTop += rr.bottom - lr.bottom + 4;
    }
  }

  // Autoplay.
  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => show(state.cur + 1), STEP_MS / state.speed);
  }
  function syncPlay() {
    if (ui) ui.play.textContent = state.playing ? 'Pause' : 'Play';
  }
  function play() {
    if (!ui) return;
    if (state.cur >= ui.cells.length - 1) show(0);
    state.playing = true;
    syncPlay();
    schedule();
  }
  function pause() {
    state.playing = false;
    clearTimeout(state.timer);
    syncPlay();
  }
  function toggle() {
    if (state.playing) pause();
    else play();
  }

  // Pause when the section leaves the screen or the tab is hidden.
  new IntersectionObserver(([e]) => { if (!e.isIntersecting) pause(); }).observe(root);
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });

  WIDE.addEventListener('change', () => { if (ui) ui.listWrap.open = WIDE.matches; });

  // Keyboard, while focus is inside the replay.
  root.addEventListener('keydown', (e) => {
    if (!ui || e.altKey || e.ctrlKey || e.metaKey) return;
    const target = e.target;
    if (target.closest('select, input, textarea')) return;
    const inList = !!target.closest('.rp-row');
    if (e.key === ' ' || e.key === 'Spacebar') {
      if (target.closest('a, summary, button:not([data-turn])')) return; // let the control act
      e.preventDefault();
      toggle();
    } else if (e.key === 'ArrowLeft' || (inList && e.key === 'ArrowUp')) {
      e.preventDefault();
      show(state.cur - 1);
    } else if (e.key === 'ArrowRight' || (inList && e.key === 'ArrowDown')) {
      e.preventDefault();
      show(state.cur + 1);
    } else if (target.closest('[data-turn]') && (e.key === 'Home' || e.key === 'End')) {
      e.preventDefault();
      show(e.key === 'Home' ? 0 : ui.cells.length - 1);
    }
  });

  // Deep links: #replay=<id>.
  const fromHash = () => {
    try { return byId.get(hashParam('replay') || ''); } catch { return undefined; }
  };
  const scrollToSection = () => document.getElementById('replay')?.scrollIntoView({ block: 'start' });
  window.addEventListener('hashchange', () => {
    const e = fromHash();
    if (!e) return;
    if (e !== state.entry) open(e);
    scrollToSection();
  });

  const linked = fromHash();
  await open(linked || index.find((e) => e.featured) || index[0]);
  if (linked) {
    scrollToSection();
    // Images above may still be loading and shift the page; scroll again once they are in.
    if (document.readyState !== 'complete') window.addEventListener('load', scrollToSection, { once: true });
  }
}
