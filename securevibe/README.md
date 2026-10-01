# SecureVibe project page in Orchard-Agentic

Dependency-free static site inspired by the academic layout of
https://auto-env-scaling.github.io/. HTML, styles, and scripts are original;
figures are copied from the SecureVibe training repository's `assets/figures/`
directory. This is a standalone project page within the Orchard-Agentic
hub, at `securevibe/` on the `gh-pages` branch.

## Preview locally

From the root of a `gh-pages` checkout:

```bash
python3 -m http.server 8000
```

Open http://localhost:8000/securevibe/. No Node dependencies or build command
are needed.

## Publish on GitHub Pages

The Orchard-Agentic hub is served from the root of the `gh-pages` branch at
https://microsoft.github.io/Orchard-Agentic/. Update this page with a pull
request against `gh-pages`; it is then live at
https://microsoft.github.io/Orchard-Agentic/securevibe/.

All asset URLs are relative, and the "← Orchard-Agentic" link points to `../`
(the hub). The hub links here from its News and Projects sections. The
root `.nojekyll` covers this directory.

## Content maintenance

- `index.html`: authors, affiliations, narrative, resource links, and citation.
- `style.css`: responsive layout, colors, and typography.
- `site.js`: interactive benchmark scores and citation copying.
- `cases.js` and `cases.css`: case-study replay, permalink sharing, and the
  interactive path-containment lab.
- `assets/cases.json`: curated public training excerpts and source provenance.
- `assets/`: the release figures (the overview also as WebP at 1200 and 2400
  px), institution logos in `assets/logos/`, and a small SVG favicon.

The paper button links to https://arxiv.org/abs/2609.38606. The BibTeX includes
the arXiv identifier, archive, primary category, and paper URL.

Author order and affiliations come from the local arXiv manuscript. Headline
claims and method descriptions follow the release README. The interactive
results are transcribed from `tab/transfer-general-transposed.tex` and
`tab/ood_cwe.tex` in that manuscript. They are not recomputed from raw grades.
Keep the three-run full-benchmark means distinct from the unseen-CWE subset.
The chart scales its axis to the largest value shown (rounded up), marks the
baseline with a dashed line, and does not display uncertainty bars.

The two behavior charts in the Behaviors section are HTML, not images. Their
values are transcribed from the data labels in `assets/security-suite-behaviors.png`
and `assets/post-training-behaviors.png`; the post-training deltas use the
figure's printed values rather than differences of rounded endpoints. Update
both the PNGs and the HTML if the figures change.

The page supports light and dark themes (system preference, plus a toggle that
stores the choice in `localStorage` under `sv-theme`).

Figures are deliberately bundled in `securevibe/assets/` so the page
has no dependencies outside its own directory. Refresh these copies when
the release figures change. No private trajectories or dataset files are
included in the site.

## Interactive case studies

Four real SFT training demonstrations cover exactly one example each of
Functionality-Focused Coding, Security-Focused Coding, Security Planning, and
Security Testing, in that order. They provide selected moments, with step
buttons, previous/next controls, a scrubber, and opt-in playback (six seconds
per moment). Playback stops on manual navigation, case changes, the last
moment, or when the document is hidden. The replay container supports left
and right arrow keys when focused. “Copy case link” preserves the case and
selected moment in the URL hash.

These are public training examples, not rollouts from trained SecureVibe
checkpoints and not a controlled model comparison. Editorial summaries are
separated from source excerpts. Message numbers are one-based indices in the
source record's `messages` array, not consecutive model turns. The source
file, revision, JSONL row, record hash, and file hash identify each example.
Reported correctness labels come from the source metadata; printed test
verdicts are identified separately, including their observed limitations.

The source is the public `dqwang122/SafeVibe` dataset at revision
`66af0caa23fba85eb60bb55af4cba333032fc1e6`. The local source file was verified
against that release's `SHA256SUMS`; private evaluation trajectories were not
used. Only selected evidence is shipped. Temporary directory names are
redacted, and source metadata containing host paths is not copied.
Underlying benchmark materials retain their source licenses and terms.
The path-helper example is from OpenDiamond through PatchEval-Gen; the
form-builder example (`form-xss`) is an Express AutoBaxBench functionality-coding
record (row 11, `text-none`, from the functional recipe). Its prompt never
mentions escaping or attacks; the replay shows the agent's unprompted bcrypt
hashing and HTML escaping, a working JSON/HTML listing, a 403 for a second user,
and a planted `<script>` payload returned as escaped text. It notes that the JWT
secret is hard-coded and that only one XSS payload is tried. The third example, `ssrf-redirect`, is a
PatchEval-Gen security-planning record for CVE-2023-28155 (CWE-918), from JSONL
row 1224. It has no functional/security pass grades; its final JSON validation
is explicitly distinguished from verifying an SSRF defense. The lint excerpt
removes the source workspace prefix. The fourth, `traversal-test`, is a Security
Testing record for CVE-2022-31506, from row 4696. It compares the same generated
test against two agent-constructed candidates: four passing checks on the
guarded candidate versus two failed traversal checks and two passing existence
checks on the naive candidate. These are recorded development runs, not
independent benchmark grades. Each case explicitly names its training task.
Follow the linked public dataset for
source attribution and terms.

Regenerate the curated JSON from the exact released SFT file:

```bash
python3 securevibe/tools/build_cases.py /path/to/sft_security_suite.jsonl
```

The builder verifies the full file checksum before selecting the reviewed
records. It never executes commands from the trajectories.

The path lab is an original illustrative simulation, not recorded evidence.
It normalizes plain POSIX paths and compares three lexical admission policies.
It does not access files, resolve symlinks, decode URL paths, execute shell
commands, or measure model performance. All user input and source excerpts
are rendered with `textContent`.

### Collection prompts versus SFT prompts

The checksum-matched preparation documentation in
`data/recipes/SFT/0729_sft/README.md` explains that security-coding task bodies
are replaced with functionality-only prompts before SFT. PatchEval uses its
raw problem statement; AutoBax uses the corresponding `text-none` prompt.
Assistant and tool messages remain unchanged. Therefore the case task labels
describe collection provenance, not the presence of a hint in the released
user message. Row 31 is security-coding provenance with its guidance removed;
row 211 is functionality-coding provenance. The page does not reconstruct or
quote missing collection-time guidance. Authentication and per-user reports
are explicit in row 211; injection and export-containment requirements are
implicit. Requirement headings vary by task instead of calling every
security requirement hidden.

The case replay is integrated under “Build a foundation with the Security
Suite.” Its four task cards open a shared example panel, collapsed on ordinary
page load. Selecting the active card or “Collapse example” closes it and
stops playback. Saved case/moment links open the matching example directly.
The path lab appears with the path-coding and security-testing examples.

The results selector also includes the 140-instance BaxBench unseen-CWE
subset from the arXiv manuscript's `tab/ood_cwe.tex` (CWE-117, CWE-400,
CWE-434). In baseline/base/RL/HG order, functional scores are
31.43/33.57/39.29/40.00 and security scores are 10.71/13.57/17.86/13.57.
The best security gain is reported as 7.14 points, matching the manuscript's
unrounded calculation rather than subtracting rounded display values.
