# Orchard-Agentic project hub

Static GitHub Pages site for the Orchard-Agentic collection of research
projects, shared infrastructure, publications, and resources.

Self-contained HTML/CSS/JS — no build step, no dependencies.

```
site/
├── index.html              # Orchard-Agentic collection landing page
├── orchard.html            # Orchard paper and project details
├── .nojekyll               # let GitHub Pages serve the assets/ folder as-is
└── assets/
    ├── css/style.css
    ├── js/main.js          # BibTeX copy button + scroll reveal
    └── img/
        ├── favicon.svg
        └── orchard-overview.png   # Orchard project overview
```

> Note: the teaser image was renamed from `Orchard Overview.png` to
> `orchard-overview.png` — spaces in filenames break URLs on GitHub Pages.

## Preview locally

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

## Deploy to GitHub Pages

Push these files to the root of the `gh-pages` branch. GitHub Pages serves the
hub at the repository's configured Pages URL.

The `.nojekyll` file is included so Pages serves the `assets/` directory verbatim.
