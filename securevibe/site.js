"use strict";

// Source: manuscript tab/transfer-general-transposed.tex and tab/ood_cwe.tex.
// Model order: Qwen baseline, SecureVibe-base, SecureVibe-rl, SecureVibe-hg.
// Full benchmark scores are means over three runs. Unseen-CWE is a separate subset analysis.
const evaluations = {
  baxbench: {name: "BaxBench", security: [19.71, 25.34, 26.62, 23.64], functional: [32.91, 36.48, 41.07, 38.26]},
  susvibes: {name: "SusVibes", security: [10.57, 13.62, 13.26, 14.16], functional: [28.14, 41.76, 41.76, 39.96]},
  swe: {name: "SWE-bench Verified", functional: [60.90, 65.00, 63.40, 64.70]},
  baxbenchUnseen: {name: "BaxBench unseen-CWE subset", security: [10.71, 13.57, 17.86, 13.57], functional: [31.43, 33.57, 39.29, 40.00], gains: {security: 7.14, functional: 8.57}},
  unseen: {name: "SusVibes unseen-CWE subset", security: [7.69, 12.82, 15.38, 19.23], functional: [20.51, 30.77, 33.33, 38.46]}
};
const models = ["Qwen baseline", "SecureVibe-base", "SecureVibe-rl", "SecureVibe-hg"];
const benchmark = document.querySelector("#benchmark");
const metric = document.querySelector("#metric");
const chart = document.querySelector("#result-chart");
const axis = chart.querySelector(".chart-axis");
const state = {benchmark: "unseen", metric: "security"};
let preferredMetric = state.metric;

function syncButtons(group, value, disabled = false) {
  group.querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.value === value));
    button.disabled = disabled && button.dataset.value !== value;
  });
}

// Scale the axis to the data so small differences stay visible; ticks are quarters of a round maximum.
function axisMax(values) {
  const quarter = Math.max(...values) * 1.1 / 4;
  const unit = quarter > 10 ? 5 : 1;
  return Math.ceil(quarter / unit) * unit * 4;
}

function renderResults() {
  const data = evaluations[state.benchmark];
  const generic = state.benchmark === "swe";
  state.metric = generic ? "functional" : preferredMetric;
  syncButtons(benchmark, state.benchmark);
  syncButtons(metric, state.metric, generic);
  const values = data[state.metric];
  const max = axisMax(values);
  const metricName = generic ? "pass@1" : `${state.metric} pass@1`;
  chart.querySelectorAll(".chart-row").forEach((row, index) => {
    row.querySelector(".bar").style.width = `${values[index] / max * 100}%`;
    row.querySelector(".base-mark").style.left = `${values[0] / max * 100}%`;
    const delta = values[index] - values[0];
    const label = row.querySelector("strong");
    label.textContent = `${values[index].toFixed(2)}%`;
    if (index > 0) {
      const gain = document.createElement("small");
      gain.className = delta >= 0 ? "gain up" : "gain down";
      gain.textContent = `${delta >= 0 ? "+" : "−"}${Math.abs(delta).toFixed(2)}`;
      label.append(gain);
    }
  });
  axis.replaceChildren(...[0, 1, 2, 3, 4].map(tick => {
    const span = document.createElement("span");
    span.style.left = `${tick * 25}%`;
    span.textContent = `${max * tick / 4}%`;
    return span;
  }));
  chart.setAttribute("aria-label", `${data.name} ${metricName}: ${models.map((model, index) => `${model} ${values[index].toFixed(2)}%`).join("; ")}.`);
  const best = Math.max(...values.slice(1));
  const winners = models.filter((_, index) => index > 0 && values[index] === best).join(" and ");
  const gain = data.gains?.[state.metric] ?? (best - values[0]);
  document.querySelector("#result-summary").textContent = `${winners} ${winners.includes(" and ") ? "improve" : "improves"} ${data.name} ${metricName} from ${values[0].toFixed(2)}% to ${best.toFixed(2)}% (+${gain.toFixed(2)} points).`;
}
benchmark.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  state.benchmark = button.dataset.value;
  renderResults();
});
metric.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  preferredMetric = button.dataset.value;
  renderResults();
});

// Count the headline gains up once they scroll into view.
const counters = document.querySelectorAll("[data-count]");
if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    observer.unobserve(entry.target);
    const node = entry.target.firstChild;
    const target = Number(entry.target.dataset.count);
    const start = performance.now();
    const tick = now => {
      const t = Math.min((now - start) / 1100, 1);
      node.textContent = `+${(target * (1 - Math.pow(1 - t, 3))).toFixed(1)}`;
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }), {threshold: 0.6});
  counters.forEach(counter => observer.observe(counter));
}

// Fade sections in as they enter the viewport.
const reveal = document.querySelectorAll(".section, .vs-card, .highlights");
if ("IntersectionObserver" in window && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
  document.documentElement.classList.add("reveal-ready");
  const revealer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) { entry.target.classList.add("revealed"); revealer.unobserve(entry.target); }
  }), {rootMargin: "0px 0px -8% 0px"});
  reveal.forEach(node => revealer.observe(node));
}

// Highlight the section-nav link for the section in view.
const navLinks = [...document.querySelectorAll(".section-nav a")];
const navObserver = "IntersectionObserver" in window && new IntersectionObserver(entries => entries.forEach(entry => {
  if (!entry.isIntersecting) return;
  navLinks.forEach(link => link.classList.toggle("active", link.hash === `#${entry.target.id}`));
}), {rootMargin: "-45% 0px -50% 0px"});
if (navObserver) navLinks.forEach(link => { const target = document.querySelector(link.hash); if (target) navObserver.observe(target); });
document.querySelector("#copy-citation").addEventListener("click", async () => {
  const status = document.querySelector("#copy-status");
  const citation = document.querySelector("#bibtex");
  try {
    await navigator.clipboard.writeText(citation.textContent);
    status.textContent = "Citation copied to clipboard.";
  } catch {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(citation);
    selection.removeAllRanges();
    selection.addRange(range);
    status.textContent = "Citation selected. Press Ctrl+C or ⌘C to copy.";
  }
});
renderResults();

// Teacher-hint examples: one pair visible at a time.
const hintTabs = document.querySelector("#hint-tabs");
hintTabs?.addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  hintTabs.querySelectorAll("button").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  document.querySelectorAll("[data-hint-panel]").forEach(panel => { panel.hidden = panel.dataset.hintPanel !== button.dataset.hint; });
});

// Light/dark theme toggle; the choice is remembered per browser when storage is available.
const themeToggle = document.querySelector("#theme-toggle");
const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const currentTheme = () => document.documentElement.dataset.theme || (darkQuery.matches ? "dark" : "light");
function syncThemeToggle() {
  themeToggle.setAttribute("aria-label", currentTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme");
}
themeToggle.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("sv-theme", next); } catch {}
  syncThemeToggle();
});
darkQuery.addEventListener?.("change", syncThemeToggle);
syncThemeToggle();
