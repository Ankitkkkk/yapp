# Website maintenance

The public site is [yapp.riggedcode.com](https://yapp.riggedcode.com/), served
by GitHub Pages from `site/`. The workflow in `.github/workflows/pages.yml`
deploys changes on `main` and copies the root `install.sh` and `llms.txt` into
the published artifact. There is no frontend build step.

Bricolage Grotesque is self-hosted in `site/assets/fonts/` so rendering does not
depend on an external font service. Its SIL Open Font License is shipped beside
the font. The WOFF2 file is a format conversion of the variable TTF from the
[Google Fonts source](https://github.com/google/fonts/tree/main/ofl/bricolagegrotesque).

The website uses warm paper (`#f4f1e8`), dark ink (`#15221b`), forest green
(`#215f44`), and lime (`#d7fa8a`) around terminal illustrations. A dark palette
follows the system preference. Bricolage carries headings and prose; monospace
labels stay with commands and keyboard controls. Keep the editorial layout,
clear rules between sections, and restrained corners instead of adding more
card decoration. The website theme is separate from the TUI's Catppuccin theme.

The homepage Markdown switch is a static illustration with an accessible
toggle, not an agent connection or Markdown parser. Without JavaScript the
formatted example remains visible; installation and navigation still work.

## Content and search intent

| Page | Purpose and search intent |
| --- | --- |
| `/` | Explain the multi-agent coding workspace: AI coding agents, Claude Code and Codex collaboration, MCP chat, and terminal orchestration. |
| `/docs/` | Help someone install yapp, create a session, connect agents, and troubleshoot setup on Linux, macOS, or WSL2. |
| `/features/` | Explain the terminal, Markdown/F7, sessions, orchestration, CLI/MCP automation, browser workflows, and recovery with links into the setup guide. |

These phrases describe the product; they are not search-volume estimates.
Use them naturally in useful content. Keep each page's title, description,
canonical URL, and social metadata aligned. Preserve the existing social image
and descriptive alternative text. `sitemap.xml` lists indexable pages; update
its modification dates only when the corresponding content changes. The 404
page stays `noindex`.

The homepage describes the software with `SoftwareApplication` and its source
with `SoftwareSourceCode` JSON-LD. The guide includes page and breadcrumb data.
There are no fabricated reviews, ratings, or FAQ rich-result promises. Follow
[Google's SEO starter guide](https://developers.google.com/search/docs/fundamentals/seo-starter-guide)
for content, titles, and crawlable links. Rankings and indexing need separate
observation after publication.

## Check documentation against the implementation

- CLI options and JSON support: `cli.py` (`build_parser` and `main`).
- Session creation: `cli_workspaces.py`, `cli_tui_dialogs.py`, and `app.py`.
  The TUI starts a configured orchestrator; plain `yapp new` does not.
- Provider support: `config.toml`, `providers/__init__.py`, and
  `workspace_launcher.py`. API wrappers and terminal-session agents differ.
- Installer paths and release selection: `install.sh` and `pyproject.toml`.
- Configuration precedence: `config_loader.py`. Local agent names must be new;
  `[updates]` overrides are also supported.
- Updates: `updates.py`, `cli_update.py`, and `cli_tui_update.py`.

Keep the website guide, `README.md`, `INSTALLATION.md`, and `llms.txt` consistent.
Do not run provider-starting examples as documentation smoke tests. CLI parsing
and `--help` can be checked without starting providers.

## Browser verification

Run the dependency-free publication checks first:

```sh
python3 tests/check_site.py
node --check site/assets/site.js
```

They validate unique page titles/descriptions, matching social metadata,
canonical URLs, JSON-LD, sitemap coverage, local assets and cross-page anchors.
GitHub Pages runs the static checks and Chromium workflow before publishing.
Failed checks prevent deployment; screenshots are retained for seven days.

Use a separate test environment:

```sh
python3 -m venv /tmp/yapp-site-check-venv
/tmp/yapp-site-check-venv/bin/pip install playwright
/tmp/yapp-site-check-venv/bin/python -m playwright install chromium
/tmp/yapp-site-check-venv/bin/python tests/browser/site.py --artifacts /tmp/yapp-site-screens
```

The script serves a temporary copy of the Pages artifact on an unused local
port, with external network requests blocked. It checks clipboard success and denial,
the Markdown/raw-source toggle, keyboard skip navigation, widths from 320 to
1440 pixels, local links, FAQ interaction, and navigation without JavaScript.
It captures all four pages in light and dark mode when `--artifacts` is supplied.
Inspect those screenshots, including the dark theme. This does not establish
compatibility with every browser or assistive technology.

For a manual preview, copy `site/`, `llms.txt`, and `install.sh` into a temporary
directory and serve it with `python3 -m http.server --bind 127.0.0.1`.
Do not start `run.py` or real coding agents to preview the marketing site.
After pushing, check the Pages deployment and all three public page URLs.
Use Search Console's URL inspection and sitemap submission to observe indexing;
metadata checks alone do not establish search visibility or rankings.
