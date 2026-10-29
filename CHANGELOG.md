# Changelog

All notable changes to P6 MCP. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased] (1.10.0)

### Added

- `p6_schedule_risk`: Monte Carlo schedule risk in memory. Each iteration draws the remaining duration of every
  open activity from a triangular range (default 85 / 100 / 135 %; ranges by Activity ID, WBS or activity code
  value; three-point estimates from number UDFs) and runs the forward pass of the same CPM engine as
  `p6_schedule_whatif`. Result: deterministic finish, finish p10 / p50 / p80 / p90, on-time probability against
  the deterministic finish and the must finish date, p80 buffer in working days, the same per milestone, a
  weekly histogram, the activities by criticality index, the engine match rate, seed and engine version, and a
  caveat. The same project, arguments and seed give the same output; a run is capped at 1,500,000 activity
  passes (the result says when). Read only, Full profile.
- `p6_update_integrity`: an update against the previous update, activities matched by Activity ID and links
  by (predecessor, successor, type) as `p6_compare_projects`: open ends tied to the finish, negative float
  cleared by a constraint change (on the activity or on the source of its float) or a later must finish date,
  must finish date moved, actual dates rewritten, durations cut, links to critical work removed, link types
  and lags changed, schedule options and critical definition changed, calendars changed, in-progress or
  critical activities deleted, a data date that did not advance. Each flag gives a count and sample rows with
  before and after values; `flag=` pages one flag. The flags are questions to raise, not proof of
  manipulation. Read only, Full profile.
- `p6_health_check`: `checks.logic_density` (links whose successor is open per open activity, at least 1.5;
  outside `score` and `summary`, which stay the DCMA 14), `weighted_score` (points and grade A to F from
  weighted checks with a warn band), `negative_float.worst` and `high_float.worst` (the activity with the
  lowest or highest total float) and `engine_version`. Health snapshots keep the weighted score and the engine
  version; `p6_health_check_compare` shows both and warns when the engine versions differ.
- `p6_milestones`: `codes=` (Activity IDs, a list or a text separated by commas; any activity type) with
  `not_found`, and per row `hard_constraint`, `predecessor_count`, `vs_constraint_wd`, `float_origin_type`
  and `float_origin_explanation`.
- `p6_compare_projects`: per activity `total_float_before_d`, `total_float_after_d`, `tf_delta_d`,
  `critical_before`, `critical_after`; summary counts `float_eroded`, `became_critical`, `no_longer_critical`.
- `p6_cpm_validate` rows `differences_text`; `engine_version` from `p6_cpm_validate` and
  `p6_schedule_whatif` (the version of the CPM engine's date, calendar and float rules).
- The public demo has a previous update, DEMO-SS225_PREV (a saved baseline of DEMO-SS225 at 19 Oct 2026) with
  the integrity issues `p6_update_integrity` finds.
- Tests for the 17 tools that had none (relationship edits one by one and in batches, float paths, dangling
  ends, redundant links, progress check, group and resource summaries, calendars, health check snapshots
  compared, schedule options read and written, EPS creation, XER inspection), plus checks that every tool is
  named in a test and declares its annotations literally.

### Changed

- Every tool now declares its four MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) as a literal on its own line of the source, so directory scanners that read the code see
  them. The values sent to MCP clients are unchanged.
- The CPM engine's passes are split (forward, backward) and the calendars add working hours through their
  cumulative index: same results, faster runs.
- A skill's reference file may hold 12,000 characters (10,000 before): skills with a larger file need P6 MCP
  1.10.0 (shared skills carry the sharer's version, so older versions never install them).
- The core set has 15 skills: update-integrity-audit, schedule-risk and logic-date-integrity are new;
  schedule-quality-audit v3 (engine check, weighted grade, where the worst float comes from),
  weekly-update-review v3 (optional previous update), milestone-tracker v2 (optional contract milestones) and
  update-import-safely v3 (suggests logic-date-integrity before F9) are updated. The bundled copy follows.

## [1.9.0] - 2026-10-28

### Changed

- No skill is built into P6 MCP any more: every P6 skill comes from the skill shop. At its first start the
  Settings app installs the **core set** of skills from the shop (index entries flagged core), or, when
  GitHub cannot be reached or the shop is turned off, from the copy of the core set shipped with P6 MCP
  (`p6mcp/data/core_skills`, the catalog's files byte for byte). Either way the skills are stored like any
  shop install, with their shop version, so the Skill shop page shows later updates. The skills you had
  turned off in 1.8 are installed turned off, then the P6 skills of the clients that already had some are
  rewritten (same `p6-<name>` folders). Until that first start the server offers the shipped copy as its MCP
  prompts. A fresh install gets its `config.json` at that first start. Config schema 3 (new key
  `core_skills_installed`; `skills_disabled` is emptied once carried over).
- An edited shop skill keeps its source (1.8 made it one of your own): the Skills page shows it as **Shop,
  edited here**, the Skill shop page shows its update, and Update asks before replacing your changes
  (**Copy to my skills** keeps a copy of your own first).
- The Skill shop page has no **Built-in** filter and the Skills page no built-in rows. The built-in entries
  of a catalog written for 1.8.2 read as needing a newer P6 MCP and are never fetched or installed.

### Added

- Twelve detailed core skills in the shop, each with a fixed report template, a field glossary, a
  checklist and a worked example; every figure of a report is a field of a tool result (no arithmetic by
  the model), checked by the catalog's golden tests against independent SQL oracles.
- New read tool `p6_milestones`: every start and finish milestone by date (or by variance), its baseline
  date, variance in calendar and working days, late / early / on time, total float, critical; counts.
- Report fields for the skills: working-day variances in `p6_project_summary` (and every baseline key
  present, null without a baseline); start variance, duration delta, `slip_cause` and `name` in
  `p6_compare_projects` (activity names on relationship rows); `critical`, forecast dates, weeks and
  `critical_only` in `p6_lookahead`; `add_remaining_days` and `float_change` in `p6_schedule_whatif`;
  `score` and the delayed activity's name in `p6_health_check`; EPS fields, `eps=` and `order='eps'` in
  `p6_list_projects`, tree order and project counts in `p6_list_eps`; `bl_planned_pct` in `p6_list_wbs`;
  `min_float_days` in `p6_critical_path`; `period_count` in `p6_progress_curve`; activity names in
  `p6_redundant_links`, `p6_driving_path` and `p6_float_origin`, with fixed row shapes; every import
  action per table in `p6_import_xer` counts; `cards[].text` (as Excel shows it) in `p6_export_dashboard`.
- **Restore core skills** on the Skills and Skill shop pages: installs the core set again (shop, else the
  shipped copy): missing skills come back, edited ones get the shop's version, current ones are kept, your
  own skills are never touched (one with a core skill's name is reported).
- With automatic updates on, the tray app also installs the newer shop versions of the skills installed
  from the shop that were not edited here (each install records the SHA256 of the content it stored), on
  the schedule of the app updates; an open Settings window without unsaved changes reloads them.
- `tools/bundle_core_skills.py` writes the shipped copy from a checkout of the catalog (refuses a file that
  does not match the index SHA256); the PyInstaller spec and the package data include it.
- Skill house rules checked on the shipped core set, reference files included: ASCII, no double hyphen,
  known tools, and every `name=` of a `p6_x(...)` call a parameter of that tool (`skills.house_problems`).

### Removed

- `shop.builtin_meta`, `shop.check_builtin` and `shop.BUILTIN_SINCE` (the catalog's built-in entries).

## [1.8.2] - 2026-10-28

### Added

- The skill shop lists the built-in skills of P6 MCP (weekly update review, schedule quality audit, update
  import, delay what-if, baseline variance, Excel dashboard) with the status **Built-in**: nothing to
  install, the Skills page turns them on or off. A **Built-in** filter on the Shop page; `p6_shop_search`
  reports the status `builtin`, `p6_shop_get` returns the built-in text of this P6 MCP, `p6_shop_install`
  explains that there is nothing to install. P6 MCP 1.8.0 and 1.8.1 show them as needing a newer P6 MCP.

## [1.8.1] - 2026-10-28

### Fixed

- Share on GitHub: the link carried the whole skill, and GitHub's sign-in (with Google in particular) failed
  on that long return link with a Server Error, so no issue was created. The link now opens only the share
  form, and the skill is always copied to the clipboard to paste in its Skill field.

## [1.8.0] - 2026-10-28

### Changed

- P6 commands are now **P6 skills** (Agent Skills format). The built-in ones and your own are written as a
  folder `p6-<name>` with `SKILL.md` and its reference files in the clients' skills folders: Claude Code,
  Cursor and VS Code read `%USERPROFILE%\.claude\skills`, Cline `%USERPROFILE%\.cline\skills`. The AI
  uses a skill when the request matches its description (the built-in descriptions say when). The MCP
  prompts stay (Claude Desktop's + menu). Settings: the Commands page is now **Skills**, the MCP clients
  page **Install skills** / **Update skills** / **Remove skills**; CLI `clients skills install|remove`
  (`clients commands` still works). The command files written by 1.4 to 1.7 are converted to skills when
  the Settings app starts. Config schema 2: `custom_commands` -> `custom_skills`, `commands_disabled` ->
  `skills_disabled` (migrated automatically).

### Added

- Your own skills can carry up to 5 text reference files (checklists, thresholds, examples; .md, .txt,
  .csv, .json; no scripts), written beside SKILL.md and appended to the MCP prompt.
- **Skill shop**: a Shop page in Settings lists the skills shared by users in the public GitHub catalog
  nizarhamza/p6-mcp-shop (read anonymously, only when the page is opened); install, update and remove them.
  **Share on GitHub** (Skills page) opens a prefilled GitHub issue; the maintainer approves it and a GitHub
  Action publishes it. **Export** / **Import** a skill as a zip file for users without a GitHub account
  (the zip is also a skill Claude Desktop can upload).
- Tools `p6_shop_search`, `p6_shop_get` and `p6_shop_install` (Full profile; the install changes P6 MCP's
  settings only, with a dry run and a confirmation token, without needing write permission). Setting
  `shop_enabled` (Tools and safety, on by default); `p6_diagnose` reports it.
- Settings warns before saving over settings another program changed meanwhile (a skill installed by your
  AI client): overwrite or reload.

### Fixed

- A custom command naming a tool that does not exist (a typo) stopped the server from starting and broke
  the Commands and MCP clients pages; such a skill is now simply not offered, and new or edited skills
  refuse unknown tools.

## [1.7.0] - 2026-10-27

### Added

- P6 command `export-dashboard` (Full profile): checks that the schedule is not stale, writes the Excel
  dashboard of a project with `p6_export_dashboard`, then reports the 7 cards as written, the counts, the
  DCMA checks that fail and the warnings. It asks before replacing an existing file (`overwrite`).
- `p6_export_dashboard` also returns `cards` (value and detail of each card, as written on the Dashboard
  sheet) and `dcma_failed` (the DCMA checks that fail).

## [1.6.0] - 2026-10-27

### Added

- `p6_export_dashboard` (Full profile): an Excel project controls dashboard of one project, built by the
  server (no template file): a protected one-page A3 Dashboard sheet with 7 cards, the S-curve, the finish
  variance of each level 1 WBS in working days (colored Delayed, At risk, On track), WBS progress (level 1)
  and level 2 progress against the baseline planned units at the data date, the milestones and the critical
  and delayed activities; a Schedule Overview sheet with every activity under WBS bands (opens folded to
  level 2); the P6 float paths; the DCMA 14 checks with sources and definitions. Values of the last F9,
  native Excel charts, no formulas. Company name and colors are arguments of the export (nothing is
  stored); an existing file is replaced only with `overwrite=True`.
- `p6_diagnose` reports the openpyxl version used by the dashboard.

### Changed

- New dependency: openpyxl (bundled in the installer and the portable zip).

## [1.5.1] - 2026-10-21

### Fixed

- An update could fail half way when P6 MCP Settings or an MCP server was started while setup ran (for
  example a click on the shortcut during an automatic update): setup could not replace a file in use,
  stopped, and left a mix of old and new files (the old "P6" icon showed). Setup now holds a lock while it
  runs; P6 MCP Settings exits at once and opens its window after the update, and a server started then
  exits with "P6 MCP is being updated" (restart the MCP client a minute later).

## [1.5.0] - 2026-10-21

### Added

- Settings > Instructions: the instructions every MCP client receives when it connects, with a preview of
  the exact text. Built-in lines can be turned off (the two-step write rule stays on) and you can add,
  edit, reorder and turn off your own lines, for example "When you show an activity, write it as
  Activity ID - Activity name". Config keys `instructions_disabled` and `custom_instructions`.
- Settings > Commands: every P6 command with whether the current tool settings offer it and a preview of
  its text. Built-in commands can be turned off; New and Copy to my commands create your own commands
  (name, title, description, arguments used as `{argument}` in the text; the P6 rules are appended).
  They are offered as MCP prompts and written by Install / Update commands like the built-in ones.
  Config keys `commands_disabled` and `custom_commands`.
- Both take effect after Save and a restart of the MCP client (the restart banner says so).

## [1.4.1] - 2026-10-21

### Changed

- New P6 MCP logo: executables, installer, Start Menu shortcut, Add/Remove Programs, Settings window and
  notification area icon, plus a logo header in the Settings window. Brand files in `assets/brand`.

## [1.4.0] - 2026-10-20

### Added

- P6 commands: five ready-made workflows that chain the tools into one fixed report: weekly update
  review, schedule quality audit, safe update import, delay what-if and baseline variance. The server
  offers them as MCP prompts (Claude Desktop: + menu; VS Code: /mcp.p6mcp.*; Claude Code:
  /mcp__p6mcp__*), only those whose tools the current tool settings offer (Full profile; the import also
  needs writes).
- MCP clients > Install commands writes them as the client's own command files, so they are typed as
  /p6-weekly-update-review and so on: Claude Code (~/.claude/commands), Cursor (~/.cursor/commands),
  VS Code prompt files (User\prompts) and Cline global workflows (Documents\Cline\Workflows). Update
  commands refreshes them after an update or a settings change; Remove commands deletes only the files
  P6 MCP wrote. Also from the command line: clients commands install|remove <ids>|--detected.
- Claude Code is a supported client: registration in ~/.claude.json (user scope).
## [1.3.0] - 2026-10-20

### Added

- Automatic updates (Logs and updates > "Update P6 MCP automatically", on by default). The tray app
  checks GitHub a minute after it starts and every 6 hours, downloads the new installer, checks its
  SHA256 against the release's `SHA256SUMS.txt`, and installs it silently as soon as no MCP client is
  using P6 MCP; P6 MCP then restarts by itself. Your MCP client settings are left as they are.
- An Update button (Logs and updates, and the banner) installs a new version at once. When MCP clients
  are using P6 MCP it asks whether to close P6 MCP in them now or to update when they close.

### Changed

- With automatic updates on, P6 MCP stays in the notification area when the Settings window closes.
- Turning off the old "check when Settings opens" option also turns automatic updates off.

Versions before 1.3.0 cannot update themselves: install 1.3.0 once from the Releases page.

## [1.2.0] - 2026-10-20

### Added

- `p6_list_udfs`: the user defined fields (UDFs) of activities, projects, WBS, assignments and the other
  subjects, with their data type, how many values they hold (in one project when given), and the sum,
  min and max of number and date UDFs.
- `p6_query_activities` takes `udf_filter` (conditions on UDF values: equal, not equal, before or after,
  contains, empty or not; a date alone covers the whole day) and `udfs` (UDF values added to each row).
- `p6_update_udf` and `p6_update_udfs` (batch, all or nothing): set or clear activity UDF values. Each
  value is checked against the UDF type (text up to 255 characters, numbers stored with 2 decimals,
  whole numbers, dates, indicators UDF_G0 to UDF_G4, codes by short name or name); formula UDFs are
  refused. UDF values do not move dates, so no F9 is needed. Write tools: off unless writes are
  allowed, two-step confirmation as for the other edits.

## [1.1.0] - 2026-10-20

### Added

- `p6_update_assignment` and `p6_update_assignments` (batch, all or nothing): set the budgeted, actual,
  remaining or at completion units of resource assignments. P6's own rules are applied: costs are
  recomputed from the price per unit when the cost is linked to the units, units/time follow (Fixed
  Duration activities only: durations never change), a not started activity keeps budget and remaining
  equal when the project links them, and the activity's labor and nonlabor unit totals follow. Actual
  units need a started activity; a completed activity keeps 0 remaining units. Write tools: off unless
  writes are allowed, two-step confirmation as for the other edits.
- `p6_get_activity` lists the `taskrsrc_id` of each assignment.
- `p6_export_xer` writes a SCHEDOPTIONS block from the project's stored schedule options (as P6's own
  exports do), so a project exported and imported again keeps its options; `p6_schedule_options` shows 0
  differences between a project and its export.

## [1.0.1] - 2026-10-20

Bug fixes from a full review of the server.

### Fixed

- XER export: the path must end with `.xer` in an existing folder, so the database, a backup or a log can
  never be overwritten; the file is written through a temporary file; rows with characters outside
  cp1252 are reported in `warnings` instead of being replaced silently; floats are never written as
  `7.`; dates with a `T` and UTF-8 text blobs are exported correctly.
- XER import of a file with several projects: activity codes of another project's code types are no
  longer imported without a type (the pre-flight lists them); a relationship repeated in the file is
  imported once; a soft-deleted row matched by the import comes back.
- New ids: NEXTKEY is matched on its exact key only (a loose match could lower another table's counter).
- `p6_create_eps` refuses a name already used by a project; `p6_delete_project` marks the projects on
  the other side of deleted cross-project links as needing F9; `p6_delete_activity` checks actual
  dates again under the write lock; `p6_delete_relationships` accepts one link named by id and by pair.
- Health check: the critical path test only delays an activity whose duration can move its finish;
  key_dates percentage counts constraints with an issue; `p6_health_check_compare(after="current")`
  compares against the snapshot it just saved.
- CPM engine: elapsed lag dropped on links to other projects too; constraints without a readable calendar
  keep their duration; faster on large projects.
- `p6_progress_check` leaves LOE and WBS summary activities out of the out-of-sequence links;
  `p6_compare_projects` counts a duplicated link once; `p6_float_origin` says `none` when no source is
  found within `max_depth`.
- Read connections are always closed when a tool returns (the database file is no longer kept open);
  several reports now read their settings and data from one snapshot; a failed backup leaves no partial
  file; a restore no longer marks as undone a change made just before the backup.

## [1.0.0] - 2026-10-19

First public release.

### Added

- MCP server for Primavera P6 Professional standalone (SQLite) databases: 46 tools for projects, WBS,
  activities, relationships, calendars, critical path, driving path, float paths and float origin,
  look-ahead, progress and S-curves, resources, grouping, DCMA 14-point health check and its comparison
  over time, update comparison, dangling and redundant logic, a CPM engine that reproduces P6's F9 for
  validation and what-if scenarios, XER inspection, import and export, backups and edit log.
- Tool policy enforced by the server: Basic (read-only, 11 tools) and Full profiles; write tools need
  write permission, a known or trusted P6 schema, a dry run and its single-use confirmation token;
  destructive tools also need a confirmation text. Backup before every write, refused while P6 runs.
- XER-only mode: read-only analysis of one or more XER files without a P6 database or P6 installed,
  including the comparison of two updates.
- Settings window and notification area app (English and French): database selection with a read-only
  connection test, startup, server, MCP clients, tools and safety, logs and updates, about.
- Automatic registration with Claude Desktop, Cursor, VS Code, VS Code Insiders, LM Studio and Cline,
  editing only the p6mcp entry of their configuration files (comments and formatting kept, backup first),
  plus configuration snippets for other clients.
- Local Streamable HTTP transport on 127.0.0.1 with a bearer token kept in the Windows Credential
  Manager, Host and Origin checks, supervised by the notification area app.
- Per-user Windows installer, portable zip and MCP bundle (.mcpb) for Claude Desktop; GitHub Actions
  for tests and releases, with optional code signing.

[1.7.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.7.0
[1.6.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.6.0
[1.5.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.5.1
[1.5.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.5.0
[1.4.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.4.1
[1.4.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.4.0
[1.3.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.3.0
[1.2.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.2.0
[1.1.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.1.0
[1.0.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.0.1
[1.0.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.0.0
