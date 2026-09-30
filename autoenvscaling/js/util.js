// Small helpers shared by every section.

const cache = new Map();

/** Fetch JSON under data/, cached per path. */
export function loadJSON(path) {
  if (!cache.has(path)) {
    cache.set(path, fetch(`data/${path}`).then((r) => {
      if (!r.ok) throw new Error(`${path}: ${r.status}`);
      return r.json();
    }));
  }
  return cache.get(path);
}

/** el('div', {class: 'x', onclick: fn}, child, 'text', ...) */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Like el(), for SVG elements. */
export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'text') node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const STAGES = {
  R: { name: 'Read', desc: 'reads the failed run, the parent task, or its tools' },
  B: { name: 'Build', desc: 'builds the environment: data, generators, image' },
  W: { name: 'Write', desc: 'writes the instruction, solution, tests, or notes' },
  T: { name: 'Test', desc: 'runs something to measure the task' },
};

export const fmtInt = (n) => (n == null ? '–' : Number(n).toLocaleString('en-US'));

/** Parse `#key=value` from the location hash, or null. */
export function hashParam(key) {
  const m = location.hash.match(new RegExp(`^#${key}=(.+)$`));
  return m ? decodeURIComponent(m[1]) : null;
}

/** Point the hash at `#key=value` without adding a history entry per click. */
export function setHash(key, value) {
  history.replaceState(null, '', `#${key}=${encodeURIComponent(value)}`);
}

/** Render an error in place of a section instead of failing silently. */
export function showError(root, what, err) {
  console.error(what, err);
  root.replaceChildren(el('p', { class: 'loading' }, `Could not load ${what}.`));
}
