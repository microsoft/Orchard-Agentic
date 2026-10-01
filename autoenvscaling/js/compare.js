// One failure, three proposers: the seed failure for a case, then how GPT-5.6-sol, Claude Opus 5,
// and the Cold-Start Qwen3.6-35B-A3B spent their turns, three marked moments, and what each shipped.
import { loadJSON, el, svg, STAGES } from './util.js';

const ROLES = ['gpt', 'opus', 'qwen'];
const ROWS = ['R', 'B', 'W', 'T'];
const DEFAULT_CASE = 'pytorch';
const LINE_CLASS = { $: 'is-cmd', '✓': 'is-ok', '×': 'is-bad', '✗': 'is-bad', '~': 'is-path', '+': 'is-add' };

// Step-line plot, after build_cases.py (paper Figure 8), in px so text keeps its size.
const PLOT = { labelW: 48, padR: 10, padT: 1, rowH: 26, axisH: 24, r: 9 };
const AXIS_LINE = '#B7B0C4';

// The plot is drawn to the column's pixel width, so each one redraws itself on resize.
const layouts = new WeakMap();
const resizer = new ResizeObserver((entries) => entries.forEach((e) => layouts.get(e.target)?.()));

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

/**
 * The episode as a step line through Read, Build, Write, Test over its turns, on shading that shows
 * where all of this proposer's study episodes spend their turns, with the three moments circled.
 * Returns the svg, a draw(width) function, and the circles for highlighting.
 */
function stepPlot(c, key, role) {
  const { labelW, padR, padT, rowH, axisH, r } = PLOT;
  const colors = role.colors;
  const seq = [...role.stages].map((s) => Math.max(0, ROWS.indexOf(s)));
  const n = seq.length;
  const grid = role.density?.grid || [];
  const height = padT + 4 * rowH + axisH;
  const counts = ROWS.map((s, k) => `${seq.filter((v) => v === k).length} ${STAGES[s].name.toLowerCase()}`).join(', ');
  const plot = svg('svg', {
    class: 'cmp-plot', role: 'img', width: '100%', height,
    'aria-label': `${role.title}: ${n} turns (${counts}), as a path through read, build, write, and test. `
      + `Circles 1 to 3 mark turns ${role.marks.join(', ')}. `
      + (role.density ? `Background shading shows where this proposer's ${role.density.episodes} study episodes spend their turns.` : ''),
  });
  const points = role.marks.map((t, j) => svg('g', { class: 'cmp-pt', 'data-mark': j },
    svg('circle', { r, fill: '#fff', stroke: colors.line, 'stroke-width': 2 }),
    svg('text', { 'text-anchor': 'middle', dy: '0.35em', fill: colors.line, 'font-size': 11, 'font-weight': 700 }, j + 1)));

  function draw(width) {
    const px = labelW;
    const pw = width - labelW - padR;
    const py = padT;
    const X = (i) => px + (pw * (i + 0.5)) / n;
    const Y = (row) => py + rowH * row + rowH / 2;
    const id = `cmp-${c.key}-${key}`;
    const out = [svg('defs', {},
      svg('clipPath', { id: `${id}-clip` }, svg('rect', { x: px, y: py, width: pw, height: rowH * 4, rx: 3 })),
      svg('marker', { id: `${id}-arrow`, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: 'auto' },
        svg('path', { d: 'M0 0L10 5 0 10Z', fill: colors.main })))];

    // White plot area, density shading, white lines between rows, stage labels.
    out.push(svg('rect', { x: px, y: py, width: pw, height: rowH * 4, rx: 3, fill: '#fff' }));
    const shade = svg('g', { 'clip-path': `url(#${id}-clip)` });
    grid.forEach((row, ri) => row.forEach((v, b) => {
      if (!v) return;
      const nb = row.length;
      shade.append(svg('rect', {
        x: (px + (pw * b) / nb).toFixed(1), y: py + rowH * ri, width: (pw / nb + 0.4).toFixed(1), height: rowH,
        fill: colors.main, opacity: (0.04 + (0.5 * v) / 100).toFixed(3),
      }));
    }));
    out.push(shade);
    ROWS.forEach((s, ri) => {
      out.push(svg('text', {
        x: px - 8, y: Y(ri), dy: '0.35em', 'text-anchor': 'end', fill: colors.text, 'font-size': 13, 'font-weight': 700,
      }, STAGES[s].name));
      if (ri) out.push(svg('path', { d: `M${px} ${py + rowH * ri}H${px + pw}`, stroke: '#fff', 'stroke-width': 1.5 }));
    });
    out.push(svg('rect', { class: 'cmp-plot-frame', x: px, y: py, width: pw, height: rowH * 4, rx: 3 }));

    // The episode: one point per turn, stepping vertically between consecutive turns.
    const pts = [];
    seq.forEach((row, i) => {
      if (i) pts.push(`${X(i).toFixed(1)},${Y(seq[i - 1]).toFixed(1)}`);
      pts.push(`${X(i).toFixed(1)},${Y(row).toFixed(1)}`);
    });
    out.push(svg('polyline', {
      points: pts.join(' '), fill: 'none', stroke: colors.line, 'stroke-width': n < 80 ? 3 : 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));

    // Numbered moments on the line: turn t is index t - 1, on that turn's row.
    role.marks.forEach((t, j) => points[j].setAttribute('transform', `translate(${X(t - 1).toFixed(1)} ${Y(seq[t - 1] ?? 0).toFixed(1)})`));
    out.push(...points);

    // Axis: 0, "turns" with an arrow through it, and n.
    const ty = py + rowH * 4 + 16;
    const mid = px + pw / 2;
    out.push(
      svg('text', { class: 'cmp-axis', x: px, y: ty }, '0'),
      svg('text', { class: 'cmp-axis', x: mid, y: ty, 'text-anchor': 'middle' }, 'turns'),
      svg('text', { class: 'cmp-axis cmp-axis-n', x: px + pw, y: ty, 'text-anchor': 'end' }, n),
      svg('path', {
        d: `M${px + 14} ${ty - 4}H${mid - 22}M${mid + 22} ${ty - 4}H${px + pw - 12 - 8 * String(n).length}`,
        fill: 'none', stroke: AXIS_LINE, 'stroke-width': 1.5, 'stroke-linecap': 'round', 'marker-end': `url(#${id}-arrow)`,
      }));
    plot.replaceChildren(...out);
  }
  return { plot, draw, points };
}

function column(c, key) {
  const role = c.roles[key];
  const { plot, draw, points } = stepPlot(c, key, role);
  const wrap = el('div', { class: 'cmp-plotwrap' }, plot);

  const cards = role.cards.map((card, j) => el('li', { class: 'cmp-step', tabindex: '0', 'data-mark': j },
    el('div', { class: 'cmp-step-head' },
      el('span', { class: 'cmp-num', 'aria-hidden': 'true' }, j + 1),
      el('h4', { class: 'cmp-step-title' }, card.title),
      role.marks[j] ? el('span', { class: 'cmp-turn' }, `turn ${role.marks[j]}`) : null),
    el('div', { class: 'cmp-lines' }, card.lines.map((line) =>
      el('div', { class: `cmp-line ${LINE_CLASS[line[0]] || ''}`.trim() }, line)))));

  // Highlight moment j: its card, and its circle filled with the line colour.
  const setActive = (j) => {
    cards.forEach((node, k) => node.classList.toggle('is-active', k === j));
    points.forEach((g, k) => {
      const [circle, text] = [g.firstChild, g.lastChild];
      circle.setAttribute('fill', k === j ? role.colors.line : '#fff');
      text.setAttribute('fill', k === j ? '#fff' : role.colors.line);
    });
  };
  const focused = () => {
    const k = cards.indexOf(document.activeElement);
    return k < 0 ? null : k;
  };
  // Hovering or focusing a card highlights its circle; hovering a circle highlights its card,
  // and clicking it focuses the card.
  cards.forEach((node, j) => {
    node.addEventListener('pointerenter', () => setActive(j));
    node.addEventListener('pointerleave', () => setActive(focused()));
    node.addEventListener('focus', () => setActive(j));
    node.addEventListener('blur', () => setActive(null));
  });
  points.forEach((g, j) => {
    g.addEventListener('pointerenter', () => setActive(j));
    g.addEventListener('pointerleave', () => setActive(focused()));
    g.addEventListener('click', () => cards[j]?.focus());
  });

  layouts.set(wrap, () => { if (wrap.clientWidth) draw(wrap.clientWidth); });
  resizer.observe(wrap);

  const col = el('article', { class: `card cmp-col is-${key}` },
    el('h3', { class: 'cmp-title' },
      el('span', { class: 'cmp-dot', 'aria-hidden': 'true' }),
      role.title,
      role.subtitle ? el('span', { class: 'cmp-sub' }, role.subtitle) : null),
    el('p', { class: 'cmp-stance' }, role.stance),
    wrap,
    el('ol', { class: 'cmp-steps' }, cards),
    el('div', { class: 'cmp-foot' },
      el('p', { class: 'cmp-ships' }, 'Ships: ', el('b', {}, role.ships)),
      role.foot ? el('span', { class: 'cmp-tag' }, role.foot) : null,
      role.episode_id
        ? el('a', { class: 'cmp-replay', href: `#replay=${encodeURIComponent(role.episode_id)}`, onclick: replayLink }, 'Replay this episode')
        : el('p', { class: 'cmp-muted' }, 'Transcript not released; summary only.')));
  // Card numbers match the circles on the plot.
  col.style.setProperty('--author-line', role.colors.line);
  return col;
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
