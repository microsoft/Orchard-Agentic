// Results section: hand-rolled SVG charts in the style of the paper's matplotlib figures
// (AutoEnvScaling/analysis/plot_*.py: same colours, #231A33 edges, #DED8EC grid, #C3B9D6
// spines, #3A3145 ticks), kept sparse: exact numbers and intervals live in the tooltips.
//   (a) #chart-valid      valid-environment rate, 8 proposer models x 3 methods (Figure 4)
//   (e) #chart-artifacts  what working in a terminal changes about environments (Figure 5)
//   (b) #chart-solver     Terminal-Bench 2.1 after solver RL (Table 3)
//   (c) #chart-selfplay   Qwen3.6-35B-A3B self-play curves (Figure 6)
//   (f) #chart-training   useful-group rate and rollout-collection time (Figure 7)
//   (d) #table-hil        HiL-Bench training table
// Each chart's one-sentence summary is its svg aria-label and a visually hidden paragraph.
// The pure data helpers are exported so the numbers can be checked in node.

import { loadJSON, el, svg, fmtInt, showError } from './util.js';

/* ------------------------------------------------------------------ */
/* Pure helpers                                                         */
/* ------------------------------------------------------------------ */

export const METHODS = [
  { key: 'prompt', label: 'Prompt', color: 'var(--prompt)' },
  { key: 'feedback', label: 'Prompt + Env Feedback', color: 'var(--feedback)' },
  { key: 'harness', label: 'AutoEnvScaling', color: 'var(--purple)' },
];
const ARM_COLOR = Object.fromEntries(METHODS.map((m) => [m.key, m.color]));

export const pct = (x, d = 1) => `${(x * 100).toFixed(d)}%`;

/** Signed one-decimal number with a real minus sign. */
export const signed = (d) => `${d < 0 ? '−' : '+'}${Math.abs(d).toFixed(1)}`;

/** "a", "a and b", "a, b, and c". */
export const listJoin = (xs) => (xs.length < 3 ? xs.join(' and ')
  : `${xs.slice(0, -1).join(', ')}, and ${xs[xs.length - 1]}`);

/** AutoEnvScaling rate divided by Prompt rate for one model. */
export const harnessRatio = (m) => m.arms.harness.rate / m.arms.prompt.rate;

/** "25×" from 10 up, one decimal below ("4.8×"). */
export const fmtRatio = (r) => `${r >= 10 ? Math.round(r) : r.toFixed(1)}×`;

/** The model with the largest AutoEnvScaling / Prompt ratio. */
export const largestGain = (models) => models.reduce((a, b) => (harnessRatio(b) > harnessRatio(a) ? b : a));

/** Mean rate per method across models. */
export function methodMeans(models) {
  const mean = (k) => models.reduce((s, m) => s + m.arms[k].rate, 0) / models.length;
  return Object.fromEntries(METHODS.map((m) => [m.key, mean(m.key)]));
}

export function validSummary(models) {
  const means = methodMeans(models);
  const top = largestGain(models);
  return `Averaged over the ${models.length} proposer models, the valid-environment rate is `
    + `${pct(means.prompt)} with Prompt, ${pct(means.feedback)} with Prompt + Env Feedback, and `
    + `${pct(means.harness)} with AutoEnvScaling; the largest gain is ${top.label}, from `
    + `${pct(top.arms.prompt.rate)} to ${pct(top.arms.harness.rate)} (${fmtRatio(harnessRatio(top))}).`;
}

/** Badge on the AutoEnvScaling mark, as badge() in plot_harness_artifacts.py: the ratio of arm
 *  means against Prompt + Env Feedback, or of their complements when lower is the claim. */
export function artifactBadge(panel) {
  const spec = panel.badge;
  let h = panel.mean.harness;
  let b = panel.mean.feedback;
  if (spec.kind === 'complement_ratio') { h = spec.total - h; b = spec.total - b; }
  return `${(h / b).toFixed(1)}${spec.suffix}`;
}

export function artifactSummary(data) {
  const name = (k) => (data.arms.find(([a]) => a === k) || [k, k])[1];
  const parts = data.panels.map((p) => {
    const unit = /\(%\)/.test(p.title) ? '%' : '';
    const t = p.title.replace(/\s*\(%\)/, '');
    return `${t.charAt(0).toLowerCase()}${t.slice(1)} ${p.mean.harness.toFixed(1)}${unit} vs `
      + `${p.mean.feedback.toFixed(1)}${unit} (${artifactBadge(p)})`;
  });
  return `Compared with ${name('feedback')}, environments from AutoEnvScaling have ${listJoin(parts)}.`;
}

export const SOLVER_RUNS = [
  { key: 'base', label: 'Base', color: 'var(--ch-base)' },
  { key: 'tmax', label: 'Tmax', color: 'var(--tmax)' },
  { key: 'autoenvscaling', label: 'AutoEnvScaling', color: 'var(--purple)' },
];

/** Bars for proposer index p: [{name, bars: [{run, v, ci}], gain}]. `base` is shared by proposers. */
export function solverGroups(data, p) {
  return data.solvers.map((s) => {
    const bars = SOLVER_RUNS.map((run) => {
      const cell = run.key === 'base' ? s.base : s[run.key]?.[p];
      return cell && cell[0] != null ? { run, v: cell[0], ci: cell[1] ?? 0 } : null;
    }).filter(Boolean);
    const at = (k) => bars.find((b) => b.run.key === k)?.v;
    return { name: s.name, bars, gain: at('autoenvscaling') - at('tmax') };
  });
}

export function solverSummary(data, p) {
  const groups = solverGroups(data, p);
  const v = (g, k) => g.bars.find((b) => b.run.key === k)?.v;
  const parts = groups.map((g) => `${signed(g.gain)} on ${g.name} (${v(g, 'autoenvscaling').toFixed(1)} vs ${v(g, 'tmax').toFixed(1)})`);
  const where = groups.length === 2 ? 'both solvers' : `all ${groups.length} solvers`;
  return groups.every((g) => g.gain > 0)
    ? `With ${data.proposers[p]} as the proposer, AutoEnvScaling beats Tmax on ${where}: ${listJoin(parts)}.`
    : `With ${data.proposers[p]} as the proposer, AutoEnvScaling minus Tmax is ${listJoin(parts)}.`;
}

/** Benchmarks with the axis limits of plot_rsi.py (every other paper tick, to keep it sparse). */
export const BENCHES = [
  { key: 'TB2.1', label: 'Terminal-Bench 2.1', lim: [38.8, 54.2], ticks: [40, 44, 48, 52] },
  { key: 'TB4', label: 'Terminal-Bench 4.0', lim: [0.8, 3.45], ticks: [0.8, 1.6, 2.4, 3.2] },
];

/** Colours and markers of plot_rsi.py (line widths scaled to the page). */
export const SELFPLAY_RUNS = [
  { name: 'AutoEnvScaling', color: 'var(--purple)', marker: 's', width: 2.6 },
  { name: 'Tmax', color: 'var(--tmax)', marker: '^', width: 1.9 },
  { name: 'AutoEnvScaling (w/o Proposer Training)', color: 'var(--ch-wo)', marker: 'o', width: 1.9 },
];

/** Each run as [step, score] points. As in plot_rsi.py, every run starts at step 0 from the
 *  shared Cold-Start checkpoint, so that point is prepended to each run. */
export function selfplaySeries(data, metric) {
  const start = data.cold_start[metric];
  return data.runs.map((run) => ({
    name: run.name,
    points: [[0, start], ...run.steps.map((s, i) => [s, run[metric][i]])],
  }));
}

export function selfplaySummary(data, metric, bench) {
  const start = data.cold_start[metric];
  const series = selfplaySeries(data, metric);
  const end = (s) => s.points[s.points.length - 1];
  const lastStep = Math.max(...series.map((s) => end(s)[0]));
  const parts = [];
  let prevStep = null;
  for (const name of ['AutoEnvScaling', 'Tmax']) {
    const s = series.find((x) => x.name === name);
    if (!s) continue;
    const [step, v] = end(s);
    parts.push(`${name} reaches ${v.toFixed(1)}${step === prevStep ? '' : ` at step ${step}`} (${signed(v - start)})`);
    prevStep = step;
  }
  let text = `On ${bench}, from the Cold-Start checkpoint at ${start.toFixed(1)}, ${listJoin(parts)}`;
  for (const s of series.filter((x) => end(x)[0] < lastStep)) {
    const who = s.name.includes('w/o') ? 'the run without proposer training' : s.name;
    text += `; ${who} collapsed at step ${end(s)[0]} (${end(s)[1].toFixed(1)})`;
  }
  return `${text}. The base model scores ${data.base[metric].toFixed(1)}.`;
}

/** Trailing mean over `window` values, as trailing_mean() in plot_training_metrics.py. */
export function trailingMean(values, window) {
  const out = [];
  for (let i = window - 1; i < values.length; i++) {
    let s = 0;
    for (let j = i - window + 1; j <= i; j++) s += values[j];
    out.push(s / window);
  }
  return out;
}

/** The two panels of plot_training_metrics.py, with its titles, labels and y limits. */
export const TRAIN_PANELS = [
  { field: 'useful', scale: 100, title: 'Useful rollout rate', yTitle: 'Useful groups (%)', max: 52, ticks: [0, 25, 50], dec: 1 },
  { field: 'minutes', scale: 1, title: 'Rollout collection time', yTitle: 'Minutes per training step', max: 420, ticks: [0, 200, 400], dec: 0 },
];
export const TRAIN_RUNS = [
  { key: 'tmax', label: 'Tmax', color: 'var(--tmax)' },
  { key: 'AutoEnvScaling', label: 'AutoEnvScaling', color: 'var(--purple)' },
];

/** One run on one panel: per-step values, trailing means (null until the window fills), flags. */
export function trainingSeries(data, runKey, panel) {
  const run = data.runs[runKey];
  const values = run[panel.field].map((v) => v * panel.scale);
  const window = data.smoothing?.[panel.field] ?? 1;
  const mean = trailingMean(values, window);
  return {
    steps: run.step,
    values,
    mean: values.map((_, i) => (i >= window - 1 ? mean[i - window + 1] : null)),
    extrapolated: run.extrapolated || values.map(() => false),
    window,
  };
}

export function trainingSummary(data) {
  return `Over matched steps ${data.matched_steps}, AutoEnvScaling keeps a ${data.useful_rate_ratio.toFixed(1)}× `
    + `higher share of rollout groups useful than Tmax and spends ${Math.round(data.collection_time_reduction * 100)}% `
    + 'less time collecting rollouts per step.';
}

/** Wrap a name into at most 3 lines at spaces and hyphens. Prefers fewer lines, then no break
 *  right before a digit ("GPT-" / "5.6-sol"), then the narrowest widest line. */
export function wrapLabel(text, maxW, measure) {
  if (measure(text) <= maxW) return [text];
  const tokens = text.match(/[^\s-]+(?:-|\s+)?|[\s-]+/g) || [text];
  const n = tokens.length;
  const cuts = [];
  for (let i = 1; i < n; i++) {
    cuts.push([i]);
    for (let j = i + 1; j < n; j++) cuts.push([i, j]);
  }
  let best = null;
  for (const c of cuts) {
    const bounds = [0, ...c, n];
    const lines = c.concat(n).map((e, k) => tokens.slice(bounds[k], e).join('').trim());
    const widest = Math.max(...lines.map(measure));
    const penalty = c.filter((i) => /^\d/.test(tokens[i])).length;
    const score = widest <= maxW ? [0, lines.length, penalty, widest] : [1, widest, lines.length, penalty];
    if (!best || lexLess(score, best.score)) best = { lines, score };
  }
  return best ? best.lines : [text];
}

function lexLess(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

/* ------------------------------------------------------------------ */
/* DOM helpers                                                          */
/* ------------------------------------------------------------------ */

const r2 = (n) => Math.round(n * 100) / 100;

let ctx2d;
let fontFamily = 'sans-serif';
/** Rendered text width in px, measured with a canvas in the page font. */
function textWidth(text, size = 12, weight = 400) {
  if (ctx2d === undefined) {
    ctx2d = document.createElement('canvas').getContext('2d');
    fontFamily = getComputedStyle(document.body).fontFamily || fontFamily;
  }
  if (!ctx2d) return text.length * size * 0.6;
  ctx2d.font = `${weight} ${size}px ${fontFamily}`;
  return ctx2d.measureText(text).width;
}

let uid = 0;

/** A labelled row of mutually exclusive buttons (shared .seg style). */
function segControl(name, options, value, onChange) {
  const id = `ch-seg-${++uid}`;
  const buttons = options.map(([v, text]) => el('button', {
    type: 'button',
    'aria-pressed': String(v === value),
    onclick: () => {
      buttons.forEach((b, i) => b.setAttribute('aria-pressed', String(options[i][0] === v)));
      onChange(v);
    },
  }, text));
  return el('div', { class: 'ch-field' },
    el('span', { id, text: name }),
    el('div', { class: 'seg', role: 'group', 'aria-labelledby': id }, buttons));
}

const legendItem = (key, text) => el('span', { class: 'ch-item' }, key, text);
const legend = (...items) => el('div', { class: 'ch-legend' }, items);

/** Legend patch for bars (fill with the paper's dark edge). */
const keyBar = (color) => el('span', { class: 'ch-key', style: { background: color } });

/** Legend key for lines: a short stroke, solid or dotted. */
function keyLine(color, kind = 'solid') {
  return svg('svg', { class: 'ch-glyph', width: 22, height: 12, viewBox: '0 0 22 12', 'aria-hidden': 'true' },
    svg('line', { class: `ch-glyph-line is-${kind}`, x1: 1, y1: 6, x2: 21, y2: 6, style: `stroke:${color}` }));
}

/** Paper-style marker: 'o' circle, 's' square, '^' triangle. */
function markerEl(shape, x, y, size, color, cls = 'ch-mark') {
  const h = size / 2;
  const attrs = { class: cls, style: `fill:${color}` };
  if (shape === 's') return svg('rect', { ...attrs, x: r2(x - h * 0.9), y: r2(y - h * 0.9), width: r2(size * 0.9), height: r2(size * 0.9) });
  if (shape === '^') {
    const t = h * 1.2;
    return svg('path', { ...attrs, d: `M${r2(x)},${r2(y - t)}L${r2(x + t * 0.95)},${r2(y + t * 0.6)}L${r2(x - t * 0.95)},${r2(y + t * 0.6)}Z` });
  }
  return svg('circle', { ...attrs, cx: r2(x), cy: r2(y), r: r2(h) });
}

/** Chart wrapper: optional controls, the plot, a tooltip, an optional one-line note, and the
 *  summary (visually hidden) plus a live region for keyboard readouts. */
function frame(container, { controls, note } = {}) {
  const ui = {
    plot: el('div', { class: 'ch-plot' }),
    tip: el('div', { class: 'ch-tip', hidden: true, 'aria-hidden': 'true' }),
    summary: el('p', { class: 'visually-hidden' }),
    live: el('div', { class: 'visually-hidden', 'aria-live': 'polite' }),
    onHide: null,
  };
  ui.wrap = el('div', { class: 'ch' },
    controls && el('div', { class: 'ch-controls' }, controls), ui.plot, ui.tip,
    note && el('p', { class: 'ch-note', text: note }), ui.summary, ui.live);
  container.replaceChildren(ui.wrap);
  // A tap outside any hover target closes a tooltip opened by touch.
  document.addEventListener('pointerdown', (e) => {
    if (!ui.tip.hidden && !(ui.wrap.contains(e.target) && e.target.closest('[data-tip]'))) hideTip(ui);
  });
  return ui;
}

function chartSvg(W, H, summary, focusable = false) {
  return svg('svg', {
    class: 'ch-svg', viewBox: `0 0 ${W} ${H}`, width: '100%', height: H,
    role: 'img', 'aria-label': summary, tabindex: focusable ? 0 : null,
  });
}

/** Re-render on width changes of `node` (debounced). Calls fn once right away. */
function watchWidth(node, fn) {
  let last = node.clientWidth;
  let timer;
  if (last) fn(last);
  if (typeof ResizeObserver === 'undefined') return;
  new ResizeObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const w = node.clientWidth;
      if (w && w !== last) { last = w; fn(w); }
    }, 100);
  }).observe(node);
}

const line = (x1, y1, x2, y2, cls) => svg('line', { class: cls, x1: r2(x1), y1: r2(y1), x2: r2(x2), y2: r2(y2) });
const text = (x, y, str, cls, anchor = 'start') => svg('text', { class: cls, x: r2(x), y: r2(y), 'text-anchor': anchor, text: str });
const rect = (x, y, w, h, cls, color) => svg('rect', {
  class: cls, x: r2(x), y: r2(y), width: r2(Math.max(0, w)), height: r2(Math.max(0, h)), style: `fill:${color}`,
});

/** Axes like the paper's: y gridlines, left and bottom spines with 3px ticks, tick labels, and
 *  axis titles (the y title rotated at x = yTitleX). Pass `grid: 'x'` for vertical gridlines. */
function drawAxes(root, b, o) {
  const g = svg('g', { class: 'ch-axes' });
  const { X, Y, xTicks = [], yTicks = [], grid = 'y' } = o;
  const xFmt = o.xFmt || String;
  const yFmt = o.yFmt || String;
  if (grid === 'y') yTicks.forEach((t) => g.append(line(b.x0, Y(t), b.x1, Y(t), 'ch-grid')));
  if (grid === 'x') xTicks.forEach((t) => g.append(line(X(t), b.y0, X(t), b.y1, 'ch-grid')));
  g.append(line(b.x0, b.y0, b.x0, b.y1, 'ch-spine'), line(b.x0, b.y1, b.x1, b.y1, 'ch-spine'));
  for (const t of yTicks) g.append(line(b.x0 - 3, Y(t), b.x0, Y(t), 'ch-spine'), text(b.x0 - 6, Y(t) + 4, yFmt(t), 'ch-tick', 'end'));
  for (const t of xTicks) g.append(line(X(t), b.y1, X(t), b.y1 + 3, 'ch-spine'), text(X(t), b.y1 + 16, xFmt(t), 'ch-tick', 'middle'));
  if (o.xTitle) g.append(text((b.x0 + b.x1) / 2, b.y1 + 33, o.xTitle, 'ch-axis-title', 'middle'));
  if (o.yTitle) {
    g.append(svg('text', {
      class: 'ch-axis-title', 'text-anchor': 'middle', text: o.yTitle,
      transform: `translate(${r2(o.yTitleX ?? 12)},${r2((b.y0 + b.y1) / 2)}) rotate(-90)`,
    }));
  }
  root.append(g);
}

/* Tooltip ------------------------------------------------------------ */

function tipRow(color, value, name, { kind, sub } = {}) {
  return el('div', { class: 'ch-tip-row' },
    el('span', { class: `ch-tip-key${kind ? ` is-${kind}` : ''}`, style: { borderTopColor: color } }),
    el('b', { text: value }),
    el('span', { class: 'ch-tip-label', text: name }),
    sub && el('span', { class: 'ch-tip-sub', text: sub }));
}

/** A box in svg coordinates, converted to wrapper coordinates. */
function boxOf(ui, root, W, x0, y0, x1, y1) {
  const s = root.getBoundingClientRect();
  const w = ui.wrap.getBoundingClientRect();
  const k = s.width / W || 1;
  return { left: s.left - w.left + x0 * k, right: s.left - w.left + x1 * k, top: s.top - w.top + y0 * k, bottom: s.top - w.top + y1 * k };
}

/** Show the tooltip beside the box ('side') or under it ('below'), always inside the wrapper. */
function showTip(ui, nodes, box, mode) {
  const { tip, wrap } = ui;
  tip.replaceChildren(...nodes);
  tip.hidden = false;
  const W = wrap.clientWidth;
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  const gap = 10;
  let x = (box.left + box.right - tw) / 2;
  let y = box.bottom + gap;
  if (mode === 'side' && box.right + gap + tw <= W) { x = box.right + gap; y = box.top; }
  else if (mode === 'side' && box.left - gap - tw >= 0) { x = box.left - gap - tw; y = box.top; }
  else if (mode === 'side' && box.top - gap - th >= 0) y = box.top - gap - th;
  tip.style.left = `${Math.max(0, Math.min(x, W - tw))}px`;
  tip.style.top = `${Math.max(0, y)}px`;
}

function hideTip(ui) {
  ui.tip.hidden = true;
  ui.wrap.querySelectorAll('.is-active').forEach((n) => n.classList.remove('is-active'));
  if (ui.onHide) ui.onHide();
}

/** Hover, tap, and keyboard focus all show the same tooltip for `node`. */
function hoverable(ui, node, show) {
  node.setAttribute('data-tip', '');
  const open = (announce) => {
    ui.wrap.querySelectorAll('.is-active').forEach((n) => n.classList.remove('is-active'));
    node.classList.add('is-active');
    const said = show();
    if (announce) ui.live.textContent = said;
  };
  node.addEventListener('pointerenter', () => open(false));
  node.addEventListener('pointerdown', () => open(false));
  node.addEventListener('pointerleave', (e) => {
    if (e.pointerType === 'mouse' && document.activeElement !== node) hideTip(ui);
  });
  node.addEventListener('focus', () => open(true));
  node.addEventListener('blur', () => hideTip(ui));
}

/** Crosshair charts: the plot is one focusable svg and arrow keys move between steps
 *  (Shift moves `bigStep` at a time, Home and End jump to the ends). */
function scrubbable(ui, root, steps, show, bigStep = 1) {
  const snap = (s) => (s == null ? steps[steps.length - 1]
    : steps.reduce((a, b) => (Math.abs(b - s) < Math.abs(a - s) ? b : a)));
  let current = null;
  const go = (s, announce) => { current = s; show(s, announce); };
  root.addEventListener('focus', () => {
    // A mouse click also focuses the plot; only keyboard focus opens the readout by itself.
    let keyboard = true;
    try { keyboard = root.matches(':focus-visible'); } catch { /* older browsers */ }
    if (keyboard) go(snap(current), true);
  });
  root.addEventListener('blur', () => hideTip(ui));
  root.addEventListener('keydown', (e) => {
    const i = steps.indexOf(snap(current));
    const jump = e.shiftKey ? bigStep : 1;
    const next = { ArrowRight: i + jump, ArrowLeft: i - jump, Home: 0, End: steps.length - 1 }[e.key];
    if (next == null) { if (e.key === 'Escape') hideTip(ui); return; }
    e.preventDefault();
    go(steps[Math.max(0, Math.min(steps.length - 1, next))], true);
  });
  return go;
}

/** Hit area for a crosshair: pointer and touch pick the nearest step. */
function scrubArea(ui, root, W, b, pick, go) {
  const hit = svg('rect', { class: 'ch-hit', x: r2(b.x0), y: r2(b.y0), width: r2(b.x1 - b.x0), height: r2(b.y1 - b.y0), 'data-tip': '' });
  const at = (e) => {
    const r = root.getBoundingClientRect();
    return pick(((e.clientX - r.left) * W) / (r.width || W));
  };
  hit.addEventListener('pointermove', (e) => go(at(e), false));
  hit.addEventListener('pointerdown', (e) => go(at(e), false));
  hit.addEventListener('pointerleave', (e) => {
    if (e.pointerType === 'mouse' && document.activeElement !== root) hideTip(ui);
  });
  return hit;
}

/* Label placement ---------------------------------------------------- */

/** Does segment p-q cross rectangle r? (Liang-Barsky clipping) */
function segHitsRect([x1, y1], [x2, y2], r) {
  let t0 = 0;
  let t1 = 1;
  const dx = x2 - x1;
  const dy = y2 - y1;
  for (const [p, q] of [[-dx, x1 - r.x0], [dx, r.x1 - x1], [-dy, y1 - r.y0], [dy, r.y1 - y1]]) {
    if (p === 0) { if (q < 0) return false; continue; }
    const t = q / p;
    if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; } else { if (t < t0) return false; if (t < t1) t1 = t; }
  }
  return true;
}

const rectsOverlap = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/** Text box for a label at baseline (x, y) with the given anchor. */
function labelRect(str, x, y, anchor, size, weight) {
  const w = textWidth(str, size, weight);
  const x0 = anchor === 'end' ? x - w : anchor === 'middle' ? x - w / 2 : x;
  return { x0: x0 - 2, x1: x0 + w + 2, y0: y - size + 1, y1: y + 3 };
}

/** Put a label at the first candidate that stays in bounds and clear of lines and other labels;
 *  when every spot is taken, use the one that hits the fewest things (the text has a halo). */
function placeLabel(root, str, cands, obs, bounds, cls, size = 11.5, weight = 400) {
  const hits = (r) => (r.x0 < bounds.x0 || r.x1 > bounds.x1 || r.y0 < bounds.y0 || r.y1 > bounds.y1 ? 100 : 0)
    + obs.rects.filter((o) => rectsOverlap(o, r)).length
    + obs.segs.filter(([p, q]) => segHitsRect(p, q, r)).length;
  let best = null;
  for (const c of cands) {
    const r = labelRect(str, c.x, c.y, c.a, size, weight);
    const n = hits(r);
    if (!best || n < best.n) best = { c, r, n };
    if (n === 0) break;
  }
  obs.rects.push(best.r);
  root.append(svg('text', { class: `${cls} ch-halo`, x: r2(best.c.x), y: r2(best.c.y), 'text-anchor': best.c.a, text: str }));
}

/* ------------------------------------------------------------------ */
/* (a) Valid-environment rate                                           */
/* ------------------------------------------------------------------ */

function renderValid(container, data) {
  const models = data.models;
  const top = largestGain(models);
  const st = { shown: new Set(METHODS.map((m) => m.key)), w: 0 };

  // Legend entries double as show/hide toggles; at least one method stays on screen.
  const toggles = METHODS.map((m) => {
    const b = el('button', { type: 'button', class: 'ch-toggle', 'aria-pressed': 'true' },
      keyBar(m.color), el('span', { class: 'ch-toggle-label', text: m.label }));
    b.addEventListener('click', () => {
      if (st.shown.has(m.key) && st.shown.size === 1) return;
      if (st.shown.has(m.key)) st.shown.delete(m.key); else st.shown.add(m.key);
      b.setAttribute('aria-pressed', String(st.shown.has(m.key)));
      draw();
    });
    return b;
  });
  const ui = frame(container, {
    controls: el('div', { class: 'ch-legend', role: 'group', 'aria-label': 'Show or hide methods' }, toggles),
  });
  const summary = validSummary(models);
  ui.summary.textContent = summary;

  function draw() {
    const W = st.w;
    if (!W) return;
    hideTip(ui);
    const shown = METHODS.filter((m) => st.shown.has(m.key));
    const horizontal = container.clientWidth < 560;
    const out = (horizontal ? validRows : validColumns)(W, models, shown, summary, top);
    ui.plot.replaceChildren(out.root);
    for (const g of out.groups) {
      hoverable(ui, g.node, () => {
        const t = validTip(g.model, shown);
        showTip(ui, t.nodes, boxOf(ui, out.root, W, ...g.box), horizontal ? 'below' : 'side');
        return t.said;
      });
    }
  }
  watchWidth(ui.plot, (w) => { st.w = w; draw(); });
}

const RATE_TICKS = [0, 0.5, 1];
const rateTick = (t) => String(t * 100);

/** "25×" over the largest gain, when both Prompt and AutoEnvScaling are on screen. */
const calloutFor = (d, top, shown) => (d === top && shown.some((s) => s.key === 'prompt')
  && shown.some((s) => s.key === 'harness') ? fmtRatio(harnessRatio(d)) : null);

/** Vertical grouped bars, one group per model, as plot_arms.py. */
function validColumns(W, list, shown, summary, top) {
  const m = { left: 48, right: 2, top: 8 };
  const plotH = W < 760 ? 220 : 260;
  const band = (W - m.left - m.right) / list.length;
  const names = list.map((d) => wrapLabel(d.label, band - 6, (t) => textWidth(t, 12)));
  const base = m.top + plotH;
  const H = base + 12 + Math.max(...names.map((l) => l.length)) * 14 + 4;
  const Y = (v) => base - (v / 1.06) * plotH;
  const root = chartSvg(W, H, summary);
  drawAxes(root, { x0: m.left, x1: W - m.right, y0: m.top, y1: base }, {
    Y, yTicks: RATE_TICKS, yFmt: rateTick, yTitle: 'Valid-environment rate (%)',
  });

  const barW = Math.min(30, band * 0.26);
  const groupW = shown.length * barW;
  const groups = list.map((d, i) => {
    const cx = m.left + band * (i + 0.5);
    const g = svg('g', { class: 'ch-group', tabindex: 0 });
    g.append(svg('rect', { class: 'ch-band', x: r2(cx - band / 2 + 1), y: 0, width: r2(band - 2), height: H, rx: 3 }));
    shown.forEach((meth, j) => {
      const a = d.arms[meth.key];
      const x = cx - groupW / 2 + j * barW;
      g.append(rect(x, Y(a.rate), barW, base - Y(a.rate), `ch-bar is-${meth.key}`, meth.color));
      const call = meth.key === 'harness' && calloutFor(d, top, shown);
      if (call) g.append(text(x + barW / 2, Y(a.rate) - 6, call, 'ch-callout', 'middle'));
    });
    names[i].forEach((ln, li) => g.append(text(cx, base + 17 + li * 14, ln, 'ch-xlabel', 'middle')));
    root.append(g);
    return { node: g, model: d, box: [cx - groupW / 2, m.top, cx + groupW / 2, base] };
  });
  return { root, groups };
}

/** Horizontal bars for narrow screens: one row per model, its name on its own line. */
function validRows(W, list, shown, summary, top) {
  const m = { left: 2, right: 30, top: 2, bottom: 40 };
  const barH = 9;
  const head = 20;
  const rowGap = 12;
  const rowH = head + shown.length * barH + rowGap;
  const bodyH = list.length * rowH;
  const H = m.top + bodyH + m.bottom;
  const X = (v) => m.left + v * (W - m.left - m.right);
  const root = chartSvg(W, H, summary);
  drawAxes(root, { x0: X(0), x1: X(1), y0: m.top, y1: m.top + bodyH - rowGap + 4 }, {
    X, xTicks: RATE_TICKS, xFmt: rateTick, grid: 'x', xTitle: 'Valid-environment rate (%)',
  });

  const groups = list.map((d, i) => {
    const y0 = m.top + i * rowH;
    const g = svg('g', { class: 'ch-group', tabindex: 0 });
    g.append(svg('rect', { class: 'ch-band', x: 0, y: y0 - 1, width: W, height: rowH - rowGap + 5, rx: 3 }));
    g.append(text(m.left + 5, y0 + 13, d.label, 'ch-rowlabel ch-halo'));
    shown.forEach((meth, j) => {
      const a = d.arms[meth.key];
      const y = y0 + head + j * barH;
      g.append(rect(X(0), y, X(a.rate) - X(0), barH, `ch-bar is-${meth.key}`, meth.color));
      const call = meth.key === 'harness' && calloutFor(d, top, shown);
      if (call) g.append(text(X(a.rate) + 5, y + barH - 0.5, call, 'ch-callout'));
    });
    root.append(g);
    return { node: g, model: d, box: [0, y0, W, y0 + rowH - rowGap + 4] };
  });
  return { root, groups };
}

function validTip(d, shown) {
  const nodes = [el('div', { class: 'ch-tip-title', text: d.label })];
  const said = [d.label];
  for (const m of shown) {
    const a = d.arms[m.key];
    const bits = [`${a.accepted} / ${a.scored} accepted`];
    if (a.ci) bits.push(`95% CI ${(a.ci[0] * 100).toFixed(1)} to ${(a.ci[1] * 100).toFixed(1)}`);
    if (a.cost_usd != null) bits.push(`$${a.cost_usd.toFixed(2)} per valid environment`);
    nodes.push(tipRow(m.color, pct(a.rate), m.label, { sub: bits.join(', ') }));
    said.push(`${m.label}: ${pct(a.rate)}, ${bits.join(', ')}`);
  }
  const ratio = `AutoEnvScaling / Prompt: ${harnessRatio(d).toFixed(1)}×`;
  nodes.push(el('div', { class: 'ch-tip-foot', text: ratio }));
  said.push(ratio);
  return { nodes, said: `${said.join('. ')}.` };
}

/* ------------------------------------------------------------------ */
/* (e) Harness artifacts: one small lollipop panel per measure          */
/* ------------------------------------------------------------------ */

function renderArtifacts(container, data) {
  const arms = data.arms.filter(([k]) => ARM_COLOR[k]);
  const st = { w: 0 };
  const ui = frame(container);
  const summary = artifactSummary(data);
  ui.summary.textContent = summary;

  function draw() {
    const W = st.w;
    if (!W) return;
    hideTip(ui);
    const n = data.panels.length;
    const cols = W >= 700 ? n : 1;
    const rowsOf = Math.ceil(n / cols);
    const armLines = arms.map(([, name]) => wrapLabel(name, 92, (t) => textWidth(t, 12)));
    const L = Math.max(...armLines.flat().map((t) => textWidth(t, 12))) + 12;
    const gap = 28;
    const titleH = 24;
    const rowH = 30;
    const plotH = rowH * 3.2; // ylim (2.6, -0.6) in plot_harness_artifacts.py
    const cellH = titleH + plotH + 8;
    const pw = (W - L - 4 - (cols - 1) * gap) / cols;
    const H = rowsOf * cellH + (rowsOf - 1) * 14;
    const root = chartSvg(W, H, summary);

    data.panels.forEach((p, i) => {
      const c = i % cols;
      const b = { x0: L + c * (pw + gap), y0: Math.floor(i / cols) * (cellH + 14) + titleH };
      b.x1 = b.x0 + pw;
      b.y1 = b.y0 + plotH;
      // Stems start at 0: without a visible x axis, a raised floor would exaggerate differences.
      const xmax = Math.max(...arms.map(([k]) => p.mean[k])) * 1.4;
      const X = (v) => b.x0 + (v / xmax) * pw;
      const rowY = (j) => b.y0 + ((j + 0.6) / 3.2) * plotH;

      const g = svg('g', { class: 'ch-group', tabindex: 0 });
      const padL = c === 0 ? L - 2 : 10;
      g.append(svg('rect', { class: 'ch-band', x: r2(b.x0 - padL), y: r2(b.y0 - titleH), width: r2(pw + padL + 4), height: r2(cellH), rx: 3 }));
      g.append(text((b.x0 + b.x1) / 2, b.y0 - 9, p.title, 'ch-panel-title', 'middle'));
      g.append(line(b.x0, b.y0, b.x0, b.y1, 'ch-spine'));
      arms.forEach(([k], j) => {
        const y = rowY(j);
        const v = p.mean[k];
        const ours = k === 'harness';
        if (c === 0) {
          const ls = armLines[j];
          ls.forEach((t, li) => g.append(text(b.x0 - 8, y + 4 + (li - (ls.length - 1) / 2) * 13, t, 'ch-ylabel', 'end')));
        }
        g.append(svg('line', { class: 'ch-stem', x1: r2(X(0)), y1: r2(y), x2: r2(X(v)), y2: r2(y), style: `stroke:${ARM_COLOR[k]}` }));
        g.append(markerEl('o', X(v), y, ours ? 13 : 11, ARM_COLOR[k], ours ? 'ch-mark is-strong' : 'ch-mark is-edge'));
        g.append(text(X(v) + (ours ? 11 : 10), y + 4.5, v.toFixed(1), `ch-value${ours ? ' is-bold' : ''}`));
        if (ours) g.append(text(X(v / 2), y - 8, artifactBadge(p), 'ch-callout ch-halo', 'middle'));
      });
      root.append(g);
      hoverable(ui, g, () => {
        const t = artifactTip(p, arms);
        showTip(ui, t.nodes, boxOf(ui, root, W, b.x0, b.y0, b.x1, b.y1), 'side');
        return t.said;
      });
    });
    ui.plot.replaceChildren(root);
  }
  watchWidth(ui.plot, (w) => { st.w = w; draw(); });
}

function artifactTip(p, arms) {
  const nodes = [el('div', { class: 'ch-tip-title', text: p.title })];
  const said = [p.title];
  for (const [k, name] of arms) {
    nodes.push(tipRow(ARM_COLOR[k], p.mean[k].toFixed(1), name));
    said.push(`${name}: ${p.mean[k].toFixed(1)}`);
  }
  const how = p.badge.kind === 'complement_ratio'
    ? 'failure rate, AutoEnvScaling over Prompt + Env Feedback'
    : 'AutoEnvScaling over Prompt + Env Feedback';
  const foot = `${artifactBadge(p)}: ${how}`;
  nodes.push(el('div', { class: 'ch-tip-foot', text: foot }));
  said.push(foot);
  return { nodes, said: `${said.join('. ')}.` };
}

/* ------------------------------------------------------------------ */
/* (b) Solver RL                                                        */
/* ------------------------------------------------------------------ */

function renderSolver(container, data) {
  const st = { p: 0, w: 0 };
  const ui = frame(container, {
    controls: [
      legend(...SOLVER_RUNS.map((r) => legendItem(keyBar(r.color), r.label))),
      segControl('Proposer', data.proposers.map((p, i) => [i, p]), st.p, (i) => { st.p = i; update(); }),
    ],
  });
  // Shared y scale for both proposers, so switching does not rescale the axis.
  const maxV = Math.max(...data.proposers.flatMap((_, p) => solverGroups(data, p).flatMap((g) => g.bars.map((b) => b.v))));
  const yMax = Math.ceil((maxV + 8) / 10) * 10;

  function update() {
    ui.summary.textContent = solverSummary(data, st.p);
    draw();
  }

  function draw() {
    const W = st.w;
    if (!W) return;
    hideTip(ui);
    const groups = solverGroups(data, st.p);
    const narrow = W < 520;
    const m = { left: 48, right: 2, top: 6, bottom: 28 };
    const plotH = narrow ? 200 : 240;
    const base = m.top + plotH;
    const H = base + m.bottom;
    const Y = (v) => base - (v / yMax) * plotH;
    const root = chartSvg(W, H, ui.summary.textContent);
    const yTicks = [];
    for (let t = 0; t <= yMax; t += 20) yTicks.push(t);
    drawAxes(root, { x0: m.left, x1: W - m.right, y0: m.top, y1: base }, { Y, yTicks, yTitle: 'Terminal-Bench 2.1' });

    const band = (W - m.left - m.right) / groups.length;
    const kMax = Math.max(...groups.map((g) => g.bars.length));
    const gap = 2;
    const barW = Math.min(52, (band * 0.7) / kMax - gap);
    const fs = barW < 30 ? 11 : 12;

    groups.forEach((grp, i) => {
      const cx = m.left + band * (i + 0.5);
      const gw = grp.bars.length * (barW + gap) - gap;
      const g = svg('g', { class: 'ch-group', tabindex: 0 });
      g.append(svg('rect', { class: 'ch-band', x: r2(cx - band / 2 + 2), y: 0, width: r2(band - 4), height: H, rx: 3 }));
      const mids = [];
      const tops = [];
      grp.bars.forEach((b, j) => {
        const x = cx - gw / 2 + j * (barW + gap);
        const mid = x + barW / 2;
        const ours = b.run.key === 'autoenvscaling';
        g.append(rect(x, Y(b.v), barW, base - Y(b.v), 'ch-bar is-thin', b.run.color),
          svg('text', {
            class: `ch-value${ours ? ' is-ours' : ''}`, x: r2(mid), y: r2(Y(b.v) - 5), 'text-anchor': 'middle',
            style: `font-size:${fs}px`, text: b.v.toFixed(1),
          }));
        mids.push(mid);
        tops.push(Y(b.v) - 5 - fs);
      });
      // Bracket from Tmax to AutoEnvScaling with the gain, as plot_teaser.py.
      const a = grp.bars.findIndex((b) => b.run.key === 'tmax');
      const z = grp.bars.findIndex((b) => b.run.key === 'autoenvscaling');
      if (a >= 0 && z > a) {
        const yb = Math.min(...tops.slice(a, z + 1)) - 7;
        g.append(
          svg('path', { class: 'ch-bracket', d: `M${r2(mids[a])},${r2(tops[a] - 2)}V${r2(yb)}H${r2(mids[z])}V${r2(tops[z] - 2)}` }),
          text((mids[a] + mids[z]) / 2, yb - 5, signed(grp.gain), 'ch-callout', 'middle'));
      }
      g.append(text(cx, base + 19, grp.name, 'ch-xlabel', 'middle'));
      root.append(g);
      hoverable(ui, g, () => {
        const t = solverTip(grp, data.proposers[st.p]);
        showTip(ui, t.nodes, boxOf(ui, root, W, cx - gw / 2, m.top, cx + gw / 2, base), 'side');
        return t.said;
      });
    });
    ui.plot.replaceChildren(root);
  }

  update();
  watchWidth(ui.plot, (w) => { st.w = w; draw(); });
}

function solverTip(grp, proposer) {
  const nodes = [
    el('div', { class: 'ch-tip-title', text: grp.name }),
    el('div', { class: 'ch-tip-meta', text: `Proposer ${proposer}, avg@5 with confidence interval` }),
  ];
  const said = [`${grp.name}, proposer ${proposer}`];
  for (const b of grp.bars) {
    const v = `${b.v.toFixed(1)} ± ${b.ci.toFixed(1)}`;
    nodes.push(tipRow(b.run.color, v, b.run.label));
    said.push(`${b.run.label}: ${v}`);
  }
  return { nodes, said: `${said.join('. ')}.` };
}

/* ------------------------------------------------------------------ */
/* (c) Self-play curves                                                 */
/* ------------------------------------------------------------------ */

function renderSelfplay(container, data) {
  const styleOf = new Map(SELFPLAY_RUNS.map((s, i) => [s.name, { ...s, order: i }]));
  const style = (name) => styleOf.get(name) || { name, color: 'var(--muted)', marker: 'o', width: 1.9, order: 9 };
  const runs = data.runs.map((r) => style(r.name)).sort((a, b) => a.order - b.order);
  const st = { bench: BENCHES[0], w: 0 };
  const ui = frame(container, {
    controls: [
      segControl('Benchmark', BENCHES.map((b) => [b.key, b.label]), st.bench.key, (v) => {
        st.bench = BENCHES.find((b) => b.key === v);
        update();
      }),
      legend(...runs.map((s) => legendItem(keyLine(s.color), s.name)),
        legendItem(keyLine('var(--ch-ref)', 'dot'), 'Cold-Start')),
    ],
  });

  function update() {
    ui.summary.textContent = selfplaySummary(data, st.bench.key, st.bench.label);
    draw();
  }

  function draw() {
    const W = st.w;
    if (!W) return;
    hideTip(ui);
    const metric = st.bench.key;
    const narrow = W < 560;
    const m = { left: 48, right: narrow ? 34 : 20, top: 8, bottom: 42 }; // right: room for end labels
    const plotH = narrow ? 220 : 270;
    const b = { x0: m.left, x1: W - m.right, y0: m.top, y1: m.top + plotH };
    const H = b.y1 + m.bottom;
    const series = selfplaySeries(data, metric).map((s) => ({ ...s, style: style(s.name) }))
      .sort((a, c) => a.style.order - c.style.order);
    const coldV = data.cold_start[metric];

    // The paper's y limits, unless the data falls outside them.
    const values = series.flatMap((s) => s.points.map((p) => p[1]));
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    let [y0, y1] = st.bench.lim;
    let yTicks = st.bench.ticks;
    if (!(lo >= y0 && hi <= y1)) {
      const pad = (hi - lo) * 0.08;
      [y0, y1] = [lo - pad, hi + pad];
      yTicks = [lo, (lo + hi) / 2, hi].map((t) => +t.toFixed(1));
    }
    const dec = yTicks.some((t) => !Number.isInteger(t)) ? 1 : 0;
    const maxStep = Math.max(...series.flatMap((s) => s.points.map((p) => p[0])));
    const xMax = Math.ceil(maxStep / 100) * 100;
    const xMin = -xMax * 0.023; // xlim (-7, 300) in plot_rsi.py
    const X = (s) => b.x0 + ((s - xMin) / (xMax - xMin)) * (b.x1 - b.x0);
    const Y = (v) => b.y0 + (1 - (v - y0) / (y1 - y0)) * plotH;
    const root = chartSvg(W, H, ui.summary.textContent, true);
    const xTicks = [];
    for (let t = 0; t <= xMax; t += 100) xTicks.push(t);
    drawAxes(root, b, { X, Y, xTicks, yTicks, yFmt: (t) => t.toFixed(dec), xTitle: 'RL training step', yTitle: 'Score' });

    // Cold-Start reference line (dotted): every run starts here at step 0.
    const yCold = Y(coldV);
    root.append(line(b.x0, yCold, b.x1, yCold, 'ch-ref'));

    // Lines, with a marker on the last point only; remember what labels must avoid.
    const obs = { rects: [], segs: [[[b.x0, yCold], [b.x1, yCold]]] };
    const plotted = series.map((s) => ({ ...s, px: s.points.map(([x, y]) => [X(x), Y(y)]) }));
    for (const s of [...plotted].reverse()) { // AutoEnvScaling is drawn last, on top
      const { color, width, marker } = s.style;
      root.append(svg('path', {
        class: 'ch-line', d: `M${s.px.map(([x, y]) => `${r2(x)},${r2(y)}`).join('L')}`,
        style: `stroke:${color};stroke-width:${width}px`,
      }));
      const [lx, ly] = s.px[s.px.length - 1];
      root.append(markerEl(marker, lx, ly, 8, color));
      obs.rects.push({ x0: lx - 5, x1: lx + 5, y0: ly - 5, y1: ly + 5 });
      for (let i = 1; i < s.px.length; i++) obs.segs.push([s.px[i - 1], s.px[i]]);
    }

    // End-of-line values (plot_teaser.py), nudged apart if they meet.
    const ends = plotted.filter((s) => s.points[s.points.length - 1][0] === maxStep)
      .map((s) => ({ s, x: s.px[s.px.length - 1][0] + 8, y: s.px[s.px.length - 1][1] + 4 }))
      .sort((a, c) => a.y - c.y);
    for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 14);
    for (const e of ends) {
      const v = e.s.points[e.s.points.length - 1][1].toFixed(1);
      const ours = e.s.name === 'AutoEnvScaling';
      root.append(svg('text', { class: `ch-end ch-halo${ours ? ' is-ours' : ''}`, x: r2(e.x), y: r2(e.y), style: `fill:${e.s.style.color}`, text: v }));
      obs.rects.push(labelRect(v, e.x, e.y, 'start', 12, 700));
    }

    const bounds = { x0: b.x0 + 2, x1: W - 2, y0: b.y0 - 2, y1: b.y1 + 3 };
    // A run that stops early (training collapsed) gets its value and a word at its last point.
    for (const s of plotted.filter((x) => x.points[x.points.length - 1][0] < maxStep)) {
      const [px, py] = s.px[s.px.length - 1];
      placeLabel(root, `${s.points[s.points.length - 1][1].toFixed(1)}, collapsed`, [
        { x: px + 8, y: py + 18, a: 'start' }, { x: px + 8, y: py - 9, a: 'start' },
        { x: px - 8, y: py + 18, a: 'end' }, { x: px - 8, y: py - 9, a: 'end' },
        { x: px, y: py + 22, a: 'middle' }, { x: px, y: py - 13, a: 'middle' },
      ], obs, bounds, 'ch-anno');
    }
    // Name the Cold-Start line where there is room: under or over it, then on it.
    const spots = [[b.x1, 'end'], [b.x0 + 10, 'start'], [(b.x0 + b.x1) / 2, 'middle']];
    placeLabel(root, `Cold-Start ${coldV.toFixed(1)}`, [
      ...spots.flatMap(([x, a]) => [{ x, y: yCold + 17, a }, { x, y: yCold - 6, a }]),
      ...spots.map(([x, a]) => ({ x, y: yCold + 4, a })),
    ], { rects: obs.rects, segs: obs.segs.slice(1) }, bounds, 'ch-anno');

    // Crosshair: a hairline at the hovered step and one dot per run.
    const cross = svg('g', { class: 'ch-cross', visibility: 'hidden' });
    const hair = line(0, b.y0, 0, b.y1, '');
    const dots = plotted.map((s) => svg('circle', { r: 4.5, style: `fill:${s.style.color}` }));
    cross.append(hair, ...dots);
    root.append(cross);
    ui.onHide = () => cross.setAttribute('visibility', 'hidden');

    const steps = [...new Set(plotted.flatMap((s) => s.points.map((p) => p[0])))].sort((a, c) => a - c);
    const show = (step, announce) => {
      hair.setAttribute('x1', r2(X(step)));
      hair.setAttribute('x2', r2(X(step)));
      plotted.forEach((s, i) => {
        const p = s.points.find((q) => q[0] === step);
        dots[i].setAttribute('visibility', p ? 'visible' : 'hidden');
        if (p) { dots[i].setAttribute('cx', r2(X(p[0]))); dots[i].setAttribute('cy', r2(Y(p[1]))); }
      });
      cross.setAttribute('visibility', 'visible');
      const t = selfplayTip(plotted, step, data.base[metric]);
      showTip(ui, t.nodes, boxOf(ui, root, W, X(step), b.y0, X(step), b.y1), 'side');
      if (announce) ui.live.textContent = t.said;
    };
    const go = scrubbable(ui, root, steps, show);
    const nearest = (px) => steps.reduce((a, c) => (Math.abs(X(c) - px) < Math.abs(X(a) - px) ? c : a));
    root.append(scrubArea(ui, root, W, b, nearest, go));
    ui.plot.replaceChildren(root);
  }

  update();
  watchWidth(ui.plot, (w) => { st.w = w; draw(); });
}

function selfplayTip(plotted, step, base) {
  const title = step === 0 ? 'Step 0 (Cold-Start)' : `Step ${step}`;
  const nodes = [el('div', { class: 'ch-tip-title', text: title })];
  const said = [title];
  for (const s of plotted) {
    const p = s.points.find((q) => q[0] === step);
    const last = s.points[s.points.length - 1][0];
    const v = p ? p[1].toFixed(1) : '–';
    nodes.push(tipRow(s.style.color, v, s.name, { sub: p ? null : `collapsed at step ${last}` }));
    said.push(`${s.name}: ${p ? v : `collapsed at step ${last}`}`);
  }
  const foot = `Base model before Cold-Start: ${base.toFixed(1)}`;
  nodes.push(el('div', { class: 'ch-tip-foot', text: foot }));
  said.push(foot);
  return { nodes, said: `${said.join('. ')}.` };
}

/* ------------------------------------------------------------------ */
/* (f) Training metrics: two panels with a shared crosshair             */
/* ------------------------------------------------------------------ */

function renderTraining(container, data) {
  const runs = TRAIN_RUNS.filter((r) => data.runs[r.key]);
  const series = Object.fromEntries(runs.map((r) => [r.key, TRAIN_PANELS.map((p) => trainingSeries(data, r.key, p))]));
  const st = { w: 0 };
  const ui = frame(container, { controls: legend(...runs.map((r) => legendItem(keyLine(r.color), r.label))) });
  ui.summary.textContent = trainingSummary(data);

  function draw() {
    const W = st.w;
    if (!W) return;
    hideTip(ui);
    const side = W >= 620;
    const L = 52;
    const gap = 16;
    const titleH = 22;
    const plotH = side ? 210 : 170;
    const cellH = titleH + plotH + 40;
    const pw = side ? (W - 2 * L - gap - 4) / 2 : W - L - 4;
    const H = side ? cellH : 2 * cellH + 12;
    const root = chartSvg(W, H, ui.summary.textContent, true);
    const steps = series[runs[0].key][0].steps;
    const sMax = steps[steps.length - 1];
    const [xa, xb] = [-3, sMax + 3]; // xlim (-3, 203) in plot_training_metrics.py

    const panels = TRAIN_PANELS.map((p, i) => {
      const b = side ? { x0: L + i * (pw + gap + L), y0: titleH } : { x0: L, y0: i * (cellH + 12) + titleH };
      b.x1 = b.x0 + pw;
      b.y1 = b.y0 + plotH;
      const X = (s) => b.x0 + ((s - xa) / (xb - xa)) * pw;
      const Y = (v) => b.y1 - (Math.min(v, p.max) / p.max) * plotH; // the paper clips at its y limit
      const xTicks = [];
      for (let t = 0; t <= sMax; t += 100) xTicks.push(t);
      root.append(text((b.x0 + b.x1) / 2, b.y0 - 8, p.title, 'ch-panel-title', 'middle'));
      drawAxes(root, b, { X, Y, xTicks, yTicks: p.ticks, xTitle: 'RL training step', yTitle: p.yTitle, yTitleX: b.x0 - L + 12 });

      for (const r of runs) { // Tmax first, AutoEnvScaling on top
        const s = series[r.key][i];
        const path = (pick) => {
          const pts = [];
          for (let k = 0; k < s.steps.length; k++) if (pick[k] != null) pts.push(`${r2(X(s.steps[k]))},${r2(Y(pick[k]))}`);
          return pts.length > 1 ? `M${pts.join('L')}` : '';
        };
        const stroke = `stroke:${r.color}`;
        root.append(svg('path', { class: 'ch-faint', d: path(s.values), style: stroke }));
        root.append(svg('path', { class: 'ch-trend', d: path(s.mean), style: stroke }));
      }
      return { b, X, Y };
    });

    // Shared crosshair across both panels, on the trailing means.
    const cross = svg('g', { class: 'ch-cross', visibility: 'hidden' });
    const parts = panels.map(({ b }) => {
      const hair = line(0, b.y0, 0, b.y1, '');
      const dots = runs.map((r) => svg('circle', { r: 4.5, style: `fill:${r.color}` }));
      cross.append(hair, ...dots);
      return { hair, dots };
    });
    root.append(cross);
    ui.onHide = () => cross.setAttribute('visibility', 'hidden');
    let hovered = 0;
    const show = (step, announce) => {
      const k = steps.indexOf(step);
      panels.forEach(({ X, Y }, pi) => {
        parts[pi].hair.setAttribute('x1', r2(X(step)));
        parts[pi].hair.setAttribute('x2', r2(X(step)));
        runs.forEach((r, ri) => {
          const s = series[r.key][pi];
          parts[pi].dots[ri].setAttribute('cx', r2(X(step)));
          parts[pi].dots[ri].setAttribute('cy', r2(Y(s.mean[k] ?? s.values[k])));
        });
      });
      cross.setAttribute('visibility', 'visible');
      const t = trainingTip(step, k, runs, series);
      const { b, X } = panels[hovered];
      showTip(ui, t.nodes, boxOf(ui, root, W, X(step), b.y0, X(step), b.y1), 'side');
      if (announce) ui.live.textContent = t.said;
    };
    const go = scrubbable(ui, root, steps, (s, a) => { if (a) hovered = 0; show(s, a); }, 10);
    panels.forEach(({ b }, pi) => {
      const pick = (px) => {
        hovered = pi;
        const s = Math.round(xa + ((px - b.x0) / pw) * (xb - xa));
        return Math.max(steps[0], Math.min(sMax, s));
      };
      root.append(scrubArea(ui, root, W, b, pick, go));
    });
    ui.plot.replaceChildren(root);
  }
  watchWidth(ui.plot, (w) => { st.w = w; draw(); });
}

function trainingTip(step, k, runs, series) {
  const extra = runs.some((r) => series[r.key][0].extrapolated[k]);
  const title = `Step ${step}${extra ? ' (Tmax extrapolated)' : ''}`;
  const nodes = [el('div', { class: 'ch-tip-title', text: title })];
  const said = [title];
  TRAIN_PANELS.forEach((p, pi) => {
    nodes.push(el('div', { class: 'ch-tip-head', text: p.yTitle }));
    said.push(p.yTitle);
    for (const r of runs) {
      const s = series[r.key][pi];
      const v = s.values[k].toFixed(p.dec);
      const mean = s.mean[k] == null ? '' : `, ${s.window}-step mean ${s.mean[k].toFixed(p.dec)}`;
      nodes.push(tipRow(r.color, v, `${r.label}${mean}`));
      said.push(`${r.label}: ${v}${mean}`);
    }
  });
  return { nodes, said: `${said.join('. ')}.` };
}

/* ------------------------------------------------------------------ */
/* (d) HiL-Bench table                                                  */
/* ------------------------------------------------------------------ */

const HIL_COLUMNS = {
  instances: 'Instances',
  task_success: 'Task success (%)',
  ask_f1: 'Ask-F1 (%)',
  ask_rate: 'Ask rate (%)',
};

function renderHil(container, data) {
  const fmt = (v, col) => (v == null ? '–' : col === 'instances' ? fmtInt(v) : v.toFixed(1));
  const table = el('table', { class: 'ch-hil' },
    el('caption', { class: 'visually-hidden', text: data.caption }),
    el('thead', {}, el('tr', {},
      el('th', { scope: 'col', text: 'Setting' }),
      data.columns.map((c) => el('th', { scope: 'col', class: 'num', text: HIL_COLUMNS[c] || c })))),
    el('tbody', {}, data.rows.map((row) => el('tr', { class: /AutoEnvScaling/.test(row.setting) ? 'is-ours' : null },
      el('th', { scope: 'row', text: row.setting }),
      row.values.map((v, i) => el('td', { class: 'num', text: fmt(v, data.columns[i]) }))))));
  container.replaceChildren(el('div', { class: 'ch-table-wrap' }, table));
}

/* ------------------------------------------------------------------ */
/* Entry point                                                          */
/* ------------------------------------------------------------------ */

const PARTS = [
  ['#chart-valid', 'results/valid_rates.json', renderValid, 'the valid-environment chart'],
  ['#chart-artifacts', 'results/harness_artifacts.json', renderArtifacts, 'the environment-artifacts chart'],
  ['#chart-solver', 'results/solver_rl.json', renderSolver, 'the solver RL chart'],
  ['#chart-selfplay', 'results/selfplay.json', renderSelfplay, 'the self-play chart'],
  ['#chart-training', 'results/training_metrics.json', renderTraining, 'the training-metrics chart'],
  ['#table-hil', 'results/hilbench.json', renderHil, 'the HiL-Bench table'],
];

/** Render every results piece; a failure in one leaves the others working. */
export async function init(root) {
  await Promise.all(PARTS.map(async ([sel, path, render, what]) => {
    const node = root.querySelector(sel);
    if (!node) return;
    try {
      render(node, await loadJSON(path));
    } catch (err) {
      showError(node, what, err);
    }
  }));
}
