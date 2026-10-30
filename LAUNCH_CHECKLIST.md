# Launch checklist: MCP for Primavera P6 2.0.0

The manual steps that follow the local commits: release, landing site and MCP directories. The repository
keeps its name (see "Repository name" in [docs/RELEASING.md](docs/RELEASING.md)): every installed copy
downloads its updates from it.

Names used below:

| Item | Value |
|---|---|
| Product | MCP for Primavera P6 (P6 MCP before 2.0) |
| Repository | `nizarhamza/p6-mcp` (no rename) |
| MCP Registry name | `io.github.nizarhamza/p6-mcp` |
| Site | `https://mcp-for-primavera-p6.pages.dev/` (Cloudflare Pages project `mcp-for-primavera-p6`) |
| Skill shop catalog | `nizarhamza/p6-mcp-shop` (unchanged) |

## A. Before pushing

- [x] Demo GIF `docs/images/demo.gif` (30 seconds, 1280 x 720, 0.5 MB): a rendered replay of a real run, not a
      screen recording. The questions "What drives MS-1100 - Project completion in DEMO-SS225?" and "What if
      PRC-TR-1040 finishes 15 working days earlier?" went through the installed server (1.9.0) on the demo database
      of `python tests/fixtures/make_demo.py <folder>`; the GIF shows those tool calls and results. Done
      2026-10-29.
- [x] Read the README and the site once more. Preview the site with
      `python -m http.server 8765 --directory site` and open `http://localhost:8765/`. Done 2026-10-30.
- [ ] Optional follow-up, not blocking: the skill shop catalog (its README, issue form, authoring guide and the
      core skills' texts) still says "P6 MCP".

## B. Release 2.0.0

- [x] Date the 2.0.0 entry of `CHANGELOG.md`, commit.
- [x] Push `main` and the tag (the tag starts the Release workflow, which builds a draft release):

  ```bash
  git -C D:/HAMZA/Documents/p6mcp push origin main
  ```

  ```bash
  git -C D:/HAMZA/Documents/p6mcp tag v2.0.0
  ```

  ```bash
  git -C D:/HAMZA/Documents/p6mcp push origin v2.0.0
  ```

- [x] Run the manual test checklist of `docs/RELEASING.md` on the draft. For 2.0 add: an update over 1.9.0
      shows "MCP for Primavera P6" in Windows Settings > Apps and in the Start menu, the old "P6 MCP Settings"
      shortcut is gone, the client entries (`p6mcp`) and the settings are unchanged. Checked 2026-10-30 on this
      PC's automatic update from 1.9.0 (after the release was published).
- [x] Publish the release (GitHub, Releases, the v2.0.0 draft, Publish). The installed 1.x copies update
      themselves to 2.0.0 within their next checks, and the MCP Registry workflow publishes the 2.0.0 entry.
- [x] Not required (decided 2026-10-30). Code signing: releases ship unsigned (SmartScreen may warn on first
      launch, the README says so). The signing steps stay behind `P6MCP_SIGNING` (`docs/RELEASING.md`, "Code
      signing").

## C. After the release

- [x] Push the catalog commit that pins `P6MCP_REF` to `v2.0.0` (it needs the tag), then check that "Check the
      catalog" is green:

  ```bash
  git -C D:/HAMZA/Documents/p6-mcp-shop push origin main
  ```

- [x] Check the MCP Registry entry:

  ```bash
  curl "https://registry.modelcontextprotocol.io/v0/servers?search=nizarhamza/p6-mcp"
  ```

- [x] Check that this PC updated to 2.0.0 (Settings, About). Done 2026-10-30: the installed server, settings app
      and uninstaller are 2.0.0 (tray update of 2026-10-29).

## D. Landing site on Cloudflare Pages

- [x] Cloudflare dashboard > Workers & Pages > Create > Pages > Connect to Git > `nizarhamza/p6-mcp`.
      Project name `mcp-for-primavera-p6`, production branch `main`, framework preset None, build command empty,
      build output directory `site`. Every push to `main` deploys.
- [x] Check `/`, `/guides/`, the four guides, `/llms.txt`, `/sitemap.xml`, `/robots.txt`, a missing URL (the
      404 page) and the response headers from `site/_headers` (Content-Security-Policy, cache).
- [x] Set the repository homepage:

  ```bash
  gh repo edit nizarhamza/p6-mcp --homepage https://mcp-for-primavera-p6.pages.dev/
  ```

- [x] Not needed for 2.0.0: the subdomain was free. If you add a custom domain later, replace
      `https://mcp-for-primavera-p6.pages.dev` everywhere: `site/` (canonical, Open Graph, JSON-LD, sitemap,
      robots, llms.txt), `server.json` (websiteUrl, icon), `packaging/bundle.py` (SITE) and the README FAQ link.
      `tests/test_registry.py` checks that server.json and the bundle agree.
- [x] Not required (decided 2026-10-30). Trademark note: Oracle's guidelines ask third parties not to use
      Oracle marks in domain names. A custom domain without "primavera" or "p6" is the safer long-term choice.
- [ ] The site has no analytics. If you add Cloudflare Web Analytics, allow its script in the
      Content-Security-Policy of `site/_headers`.

## E. MCP directories

- [x] Official MCP Registry: section C (or automatic in B). `io.github.nizarhamza/p6-mcp` 2.0.0 is active and
      latest since 2026-10-29.
- [x] Not required (decided 2026-10-30). GitHub's MCP registry (github.com/mcp) and VS Code's MCP gallery.
      Checked 2026-10-30: not listed. GitHub's registry is curated (647 servers) and does not mirror the official
      one; GitHub takes nominations by email at partnerships@github.com (GitHub blog, "How to find, install, and
      manage MCP servers with the GitHub MCP Registry").
- [x] Not required (decided 2026-10-30). Glama: `glama.json` is at the repository root (maintainer
      `nizarhamza`). On glama.ai/mcp/servers, add the repository URL, then use Claim. Glama's build check runs in
      Linux containers and cannot start a Windows server: expect the listing without a passing build. Glama
      indexed the repository on its own (rated A, 2026-10-30); its page says "P6 MCP" and 11 tools until it is
      claimed.
- [x] Not required (decided 2026-10-30). MCP Server Finder: it takes submissions by email at
      info@mcpserverfinder.com: name, repository, site, one line ("Local MCP server for Primavera P6 Professional:
      analysis, DCMA check, what-if and safe write-back to the P6 database"), category (productivity or project
      management).
- [x] awesome-mcp-servers (punkpeye): a pull request adding one line, alphabetically, under "Workplace &
      Productivity" (no Primavera server is listed there as of 2026-10-29). Follow the list's format: repository
      link, the Glama score badge once listed, the legend markers for Python, local service and Windows, then:
      "Primavera P6 Professional schedules (database or XER): critical and driving path, DCMA 14-point check,
      update comparison, CPM what-if, and edits written back to the P6 database after a dry run, a confirmation
      and a backup." Pull request #18074 opened 2026-10-30, bot checks green, waiting for the maintainers.
- [x] Not required (decided 2026-10-30). Cline MCP Marketplace: an issue in `cline/mcp-marketplace` with a
      400 x 400 PNG logo (`assets/brand/png/p6mcp-icon-512.png` resized); its form asks you to confirm that Cline
      set the server up from the README or an `llms-install.md`.

## F. After the launch

- [ ] When a guide changes, update its `dateModified` (JSON-LD) and the `lastmod` of `site/sitemap.xml`.
- [ ] The guide figures come from the public demo project: when a tool's output changes, rebuild the demo
      (`tests/fixtures/make_demo.py`) and refresh them.
- [ ] `python tools/gen_docs.py` after adding or renaming a tool (the tests fail until the README and
      `site/llms.txt` tables are regenerated).
