// One failure, three proposers: the seed failure for a case, then how GPT-5.6-sol, Claude Opus 5,
// and the Cold-Start Qwen3.6-35B-A3B spent their turns, three marked moments, and what each shipped.
import { loadJSON, el, svg, STAGES } from './util.js';

const ROLES = ['gpt', 'opus', 'qwen'];
const ORDER = ['R', 'B', 'W', 'T'];
const DEFAULT_CASE = 'pytorch';
const MIN_STRIP = 18; // % of the column, so short episodes stay visible
const HEAD_R = 9; // marker radius, px
const MARKS_H = 28; // height of the marker row, px
const LINE_CLASS = { $: 'is-cmd', '✓': 'is-ok', '×': 'is-bad', '✗': 'is-bad', '~': 'is-path', '+': 'is-add' };

// Marker positions depend on the rendered strip width, so each track re-lays itself out on resize.
const layouts = new WeakMap();
const resizer = new ResizeObserver((entries) => entries.forEach((e) => layouts.get(e.target)?.()));

const stageKey = (s) => (STAGES[s] ? s : 'none');
const chip = (s) => el('span', { class: `stage s-${stageKey(s)}`, title: STAGES[s]?.name || 'Unlabelled', 'aria-hidden': 'true' }, s);

function countStages(stages) {
  const counts = { R: 0, B: 0, W: 0, T: 0, none: 0 };
  for (const s of stages) counts[stageKey(s)]++;
  return counts;
}

function didList(items) {
  return el('ul', { class: 'cmp-did' }, items.map((d) => el('li', { class: d.ok ? 'is-ok' : 'is-bad' },
    el('span', { class: 'cmp-mark', 'aria-hidden': 'true' }, d.ok ? '✓' : '✗'),
    el('span', { class: 'visually-hidden' }, d.ok ? 'Done: ' : 'Failed: '),
    el('span', {}, d.text))));
}

function seedCard(c) {
  return el('div', { class: 'card cmp-seed' },
    el('div', {},
      el('h3', { class: 'cmp-seed-title' }, c.case_title),
      el('p', { class: 'cmp-task' }, c.task),
      el('ul', { class: 'cmp-chips', 'aria-label': 'What the task checks' }, c.chips.map((t) => el('li', {}, t)))),
    el('div', {},
      el('h4', { class: 'cmp-subhead' }, "The solver's failed run"),
      didList(c.did)));
}

/** Re-clicking a link to the episode already in the hash fires no hashchange; fire it by hand. */
function replayLink(e) {
  if (e.currentTarget.hash === location.hash) {
    e.preventDefault();
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  }
}

function column(c, key) {
  const role = c.roles[key];
  const stages = [...role.stages];
  const n = stages.length;
  const longest = Math.max(...ROLES.map((r) => c.roles[r].stages.length));
  const counts = countStages(stages);
  const pct = (v) => Math.round((100 * v) / n);

  // Stage strip, as wide as its share of the longest episode in this case.
  const cells = stages.map((s) => el('span', { class: `cmp-cell s-${stageKey(s)}` }));
  const summary = ORDER.map((s) => `${counts[s]} ${STAGES[s].name.toLowerCase()}`).join(', ');
  const strip = el('div', {
    class: 'cmp-strip', role: 'img', style: { width: `${Math.max((100 * n) / longest, MIN_STRIP)}%` },
    'aria-label': `${n} turns: ${summary}. Marked turns ${role.marks.join(', ')}.`,
  }, cells);

  // Numbered markers above the strip, with leader lines to the exact turn.
  const markers = role.marks.map((m, j) => el('button', {
    type: 'button', class: 'cmp-marker', 'data-mark': j, title: `Turn ${m}`, 'aria-label': `Moment ${j + 1}, turn ${m}`,
  }, j + 1));
  const leaders = svg('svg', { class: 'cmp-leaders', 'aria-hidden': 'true', height: MARKS_H });
  const track = el('div', { class: 'cmp-track' }, el('div', { class: 'cmp-marks' }, leaders, markers), strip);

  const cards = role.cards.map((card, j) => el('li', { class: 'cmp-step', tabindex: '0', 'data-mark': j },
    el('div', { class: 'cmp-step-head' },
      el('span', { class: 'cmp-num', 'aria-hidden': 'true' }, j + 1),
      el('h4', { class: 'cmp-step-title' }, card.title),
      role.marks[j] ? el('span', { class: 'cmp-turn' }, `turn ${role.marks[j]}`) : null),
    el('div', { class: 'cmp-lines' }, card.lines.map((line) =>
      el('div', { class: `cmp-line ${LINE_CLASS[line[0]] || ''}`.trim() }, line)))));

  let active = null;
  let paths = []; // leader lines, rebuilt on each layout
  const setActive = (j) => {
    active = j;
    for (const node of [...markers, ...cards]) node.classList.toggle('is-active', +node.dataset.mark === j);
    paths.forEach((p, k) => p.classList.toggle('is-active', k === j));
    role.marks.forEach((m, k) => cells[m - 1]?.classList.toggle('is-active', k === j));
  };
  // Hovering or focusing a card highlights its marker, and the other way round.
  const focused = () => {
    const node = document.activeElement;
    return node && (markers.includes(node) || cards.includes(node)) ? +node.dataset.mark : null;
  };
  for (const node of [...markers, ...cards]) {
    const j = +node.dataset.mark;
    node.addEventListener('pointerenter', () => setActive(j));
    node.addEventListener('pointerleave', () => setActive(focused()));
    node.addEventListener('focus', () => setActive(j));
    node.addEventListener('blur', () => setActive(null));
  }
  markers.forEach((b, j) => b.addEventListener('click', () => cards[j]?.focus()));

  layouts.set(track, () => {
    const width = track.clientWidth;
    const stripW = strip.getBoundingClientRect().width;
    if (!width) return;
    const xs = role.marks.map((m) => ((m - 0.5) / n) * stripW);
    const order = xs.map((_, j) => j).sort((a, b) => xs[a] - xs[b]);
    const gap = 2 * HEAD_R + 3;
    const hx = [];
    // Push heads apart left to right, then pull them back inside the track from the right.
    order.forEach((j, k) => { hx[j] = Math.max(xs[j], HEAD_R, k ? hx[order[k - 1]] + gap : 0); });
    for (let k = order.length - 1; k >= 0; k--) {
      const j = order[k];
      hx[j] = Math.min(hx[j], k === order.length - 1 ? width - HEAD_R : hx[order[k + 1]] - gap);
    }
    markers.forEach((b, j) => { b.style.left = `${hx[j]}px`; });
    leaders.setAttribute('width', width);
    paths = xs.map((x, j) => svg('path', {
      class: `cmp-leader${j === active ? ' is-active' : ''}`, d: `M${hx[j]} ${2 * HEAD_R} L${x} ${MARKS_H}`,
    }));
    leaders.replaceChildren(...paths);
  });
  resizer.observe(track);

  return el('article', { class: `card cmp-col is-${key}` },
    el('h3', { class: 'cmp-title' },
      el('span', { class: 'cmp-dot', 'aria-hidden': 'true' }),
      role.title,
      role.subtitle ? el('span', { class: 'cmp-sub' }, role.subtitle) : null),
    el('p', { class: 'cmp-stance' }, role.stance),
    track,
    el('div', { class: 'cmp-meta' },
      el('span', {}, `${n} turns`),
      el('span', { class: 'cmp-share' },
        el('span', { class: 'visually-hidden' }, 'Share of turns by stage: '),
        ORDER.map((s) => el('span', { class: 'cmp-share-item' }, chip(s), `${pct(counts[s])}%`)))),
    el('ol', { class: 'cmp-steps' }, cards),
    el('div', { class: 'cmp-foot' },
      el('p', { class: 'cmp-ships' }, 'Ships: ', el('b', {}, role.ships)),
      role.foot ? el('span', { class: 'cmp-tag' }, role.foot) : null,
      role.episode_id
        ? el('a', { class: 'cmp-replay', href: `#replay=${encodeURIComponent(role.episode_id)}`, onclick: replayLink }, 'Replay this episode')
        : el('p', { class: 'cmp-muted' }, 'Transcript not released; summary only.')));
}

export async function init(root) {
  const cases = await loadJSON('compare/cases.json');
  const panel = el('div', { class: 'cmp-panel', role: 'tabpanel', id: 'cmp-panel' });
  const tabs = cases.map((c) => el('button', {
    type: 'button', role: 'tab', class: 'cmp-tab', id: `cmp-tab-${c.key}`, 'aria-controls': 'cmp-panel', onclick: () => select(c),
  }, el('span', { class: 'cmp-tab-title' }, c.case_title), el('span', { class: 'cmp-tab-label' }, c.label)));
  const tablist = el('div', { class: 'cmp-tabs', role: 'tablist', 'aria-label': 'Case' }, tabs);

  // Arrow keys move between tabs.
  tablist.addEventListener('keydown', (e) => {
    const k = tabs.indexOf(document.activeElement);
    const to = { ArrowRight: k + 1, ArrowLeft: k - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (k < 0 || to == null) return;
    e.preventDefault();
    const j = (to + tabs.length) % tabs.length;
    tabs[j].focus();
    select(cases[j]);
  });

  function select(c) {
    tabs.forEach((t, k) => {
      const on = cases[k] === c;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
    });
    panel.setAttribute('aria-labelledby', `cmp-tab-${c.key}`);
    resizer.disconnect();
    panel.replaceChildren(seedCard(c), el('div', { class: 'cmp-cols' }, ROLES.map((r) => column(c, r))));
  }

  root.replaceChildren(tablist, panel);
  select(cases.find((c) => c.key === DEFAULT_CASE) || cases[0]);
}
