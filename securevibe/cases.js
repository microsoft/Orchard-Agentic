"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  let data;
  let selected = 0;
  let position = 0;
  let playback = null;
  const panel = $("#case-details");
  const taskButtons = [...document.querySelectorAll(".suite-task")];
  const explorer = $("#case-explorer");

  function stop() {
    window.clearInterval(playback);
    playback = null;
    $("#case-play").textContent = "Play";
    $("#case-play").setAttribute("aria-label", "Play selected moments");
  }

  // Stage families give the timeline a consistent color code across cases.
  const stageKinds = {
    plan: ["Request", "Inspect", "Diagnose", "Identify", "Plan", "Design"],
    code: ["Code", "Candidate", "Export", "Submit", "Deliver"],
    test: ["Test", "Verify", "Run"],
    revise: ["Revise"]
  };
  const stageKind = stage => Object.keys(stageKinds).find(kind => stageKinds[kind].includes(stage)) || "plan";

  // Lightweight highlighter for mixed evidence (code, logs, JSON, prose). Text is escaped before markup is added.
  const escapeHtml = text => text.replace(/[&<>"]/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));
  const keywords = "def|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|import|from|try|except|raise|with|as|class|function|var|let|const|new|this|self|null|undefined|true|false|async|await";
  const codeToken = new RegExp([
    String.raw`("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|\`[^\`\n]*\`)`,
    String.raw`((?:#|\/\/)[^\n]*)`,
    String.raw`(✓|\bPASSED\b|\bPASS\b|\bpassed\b)`,
    String.raw`(✗|\bFAILED\b|\bFAIL\b|\bfailed\b|\b[Ee]rror\b)`,
    String.raw`\b(${keywords})\b`,
    String.raw`\b(\d+(?:\.\d+)?)\b`
  ].join("|"), "g");
  const proseToken = /(`[^`\n]+`)|(✓)|(✗)/g;
  function highlight(text) {
    const lines = text.split("\n");
    const prose = !/[={;]/.test(text) && text.length / lines.length > 80;
    const classes = prose ? ["tk-s", "tk-ok", "tk-bad"] : ["tk-s", "tk-c", "tk-ok", "tk-bad", "tk-k", "tk-n"];
    let html = "";
    let last = 0;
    for (const match of text.matchAll(prose ? proseToken : codeToken)) {
      const group = match.slice(1).findIndex(Boolean);
      html += escapeHtml(text.slice(last, match.index)) + `<span class="${classes[group]}">${escapeHtml(match[0])}</span>`;
      last = match.index + match[0].length;
    }
    return html + escapeHtml(text.slice(last));
  }

  function renderMoment() {
    const current = data.cases[selected];
    const step = current.steps[position];
    $("#moment-stage").textContent = step.stage;
    $("#moment-stage").dataset.kind = stageKind(step.stage);
    $("#moment-title").textContent = step.title;
    $("#moment-summary").textContent = step.summary;
    $("#moment-evidence").innerHTML = highlight(step.evidence);
    $("#moment-note").textContent = step.note;
    $("#moment-refs").textContent = `Source message${step.messages.length > 1 ? "s" : ""} ${step.messages.join(", ")}`;
    $("#case-counter").textContent = `${position + 1} / ${current.steps.length}`;
    $("#case-position").max = current.steps.length;
    $("#case-position").value = position + 1;
    $("#case-position").setAttribute("aria-valuetext", `${position + 1} of ${current.steps.length}: ${step.title}`);
    $("#case-prev").disabled = position === 0;
    $("#case-next").disabled = position === current.steps.length - 1;
    [...$("#case-timeline").children].forEach((button, index) => button.setAttribute("aria-pressed", String(index === position)));
    $(".evidence-block").scrollTop = 0;
  }

  function move(next) {
    stop();
    position = Math.max(0, Math.min(next, data.cases[selected].steps.length - 1));
    renderMoment();
  }

  function choose(index, moment = 0) {
    stop();
    selected = index;
    const current = data.cases[selected];
    position = Math.max(0, Math.min(moment, current.steps.length - 1));
    $("#case-title").textContent = current.title;
    $("#case-tags").textContent = `${current.taskType} · ${current.benchmark} · ${current.cwes.join(" / ")}`;
    $("#case-requirement-label").textContent = current.requirementLabel;
    for (const field of ["task", "hidden", "instance"]) $("#case-" + field).textContent = current[field];
    $("#case-source").href = data.sourceUrl;
    $("#case-row").textContent = `JSONL row ${current.sourceRow} (one-based)`;
    $("#case-status").textContent = "";
    syncTaskButtons();
    $("#case-timeline").replaceChildren(...current.steps.map((step, i) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${String(i + 1).padStart(2, "0")} ${step.stage}`;
      button.dataset.kind = stageKind(step.stage);
      button.setAttribute("aria-label", `Moment ${i + 1}: ${step.title}`);
      button.addEventListener("click", () => move(i));
      return button;
    }));
    renderMoment();
  }

  function syncTaskButtons() {
    taskButtons.forEach((button, index) => {
      const expanded = !panel.hidden && selected === index;
      button.setAttribute("aria-expanded", String(expanded));
      button.querySelector(".task-action").textContent = expanded ? "Collapse example ↑" : "View example ↓";
    });
  }

  function collapse() {
    stop();
    panel.hidden = true;
    syncTaskButtons();
  }

  taskButtons.forEach((button, index) => button.addEventListener("click", () => {
    if (!panel.hidden && selected === index) { collapse(); return; }
    selected = index;
    panel.hidden = false;
    if (data) choose(index);
    else syncTaskButtons();
    panel.scrollIntoView({block: "start", behavior: "instant"});
  }));
  $("#case-close").addEventListener("click", () => {
    collapse();
    taskButtons[selected].focus();
  });

  function readPermalink() {
    const match = location.hash.match(/^#case=([a-z-]+)&moment=(\d+)$/);
    if (!match || !data) return false;
    const index = data.cases.findIndex(item => item.id === match[1]);
    if (index < 0) return false;
    panel.hidden = false;
    choose(index, Number(match[2]) - 1);
    panel.scrollIntoView({behavior: "instant", block: "start"});
    return true;
  }

  async function loadCases() {
    $("#case-loader").hidden = false;
    $("#case-loader").textContent = "Loading case studies…";
    $("#case-retry").hidden = true;
    try {
      const response = await fetch("assets/cases.json?v=10", {cache: "no-cache"});
      if (!response.ok) throw new Error("Case data unavailable");
      data = await response.json();
      if (data.schemaVersion !== 1 || !data.cases?.length) throw new Error("Unsupported case data");
      explorer.hidden = false;
      $("#case-loader").hidden = true;
      if (!readPermalink()) choose(selected);
    } catch {
      $("#case-loader").textContent = "Case studies could not load. Retry, or preview this page through the local HTTP server described in the hosting guide.";
      $("#case-retry").hidden = false;
    }
  }
  $("#case-retry").addEventListener("click", loadCases);
  $("#case-prev").addEventListener("click", () => move(position - 1));
  $("#case-next").addEventListener("click", () => move(position + 1));
  $("#case-position").addEventListener("input", event => move(Number(event.target.value) - 1));
  $("#case-play").addEventListener("click", () => {
    if (playback) { stop(); return; }
    if (position === data.cases[selected].steps.length - 1) { position = 0; renderMoment(); }
    $("#case-play").textContent = "Pause";
    $("#case-play").setAttribute("aria-label", "Pause selected moments");
    playback = window.setInterval(() => {
      position += 1;
      renderMoment();
      if (position === data.cases[selected].steps.length - 1) stop();
    }, 6000);
  });
  explorer.addEventListener("keydown", event => {
    if (["INPUT", "SELECT", "TEXTAREA", "BUTTON", "A", "SUMMARY"].includes(event.target.tagName)) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault(); move(position + (event.key === "ArrowRight" ? 1 : -1));
    }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  window.addEventListener("hashchange", readPermalink);
  $("#case-link").addEventListener("click", async () => {
    const link = new URL(location.href);
    link.hash = `case=${data.cases[selected].id}&moment=${position + 1}`;
    try {
      await navigator.clipboard.writeText(link.href);
      $("#case-status").textContent = "Link to this case and moment copied.";
    } catch {
      $("#case-status").textContent = `Copy this link: ${link.href}`;
    }
  });
  $("#evidence-copy").addEventListener("click", async () => {
    const button = $("#evidence-copy");
    try {
      await navigator.clipboard.writeText(data.cases[selected].steps[position].evidence);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy failed";
    }
    window.setTimeout(() => { button.textContent = "Copy"; }, 1600);
  });
  loadCases();

  const root = "/data/uploads";
  let pathCheckCount = 0;
  function syncPathPresets() {
    document.querySelectorAll("#path-presets button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.path === $("#path-input").value)));
  }
  function markPathPending() {
    $("#path-result").hidden = true;
    $("#path-status").textContent = "Filename changed. Select Check path to see updated results.";
    syncPathPresets();
  }
  function normalize(path) {
    const segments = [];
    for (const segment of path.split("/")) {
      if (!segment || segment === ".") continue;
      if (segment === "..") segments.pop();
      else segments.push(segment);
    }
    return "/" + segments.join("/");
  }
  function checkPath() {
    const input = $("#path-input").value;
    const invalid = !input.length || input.includes("\0");
    const path = normalize(input.startsWith("/") ? input : `${root}/${input}`);
    const contained = path === root || path.startsWith(root + "/");
    $("#path-normalized").textContent = invalid ? "Enter a nonempty path without NUL characters." : path;
    const policies = [
      {name: "Join only", code: "normalize(join(root, input))", allow: true},
      {name: "Naive prefix", code: "resolved.startsWith(root)", allow: path.startsWith(root)},
      {name: "Directory boundary", code: "resolved === root || resolved.startsWith(root + '/')", allow: contained}
    ];
    $("#policy-results").replaceChildren(...policies.map(policy => {
      const card = document.createElement("article");
      card.className = "policy-card";
      const heading = document.createElement("h4"); heading.textContent = policy.name;
      const code = document.createElement("code"); code.textContent = policy.code;
      const verdict = document.createElement("p"); verdict.className = "policy-verdict";
      const explanation = document.createElement("p"); explanation.className = "policy-explanation";
      if (invalid) {
        verdict.textContent = "No result"; explanation.textContent = "Enter a valid test input.";
      } else {
        const unsafe = policy.allow && !contained;
        card.dataset.result = unsafe ? "unsafe" : "safe";
        verdict.textContent = unsafe ? "Admitted · escapes root" : policy.allow ? "Admitted · inside root" : "Blocked · outside root";
        explanation.textContent = unsafe ? "A functional path operation would cross the intended boundary." : policy.allow ? "Lexically contained; filesystem properties remain unchecked." : "This lexical escape is rejected.";
      }
      card.append(heading, code, verdict, explanation);
      return card;
    }));
    $("#path-explanation").textContent = invalid ? "" : contained
      ? "This path stays within the root after normalization. Rejecting every '..' would also reject some legitimate filenames; containment is the property being checked."
      : path.startsWith(root)
        ? "A sibling such as uploads-backup shares the text prefix but is not inside uploads. A directory-separator boundary distinguishes them."
        : "Joining or normalizing a path does not enforce containment. The resolved target must also be checked against the allowed root.";
    pathCheckCount += 1;
    $("#path-result-title").textContent = `Path check #${pathCheckCount}`;
    $("#path-status").textContent = invalid
      ? "Enter a nonempty filename to check."
      : `Check #${pathCheckCount} complete. ${policies.filter(policy => policy.allow).length} of 3 policies admit this path. Results below.`;
    $("#path-result").hidden = false;
    syncPathPresets();
    $("#path-result").focus({preventScroll: true});
    $("#path-result").scrollIntoView({block: "nearest", behavior: "instant"});
  }
  $("#path-form").addEventListener("submit", event => { event.preventDefault(); checkPath(); });
  document.querySelectorAll("#path-presets button").forEach(button => button.addEventListener("click", () => { $("#path-input").value = button.dataset.path; markPathPending(); }));
  $("#path-input").addEventListener("input", markPathPending);
  syncPathPresets();
})();
