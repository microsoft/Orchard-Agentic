import { showError } from './util.js';

const SECTIONS = [
  ['replay-app', './replay.js', 'the episode replay'],
  ['compare-app', './compare.js', 'the comparison'],
  ['env-browser', './browser.js', 'the environments'],
  ['results', './charts.js', 'the results'],
];

for (const [id, path, what] of SECTIONS) {
  const root = document.getElementById(id);
  if (!root) continue;
  import(path)
    .then((mod) => mod.init(root))
    .catch((err) => showError(id === 'results' ? document.getElementById('chart-valid') : root, what, err));
}

// Copy buttons: <button class="copy" data-copy="#selector">
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('button.copy[data-copy]');
  if (!btn) return;
  const text = document.querySelector(btn.dataset.copy)?.textContent || '';
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = 'Copied';
  } catch {
    btn.textContent = 'Press ⌘C';
  }
  setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
});
