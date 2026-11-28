# Changelog

All notable changes to MCP for Primavera P6 (called P6 MCP before 2.0). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Tool messages in French: errors and refusals (policy, writes off, EPPM, XER-only, SQL Server / Oracle, remote),
  dry-run plan lines and `next`, notes, caveats, warnings, definitions and explanations, and p6_diagnose advice
  follow the Language setting of the Settings window (`language`; `auto` = the Windows display language), read on
  every call, so a change applies without a restart. English stays the reference: field names, keys, codes and
  statuses, P6 names, units, tool names, descriptions and schemas, the server instructions, the confirmation_text
  to type, the edit log, audit logs and the Excel workbooks are unchanged. Messages are built with
  `p6mcp.messages._t` (an English str that keeps its template), rendered at the edge of each call from
  `p6mcp/locales/tools/fr.json` (about 1,260 templates); `tools/tool_messages.py --check` and
  `tests/test_tool_messages.py` keep the catalog complete and refuse a new raw English message.
- Five more MCP clients registered by the installer and the Settings Clients page (register, repair, remove, the
  p6mcp entry only, a backup of the file first, a file that cannot be read is refused and left untouched):
  - **Devin** (Windsurf became Devin Desktop in June 2026): `%APPDATA%\devin\mcp_config.json`, read by Devin
    Desktop and the Devin CLI.
  - **Zed**: `context_servers` of `%APPDATA%\Zed\settings.json` (comments kept).
  - **Continue**: a block file of its own, `~/.continue/mcpServers/p6mcp.yaml` (written as JSON, which is valid
    YAML); the user's `config.yaml` is never edited. HTTP entries use `streamable-http` with `requestOptions`.
  - **Gemini CLI**: `mcpServers` of `~/.gemini/settings.json` (HTTP entries use `httpUrl`).
  - **Codex** (CLI, IDE extension and ChatGPT desktop app): the `[mcp_servers.p6mcp]` table of
    `~/.codex/config.toml` (`CODEX_HOME` followed), with a 30 s start and 600 s tool timeout. `p6mcp/tomledit.py`
    edits only that table and its subtables, keeps every other byte, refuses a file that does not parse or an
    entry written as an inline table or dotted keys, and checks every edit by parsing it again.
- P6 skills for Codex, Zed, Gemini CLI and Devin in `~/.agents/skills`, the folder the four share (Install skills
  once serves them all). Continue gets no skills (its loader reads its own skills folder twice).
- Remote file handoff for claude.ai and ChatGPT (setting **Allow file downloads and uploads** on the Remote access
  page, `remote_allow_files`, off by default; restart remote access after turning it on):
  - `p6_export_xer`, `p6_export_dashboard` and `p6_export_lookahead` are offered to remote clients: they write
    into a private folder of the connection (`%LOCALAPPDATA%\P6MCP\remote\files`; remotely `file_name` replaces
    `path` and `overwrite`) and return `download_url`, a one-time link (single use, 15 minutes) to a page whose
    Download button sends the file once, then deletes it.
  - `p6_remote_upload_link` (remote clients only) returns `upload_url`, a one-time page (15 minutes) where the user
    sends one `.xer` of up to 50 MB from the browser (streamed to disk, checked as an XER), and an `upload_id` that
    `p6_inspect_xer` and `p6_import_xer` take instead of a path for an hour, for that connection only. The import
    stays a write: remote edits switch, a connection approved with edits, dry run and confirmation.
  - Links carry only a 256-bit random token, stored hashed in `auth.db` and compared in constant time; ending a
    connection voids its links; expired files are removed at start, on access and every minute; at most 10 files
    wait per connection; the audit log records links minted, used and expired and uploads received or refused,
    never tokens, file names or contents (request paths are logged with the token left out, and uvicorn's access
    log is off). Rate limits for `/files` and `/upload`; the upload route alone accepts a body above 1 MB. The
    relay Worker already forwards every path: no Repair is needed.

### Changed

- Settings window accessibility (ACCESSIBILITY.md):
  - Screen readers: every button, field, list, table and card has a name (form labels are tied to their fields,
    lists and tables are named after their box); a status that changes (test results, port check, server and
    remote access state, copy progress, update state, shop catalog, the banner, "Copied" and "Saved" messages) is
    read out at once through a UI Automation notification, without moving the focus.
  - Keyboard: access keys (Alt + the underlined letter) for the pages, Save, Revert, Language and each page's main
    buttons and choices, unique among the controls shown together, in English and in French; Ctrl+S saves; Esc
    closes every dialog (and answers No to a question); a focus ring is drawn around the focused control at 3:1 or
    more against the background.
  - Contrast: secondary text (the Windows 11 placeholder colour, 3.9:1) is darkened or lightened to 4.5:1, card
    borders to 3:1, the hover and pressed fills of primary buttons keep 4.5:1 against their label with any Windows
    accent colour.
  - Windows high contrast mode: the window switches to the classic style and the system colours, and drops its own
    colours (status pills, badges, primary buttons), also when the mode is turned on while it is open.
  - Large text and display scale: long check box and radio button labels wrap instead of widening the page, spin
    boxes fit their longest value, code boxes follow the text size, and the window opens wide enough for its widest
    page (within the screen).

## [3.6.0] - 2026-11-28

### Added

- `p6_use_database` (Full profile, app settings kind: dry run, then your confirmation; never offered to remote
  clients): switches the data source of this server process to a database, the XER files, a P6 EPPM working copy or
  a SQL Server / Oracle copy already set up and confirmed in Settings. Without arguments it lists them (what each
  points to, confirmed, local copy, switchable or why not, active now, saved choice) and changes nothing;
  `source="saved"` returns to the saved choice. The switch is layered over config.json in memory and never saved:
  config.json and Settings keep their choice, and a restart returns to it. Refused: unconfirmed profiles, missing
  files, P6 EPPM and SQL Server / Oracle profiles without a local copy, and any switch while `P6_DB_PATH` is set.
  The tool list does not change: tools the new source does not allow (writes on XER files or a SQL Server / Oracle
  copy) are refused when called. Caches, backups and the edit log follow the new database file. In HTTP mode the
  switch applies to every later request of every client of that server. `p6_diagnose` gains `data_source` (active,
  saved, switched, since, transport), and `restart_required` now compares with the saved settings.
- P6 Professional on SQL Server or Oracle (read only): a fourth data source in Settings (Database), "A P6
  Professional database on SQL Server or Oracle". A profile holds the database type, server, port, database or
  service name, schema (owner), database login (its password in the Windows Credential Manager, stored once a test
  or a copy shows it works), an encryption option (SQL Server encryption required, Oracle over TCPS), the projects
  to copy (Project IDs, or every project of an EPS, templates left out) and the baselines (each project's baseline,
  all of them, or none). Test connection reads the P6 tables with a query that returns no row, the P6 version
  recorded in the database (PREFER) and the number of projects, and checks the named projects and EPS. The copy
  (Copy now in Settings, or the new tool `p6_pmdb_refresh`) reads the projects, their baselines and the tables the
  read tools use (calendars, activity codes, UDFs, resources and roles, notebooks, steps, expenses, risks, schedule
  options, financial periods) into a local SQLite file under `%LOCALAPPDATA%\P6MCP\pmdb`, with SELECT statements
  only (on Oracle inside a read-only transaction, so every table comes from the same point in time); a failed copy
  keeps the previous one. Every read tool runs on the copy unchanged; `p6_diagnose` (`pmdb_mode`, no password) and
  the `schedule_state` of the tools give `copied_at` and its age. Every write tool is refused with a message that
  says the database server is never written, and writes cannot be turned on in this mode. The SQL layer and the
  table copy are the P6 EPPM SQL route's (drivers installed on demand from PyPI). Not yet tested against a real
  SQL Server or Oracle P6 database: the tests run on simulated servers that speak each dialect (parameter style,
  TOP / FETCH FIRST, upper case Oracle names, schema prefix, dates, decimals, LOBs, NVARCHAR text, Oracle's
  1000-item IN lists) over the synthetic demo database, and check that every read tool of a review gives the same
  answer on the copy as on the database itself.
- `p6_update_activity_codes`: assigns, replaces or clears activity code values of many activities in one
  all-or-nothing change (dry run, then your confirmation). Item `{task_code, code_type, code_value}` or
  `{task_code, code_type, clear: true}`; the code type is the one the project sees (its project codes, then its EPS
  codes, then global codes, case ignored), the value its short name, else its path (`CON.MEC`), else its name
  (several matches are refused with their paths). P6 keeps one value per type and activity (TASKACTV primary key):
  a new value replaces the old one, a soft-deleted row is revived, a clear is a hard delete as in P6. Codes move no
  date: no project is marked stale, no F9 needed. One edit log entry per value.
- `p6_move_activities`: moves activities to another WBS node of their project in one all-or-nothing change. `wbs`
  = the WBS code as `p6_list_wbs` shows it (with or without the Project ID), else a WBS name unique in the project;
  the project node itself is allowed (P6 keeps activities there), a WBS of another project is refused. Dates
  depend on the WBS only through WBS summary activities: the project is marked stale (F9) only when one spans the
  old or the new WBS (`schedule_effect`, `wbs_summaries` per item); P6's summary tables are reported in
  `summaries_stale` (Tools > Summarize). One edit log entry per activity.
- Both are write tools (Full profile, writes on, two-step confirmation; remote clients only with remote edits on
  and a grant with edits). They are refused on a P6 EPPM working copy: the push does not send activity code
  values or WBS changes yet.
- `p6_add_activities`: creates activities in a project, with their relationships, in one all-or-nothing change
  (two-step write, at most 500 items). Item: Activity ID (unique in the project, length limit of the admin
  preferences), name, WBS (code or unique name; default the project node), type (task, resource dependent, start or
  finish milestone, level of effort, WBS summary), original duration in days of the activity calendar (task and
  resource dependent default to the admin preference; none for milestones, level of effort and WBS summary),
  calendar (global or project calendar by name; default the project calendar), constraint and date as
  `p6_update_activity` takes them, predecessors and successors (Activity IDs of the project, of the same call or of
  another project, with type and lag). New activities are Not Started with remaining = original duration and the
  project's default duration and % complete types; their planned, early and remaining dates sit at the data date
  until F9 (late dates and float empty). Refused: an Activity ID already used or given twice, an unknown WBS or
  calendar, a relationship P6 would refuse, a logic loop the new relationships close. One edit log entry per
  activity and per relationship; the project is marked stale (F9). Not available on a P6 EPPM working copy (the
  push does not create activities); offered to remote clients with edits allowed. Checked by an XER export and
  re-import, the CPM engine and a delete; not yet confirmed by opening the edited database in P6 itself: try it on a
  copy first.
- Fragnet what-if (time impact analysis): `p6_schedule_whatif` takes `add_activity` edits that insert new activities
  in memory only (a new Activity ID and name, the original duration in days of a calendar, the project calendar by
  default, task type TT_Task, TT_Mile or TT_FinMile, a WBS code, the project node by default, and an optional
  constraint), and `add_link` edits link them to each other or to existing activities. The result then adds a
  `fragnet` block: the fragnet activities with the engine's early and late dates, total and free float, critical
  and longest path flags, links and what drives them; the impact on the project finish and on each milestone (the
  ones given, else every open milestone) before and after, with the delay in calendar days and in working days of
  the project calendar; and the driving path from the fragnet into the project finish (the activities whose
  relationships set the dates). Fragnet activities count as new activities in the assumption flags (an activity
  left without a predecessor or successor is an open end) and in the DCMA checks run before and after the edits.
  Nothing is written to the database. Without `add_activity` edits the result is unchanged.
- `p6_schedule_risk` risk model: `correlations=` groups of activities (Activity IDs, a WBS code or an activity code
  value) with a coefficient from 0 to 1 make their drawn durations move together. Method: a Gaussian copula with one
  shared factor per group and iteration mixed with each activity's own draw, so each activity keeps its own range;
  the rank correlation of two members is close to the coefficient (6/pi asin(c/2): 0.48 for 0.5), 1 gives every
  member the same percentile of its range, 0 leaves the draws as they were. Limits: one group per activity (the
  first listed), no negative correlation, no correlation between groups or with the risk events.
- `p6_schedule_risk` risk events: `risks=` with an id, a name, a probability from 0 to 1 and an impact (days added
  to the remaining duration of Activity IDs, a WBS or an activity code value, a number or min / likely / max drawn
  once per firing; negative for an opportunity). Each event fires on its own in each iteration. `risk_events` ranks
  them: how often each fired, the mean finish delay in working days when it fired and when not, their difference
  (finish impact; with several events and wide ranges it carries sampling noise, read small values as none), the
  expected delay and its share. `use_p6_risks=True` lists the project's P6 risks (PROJRISK, activities from
  TASKRISK): P6 stores their probability and schedule impact as levels of its scoring matrix, so one is simulated
  when `risks=` gives its probability and days under its Risk ID (its assigned activities when the impact names
  none).
- `p6_schedule_risk` runs large simulations in worker processes: `workers=` (default min(4, CPUs - 1), 1 = none)
  when iterations x recomputed activities reach 200,000 (smaller runs would spend more starting the workers, about
  2 to 4 s, than they save). The output is the same for any worker count, and the same as 3.5.0 for the same seed
  when no correlation or event is given; `parallel` says how many workers ran. If the workers cannot start, every
  iteration runs in the server, with a note. On a project of 3,120 activities (1,278 recomputed per iteration),
  1,000 iterations took 18 to 27 s with 4 workers, 29 to 40 s with 2 and 36 to 68 s in one process (three rounds
  on a machine busy with other work).

### Changed

- P6 EPPM SQL pull: number columns of the working copy are typed (ids and counters INTEGER, hours, quantities,
  costs and percentages REAL). SQL Server gives decimal columns as Decimal, stored as whole numbers when they were
  whole, so a duration of 12 hours on an 8 hour calendar could have read as 1 day (integer division in SQLite).
  Oracle BLOB and CLOB columns (calendars, notebooks) are read from their LOB objects, and id lists longer than
  1000 items are split for Oracle.
- Settings (Database): the disabled "SQL Server (later version)" and "Oracle (later version)" types of a database
  entry are gone: these databases are the new data source.
- `p6_schedule_whatif`: an `add_link` edit that would close a loop is refused at that edit, with the loop it closes
  (`A -> B -> C -> A`), instead of the engine's loop error after every edit.

- The core skills shipped with the app (installed on first start, or when the skill shop cannot be reached) follow
  the skill shop at 3b082c3: two new skills, `trend-report` (`p6_trend`) and `resource-loading-review`
  (`p6_resource_loading`), cost figures in `progress-earned-value` and `weekly-update-review`, and smaller activity
  lists (`fields=`) in `float-near-critical`, `lookahead-brief` and `weekly-update-review`. 17 core skills.
- Server instructions: new activities come from `p6_add_activities` or `p6_import_xer`.
- Faster repeated engine calls: the server keeps the loaded schedule network of the last 4 projects (the CPM
  engine's network, its run, the DCMA critical path test and the link network) while the database file is
  unchanged, so a second `p6_health_check`, `p6_cpm_validate`, `p6_schedule_whatif`, `p6_schedule_risk`,
  `p6_export_dashboard`, `p6_trend`, `p6_driving_path`, `p6_float_origin`, `p6_redundant_links`, `p6_dangling`,
  `p6_milestones` or `p6_update_integrity` on the same project no longer reloads and reruns it (on a project of
  3,120 activities, a second health check takes about 0.7 s instead of 2.4 s). Results are unchanged. Any commit
  to the file (P6, this server or another program, rollback-journal or WAL mode) or a change to the project's
  activities, links, assignments, WBS, calendars, resources or schedule options makes the next call read the
  database again; every write of this server, a restore and an EPPM pull or push also empty the cache.
  `p6_diagnose` reports it (`engine_cache`: on or off, projects kept, hits, misses); the environment variable
  `P6MCP_ENGINE_CACHE=0` turns it off.

## [3.5.0] - 2026-11-27

### Added

- `p6_resource_loading`: time-phased units per resource and month or week (labor and nonlabor in hours; material in
  its own unit, listed after them), with budget on the planned dates, actual up to the data date and remaining from
  the remaining dates, against what the resource can give in the period: working hours of its calendar x its max
  units per hour by effective date (RSRCRATE). Rows flag over-allocated periods (actual + remaining above the
  limit); `resources` gives each resource's totals (the quantities of `p6_resource_summary`), peak period and
  over-allocated periods; `counts` the over-allocated periods, the resources over and the resources without a
  limit. `resource=`, `role=`, `from_date` / `to_date`. Read only, also for remote clients.
- Cost earned value at the data date: `p6_project_summary` returns a `cost` block beside the units figures, and
  `p6_progress_curve(measure='cost')` the same block as `to_date.cost`. It follows P6's earned value fields: BAC
  (budgeted cost of every assignment, labor, nonlabor and material, plus expenses), PV (the baseline's planned cost
  at the data date), EV (budgeted cost x the earned % of each activity under its WBS earned value technique), AC
  (actual regular and overtime cost plus actual expenses), CV, SV, CPI, SPI, ETC, EAC, VAC and TCPI, with a
  breakdown by source (labor, nonlabor, material, other, expenses). EAC follows the project's technique for
  computing estimate to complete (its WBS node, else the admin preferences: remaining cost, PF = 1, PF = 1 / CPI,
  PF = 1 / (CPI x SPI) or a custom PF), and every variant is given in `eac_variants`. Without a baseline PV, SV and
  SPI are null; without costs the block says so. Units figures are unchanged.
- `p6_trend`: a trend across the schedule updates of one job, one row per update ordered by data date. Give the
  Project IDs of the updates (`projects=`), or an EPS (`eps=`: every project of that EPS and of the EPS below it,
  baselines and templates left out). Per update: data date and last F9, scheduled finish and must finish date,
  the finish against the must finish date, the first update, the previous update and the baseline in working days
  of the project calendar, units % complete, baseline planned % and SPI (units) as `p6_project_summary`, critical,
  near critical and negative float counts, total float statistics of open activities, the DCMA weighted score with
  its failed checks and BEI as `p6_health_check` (no snapshot saved; `dcma=False` skips it), the milestones present
  in every update (or `codes=`) with their date, slip since the previous update and variance since the first, and
  what changed since the previous update: activities and links added and removed, durations cut (as
  `p6_compare_projects` and `p6_update_integrity` count them). Read only, every update from its own read snapshot.
- `p6_export_dashboard(trend=[...])`: the Project IDs of the earlier updates add a **Trend** sheet (linked from the
  sidebar) with the per-update table, a chart of the forecast finish and must finish per data date, a chart of
  units % complete, baseline planned % and SPI per data date, and the milestones by update; the Reports sheet
  explains it. Without `trend=` the workbook is the same as before.

### Changed

- The server is split into modules: `p6mcp/server.py` (15,456 lines) becomes a facade over `p6mcp/registry.py`,
  `p6mcp/core/` (state, database access, calendars, schedule options, CPM engine, analysis, reporting, XER,
  Excel workbooks...) and `p6mcp/tools/` (one module per tool family). No change in behaviour: the same tools, in
  the same order, with the same schemas and results; `p6mcp.server` still exposes every name.
- `p6_progress_curve` periods are spread over working time: each assignment over its calendar (the resource's
  calendar for a resource dependent activity, else the activity's), on the assignment's resource curve when it has
  one (P6's 21 point curves of RSRCCURV), and with the stored period actuals (TRSRCFIN) placed in their financial
  periods when P6 stored them. Values at the data date do not change (`to_date`, `measure`, `earned_points`,
  `p6_project_summary`, the dashboard cards): `to_date` keeps the planned value of the planned dates linear over
  calendar time, as before. On two real projects the cumulative curve moves by at most 0.8 point of the budget
  (monthly at most 0.5). `checks.spread` counts the spans spread over calendar time (no working time on their
  calendar), the curves applied or not found and the assignments with period actuals; the note says what is still
  approximate. The dashboard S-curve follows.
- Tests run in parallel on 4 workers (pytest-xdist, `pytest -n 4`) in CI and in the release build: 60 to 95 s
  instead of 165 to 205 s serial on a 12 core machine. Tests that start a server take an OS-chosen port per worker,
  the Run at startup test uses its own registry key per process and the tray tests their own single instance
  pipe. Nothing user visible.

## [3.4.0] - 2026-11-27

### Added

- P6 EPPM proxy: the calls to a P6 EPPM server go through the proxy set in Windows (or the `HTTP_PROXY` and
  `HTTPS_PROXY` variables) with its bypass list, automatically. A server's settings can instead say **No proxy** or
  **This proxy** (an `http://host:port` address for that server only), with an optional proxy user whose password is
  kept in the Windows Credential Manager (Basic proxy sign-in). A server on this PC is never proxied, the SQL account
  always connects directly, and a proxy address with a user or a password in it is refused. Test connection uses
  the setting and says which proxy it went through; `p6_diagnose` reports it (never a password). A Windows proxy
  setup script (PAC) is not run: it is reported, and the proxy can be entered in the server's settings.
- `p6_eppm_push(schedule=True)`: once every call of the push has succeeded, the pushed projects are scheduled on
  the server with P6's own scheduler (optionally with `new_data_date`) and, with `pull_after` (default), pulled
  again. The dry run lists the schedule after the push, and one confirmation covers both steps (the text becomes
  `PUSH TO <server> AND SCHEDULE`); a push that fails or meets a conflict schedules nothing. The result and the edit
  log hold both steps.
- `p6_export_xer` and `p6_import_xer` carry activity notebooks (TASKMEMO, topics in MEMOTYPE), steps (TASKPROC),
  expenses (PROJCOST, categories in COSTTYPE), roles (ROLES, ROLERATE, and the role of each resource assignment)
  and risks (RISKTYPE, PROJRISK, TASKRISK). A project exported then imported keeps them. The import matches topics,
  expense categories, roles (by Role ID under the same parent role) and risk types by name and creates the missing
  ones (role prices only for new roles); in update mode notebooks are matched by activity and topic, steps and
  expenses by activity and name, risks by Risk ID, and the ones missing from the XER are kept. A table the database
  lacks is skipped with a warning (`p6_inspect_xer`: `tables_not_in_database`). The place of these tables in the
  file follows P6's table order rule; no P6 export containing them was available to confirm it.
- The EPPM SQL pull also copies steps, expenses, expense categories and role prices.
- `detail="brief"` on `p6_query_activities`, `p6_critical_path`, `p6_lookahead`, `p6_float_paths`, `p6_milestones`
  and `p6_get_activity`: rows without their empty (null) values, and in `p6_get_activity` links without
  `direction`, `lag_h` (`lag_d` stays) and `other_project` when it is the activity's own project. On a
  3,120-activity project a 200-row page is 17 to 19 % smaller (activity lists), 16 % for milestones and one activity
  32 %. `fields=` works with it. The default `detail="full"` keeps every key of every row, as before.
- `explain=True` on `p6_update_integrity` and `p6_compare_projects` returns the fixed texts described below, so
  the result is the same as in 3.3.
- `p6_calendar_check`: checks the calendars a project uses (project calendar, activity calendars, calendars of
  the assigned resources and their base calendars) over the years from the data date to the project finish.
  Findings, one row each with the calendar, date or year, a detail and the number of activities on that calendar:
  a holiday of the project calendar that another calendar works and the reverse, years without any exception
  date, hours per day or per week that disagree with the work week, work weeks other than the project calendar's,
  unreadable or missing calendars and base calendars, calendars without working time, and open resource dependent
  activities whose resources have no usable calendar. Read only, paged, with counts per kind and per severity.
- `p6_export_lookahead`: the `p6_lookahead` window as a printable Excel sheet (same activities, categories,
  critical flag and filters): activities grouped by WBS in P6 order with Activity ID, name, start, finish (actual
  dates marked A), remaining duration, total float, critical and status, then a day or week grid with a bar per
  activity (critical in red, completed in gray, milestones as a diamond), non-work days of the project calendar
  shaded and the data date marked. Landscape A3 fit to width, header rows repeated on every page, panes frozen.
  Writes a file, like `p6_export_dashboard`: Full profile only, never offered to remote clients.

### Changed

- Fixed texts that do not depend on the project are no longer sent with every result (visible to anyone who reads
  these keys): `p6_update_integrity` no longer returns `definition` in each flag (and in `result` with `flag=`),
  `p6_compare_projects` no longer returns `summary.activities.float_definition`, and `schedule_state.note` is null
  unless it says something about this project (changes made after the last F9, or no F9 date recorded). The words
  are in the tool descriptions and come back with `explain=True` where the tool has it. `p6_update_integrity` is
  14 % smaller (2 KB), and every result carrying `schedule_state` loses about 120 characters. `p6_cpm_validate`
  `rules` and `p6_milestones` `float_origin_explanation` stay: skills read them.
- Faster engine and report tools: stored dates are read once per distinct value instead of once per use. On a
  3,120-activity project, `p6_cpm_validate` takes 0.54 s instead of 0.98 s, `p6_health_check` 1.11 s instead of
  1.57 s, `p6_schedule_whatif` 2.65 s instead of 3.74 s, `p6_project_summary` 0.11 s instead of 0.31 s. Results
  are unchanged.
- `p6_export_dashboard` about twice as fast (3.9 s instead of 7.1 s on the same project): cell styles are shared
  instead of created for every cell. The workbook is unchanged.
- Tool results reach the AI app as compact JSON instead of indented JSON: 18 to 34 % fewer characters for the same
  content, so long answers use less of the conversation. Results are text only (never a second structured copy).
- The tool list the AI app reads at the start of a chat is 11 % smaller (132 KB instead of 149 KB with every tool
  on): schema titles that repeated each parameter's name are left out, and so is the deprecated `confirm`
  parameter, which is still accepted.
- `p6_schedule_whatif` about 45 % faster (1.6 s instead of 2.9 s on a 3,120-activity project): its DCMA critical
  path test reuses the networks and engine runs the what-if already has (the stored database before, the edited
  network after) instead of loading and running the engine again, and the test's delayed run skips the free float
  and float paths it never reads. Results, `p6_health_check` included, are unchanged.
- `p6_import_xer` in update mode about 2.5 times as fast (a dry run takes 1.05 s instead of 2.7 s for a
  3,120-activity update): the existing rows are matched from keys read once per table instead of one query per row,
  and the updates are sent in batches. Counts, rows, soft-deleted rows coming back, the edit log and the result are
  unchanged.
- Earned value (`p6_progress_curve` to date and earned points, `p6_project_summary` `spi_units`) applies each WBS
  node's own technique: activity percent complete, 0/100, 50/50, custom percent complete, WBS milestones percent
  complete and activity percent complete using resource curves (a node without one takes the project node's, then
  the admin default). Before, every node counted as activity percent complete. The rules follow the P6
  documentation; no P6 earned value figure was available to check them. `checks` adds `ev_techniques` (WBS nodes
  per technique) and `ev_unknown_techniques`. Projects whose WBS all use activity percent complete get the same
  values as before.
- `p6_update_assignment` and `p6_update_assignments` say which P6 summary rows (SUMTASK, SUMTASKSPREAD,
  SUMTRSRC) the change leaves out of date (`summaries_stale`, plus a plan line) and that Tools > Summarize in P6
  refreshes them. The summary tables are still never written: they are time-phased spreads that could not be
  rebuilt to match P6.

### Fixed

- Deleting an activity or a project now also deletes the UDF values of its steps, expenses and risks,
  which were left without their row before.
- Remote access, Connect with this PC: the notification area app could reset the browser's connection instead of
  answering when it refused a request (it closed the connection before reading the request body). It now reads
  the body first, and stops waiting for a body after 10 s.
- After an automatic update the notification area app did not start again, so remote access stayed off until the
  next Windows sign-in: setup starts it while it still holds its mutex, and the app exited at once. Setup now starts
  it with `--after-setup`, which waits for setup to end (also for "Open Settings" on the last setup page).
- The Windows startup entry is put back when the app starts, if the settings ask for one and it is missing or names
  another command (removed by another program, a moved install). It is never removed there: only Save does.

## [3.3.0] - 2026-11-26

### Added

- Remote access: a redesigned connection page (brand colors, light and dark, phone width) with a Connect with this
  PC button. In the browser that started the connection it sends the code to the notification area app on the
  same PC (`127.0.0.1`, setting `remote_pair_port`, default 39219), which fills it in the approval window: the user
  only clicks Approve there. The code with a Copy button stays as the second way. The button is only offered to the
  starting browser (a key kept in a cookie) and the app accepts only pages of the user's own connector address, so
  a request started by someone else still cannot be approved from this PC by a click.
- The approval window no longer pops up over the browser page when a request arrives: Connect opens it with the code
  filled in, and the tray notification or the new tray menu entry Approve a connection opens it to type the code.

## [3.2.0] - 2026-11-26

### Added

- Remote access (beta, off by default): claude.ai and ChatGPT in the browser (and Claude on the phone) use MCP for
  Primavera P6 on this PC through the user's own Cloudflare account, free plan, no domain needed. Settings,
  Remote access: a Cloudflare setup from one API token used once and never saved (a relay Worker on the account's
  workers.dev address, a Workers VPC link and a tunnel this PC opens; or a host name on the user's own domain),
  Repair, Remove from Cloudflare, the connector address to paste in the AI app, Turn on remote access, Allow edits
  from remote clients, Keep this PC awake, Connected apps with End connection. Tested on 2026-11-26 with claude.ai
  and ChatGPT Free, reads and confirmed edits.
- A remote access server (`p6mcp-server --remote`, `127.0.0.1` only, run by the notification area app with the
  bundled `cloudflared`) with its own OAuth 2.1 sign-in: client metadata documents or dynamic registration, PKCE,
  tokens bound to the connector address (1 hour, renewed for 30 days of use, a reused refresh token ends the
  connection). Each connection is approved on the PC by typing the code the browser shows.
- Remote rules: read tools; edits only with the remote edit switch, Allow write operations and a connection
  approved with edits, still with the dry run and the confirmation in the chat; never delete, restore, file
  import or export, or the skill shop; local paths shown as file names; password columns empty in
  `p6_sql_select`; results up to 100,000 characters; the confirmation token of a dry run works only for the
  connection that asked for it.
- Audit log of remote access (`%LOCALAPPDATA%\P6MCP\remote\audit_*.jsonl`, 90 days, no values, tokens or query
  strings), the app named in the edit log for remote edits, request rate limits, optional IP allowlist
  (Anthropic's and OpenAI's published addresses).
- `p6mcp-server remote pending | approve CODE [--edits] | deny | connections | revoke | revoke-all | forget`
  (the uninstaller runs `forget`: secrets and connections of this PC removed).

### Changed

- The installer includes `cloudflared` 2026.11.2 (Cloudflare's tunnel connector, Apache 2.0, unchanged, pinned by
  its published SHA256): about 55 MB more on disk.

## [3.1.1] - 2026-11-25

### Fixed

- P6 EPPM sign-in on a locked account: P6 EPPM locks an account after 5 failed sign-ins in a row, and its REST
  sign-in then answers HTTP 500 "Request failed" even with the right password. The message now says the account
  is probably locked and that a P6 administrator must unlock it. When the server has a read-only SQL account, the
  user's count of failed sign-ins (`USERS.failed_login_attempts_cnt`) confirms the lock or rules it out.
- Settings (Database, P6 EPPM server) warns that each failed connection test counts toward P6's lockout.

### Changed

- Build workflows use `actions/checkout@v7`, `actions/setup-python@v7`, `actions/upload-artifact@v7` and
  `azure/login@v3` (Node 24; the Node 20 versions were deprecated).

## [3.1.0] - 2026-11-25

### Added

- `p6_update_activities`: many activity edits in one change, all or nothing, for a progress update (actual dates,
  remaining durations, physical %, names, constraints): one dry run and one confirmation instead of one per
  activity. Each item follows the rules of `p6_update_activity`; one edit log entry per activity. Works on a P6
  Professional database and on a P6 EPPM working copy (the edits become pending changes).
- `p6_eppm_discard`: drops pending changes of a P6 EPPM working copy one by one, by the id `p6_eppm_status` now
  shows for each change (and a push dry run gives with each conflict), or all of them. Each object gets back the
  values it had at the pull; the other changes stay pending, and the server is not touched. Before, the only way
  out of a conflict was a new pull that dropped every local change.

### Changed

- Setup writes a log of every install in `%TEMP%` (`Setup Log <date> #<n>.txt`), as automatic updates already did
  in `%LOCALAPPDATA%\P6MCP\updates\setup.log`: a 3.0.0 install that replaced no file left nothing to read.

## [3.0.0] - 2026-11-25

### Added

- **P6 EPPM servers.** A third data source next to the P6 Professional database and XER files: a live P6 EPPM
  server, on premises or on Oracle Cloud. In the Settings app, Database page, choose "A P6 EPPM server", add its
  address, the database name of P6's sign-in page, your P6 user and password, and click Test connection (a
  session sign-in on premises, an OAuth token on Oracle Cloud; it counts the projects you can read). A password
  is kept in the Windows Credential Manager only once a test shows it works. An optional read-only SQL account
  (SQL Server or Oracle) reads every table as stored; its driver (pymssql or oracledb) is downloaded from PyPI
  only when you click Install the SQL driver, and each file is checked against the SHA-256 PyPI publishes.
- `p6_eppm_projects` lists the projects your P6 user can read. `p6_eppm_pull` copies the projects you name, with
  their current baseline, into a local working copy that every other tool reads (by SQL, or by REST alone: the
  project from P6's XER export, the baseline from REST objects since P6 EPPM does not export a baseline, the
  schedule options from REST). `route` chooses sql, rest or auto.
- Edits in P6 EPPM mode change only the working copy: activity fields, relationships and assignment units, each
  with its dry run and your confirmation. `p6_eppm_status` lists the pending changes, netted per object and
  field, and counts the activities changed on the server since the pull. A pull refuses to drop pending changes
  unless `discard_pending=True`.
- `p6_eppm_push` sends the pending changes. It reads every changed object on the server and refuses on any
  change made there since the pull (the server has no lock of its own), then, after your yes and the
  confirmation text `PUSH TO <server>`, sends one object per call in a fixed order and stops at the first
  failure. A journal records each call; what reached the server is no longer pending, so a new push sends the
  rest. New relationships take the server's ObjectId.
- `p6_eppm_schedule` runs P6 EPPM's own scheduler (optionally with a new data date), waits for the job, returns
  P6's scheduling log with a short summary and pulls the projects again. It never runs by itself after a push.
- In P6 EPPM mode, the instructions sent to MCP clients add one line: use `p6_eppm_push` and `p6_eppm_schedule`
  where a tool or skill says to press F9 in P6.

### Changed

- Config schema 4 (`data_source: eppm`, `eppm_profiles`, `eppm_active`); an older version refuses the file
  rather than misread it. Changing the data source now asks for a restart of the MCP client, since the
  instructions depend on it.
- Setup stops the MCP for Primavera P6 servers your MCP clients started from the installation folder before it
  replaces the files (an update with Claude Desktop open stopped with "DeleteFile failed; code 5"), and closes
  any other program holding a file. Restart your MCP clients after an update, as before.
- Setup no longer shows the large logo at the top right of its pages.
- `p6_eppm_status` no longer reports your own pushes as changes made by someone else on the server: they are
  counted apart in `pushed_by_you`. `changed_on_server.since` is now the pull time on this PC's clock, and
  `since_server_clock` the server time the comparison starts from: the latest change of the project or of one
  of its activities at the pull (an activity edit does not change the project's date, so an activity changed
  before the pull was counted as changed since).
- In P6 EPPM mode, the server instructions no longer speak of P6 Professional (standalone SQLite, "P6 must be
  closed", new activities through an XER import), `p6_diagnose` names the schema "P6 EPPM working copy"
  instead of an unknown, unwritable P6 schema, a backup restore no longer asks to close P6, and the
  update-import-safely command is not offered (the working copy refuses `p6_import_xer`).

### Not in this version

- Through a P6 EPPM working copy: deleting activities, UDF edits (their REST calls are not checked yet), schedule
  options, EPS and other structure changes. Do them in P6 EPPM, then pull again.

## [2.2.0] - 2026-11-19

### Added

- `p6_cpm_validate` explains every difference. Each row has a `cause_class`: `stale_f9` (the activity, one of its
  links, its assignments or calendar, a linked activity of another project, the scheduling options, deleted rows
  or the server's own writes changed after the last F9: the stored dates are out of date, not wrong), `data`
  (invalid input: actual dates after the data date or out of order, status against actual dates, out of sequence
  progress, a missing or unreadable calendar, an unreadable date, a constraint without a date, no F9 recorded),
  `engine` (same inputs, another result, with the rule named) or `unknown` (never guessed). Rows also carry
  `evidence`, `inherited_from` (the neighbour whose difference they only inherit) and `context`: the activity,
  its calendar with the non-work days around the differing date, its constraints and actual dates, and its links
  with type, lag, lag calendar and which link drives the stored date and the engine's. New result keys:
  `cause_counts`, `project_context` (data date, last F9, scheduling options in force), `stale_f9` and
  `stale_f9_detail` (what changed after the F9, measured from the end of an XER import when the project came in
  by import). `match_rate` and the 0.99 trust threshold are unchanged. A page ends early past about 150 KB.
- `p6_schedule_whatif` adds `assumptions`, flags only (the run is never refused): duration cuts above
  `max_cut_pct` (default 30) of the original or remaining duration, open or dangling ends left by deleted or
  retyped links, new hard constraints, negative lags, SS links with lag 0, activities that became near critical
  (`near_critical_days`, default 10), the paths that converge on the project finish, and the DCMA checks before
  and after the edits, run in memory with no snapshot (`dcma=False` skips them). A fixed `caveat` line says the
  dates are calculated the way P6 would, not checked against crews, materials or access.

## [2.1.1] - 2026-11-13

### Fixed

- The clients' skills folders (Claude Code, Cursor, VS Code, Cline) now follow the saved skills: Save in the
  Settings app rewrites the files that differ after a skill is turned on or off, installed or updated from the
  Shop page, edited, restored, or the tool settings change. Before, only the first start, the automatic shop
  update and Update skills on the Clients page wrote them, so the 2.0 core skills (logic-date-integrity,
  schedule-risk, update-integrity-audit) could be missing from those clients. Each start of the Settings app
  also brings folders left behind in line.
- XER-only mode offers no write skill in the clients' folders, the Clients page and `p6_shop_install`, as the
  server and the Skills page already did (XER-only mode never writes).

## [2.1.0] - 2026-11-12

### Changed

- The CPM engine behind `p6_cpm_validate`, `p6_schedule_whatif`, `p6_schedule_risk` and the DCMA critical path
  test now follows the rules P6 25.12 applies, measured on 107 parity cases (engine version 23, was 1): level of
  effort and WBS summary activities, as late as possible, suspend and resume dates, resource dependent
  activities on their resources' calendars, assignment lag, the four out-of-sequence modes, constraint pairs and
  constraints on started activities, the project planned start, open ends, ignored links to other projects, a
  calendar without a work week (Monday to Friday 08:00 to 16:00). It also computes free float, the longest path,
  multiple float paths and the remaining and planned dates F9 writes.
- `p6_cpm_validate` compares free float, the longest path flag and float paths with the last F9
  (`other_fields`); `p6_schedule_whatif` uses the engine's longest path on longest-path projects and reports
  the activities that join or leave it (`longest_path_change`).
- `p6_driving_path`, `p6_float_origin` and `p6_redundant_links`: late gaps follow the backward pass (no false
  gaps when lags use the project calendar), out-of-sequence options and resource calendars apply, a new root
  driver `as_late_as_possible`, and the lag from a progressed predecessor follows P6.

### Fixed

- `p6_set_schedule_options` and `p6_import_xer`: the float type codes are P6's (`FT_FF`, `FT_SS`, `FT_Min`),
  and retained logic with progress override (both Y, a P6 state) is accepted.

## [2.0.0] - 2026-10-29

Version 1.10.0 was never released: its changes ship in 2.0.0.

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

- P6 MCP is now **MCP for Primavera P6**: Settings window, notification area, installer, Start menu shortcuts
  (the 1.x shortcuts are removed on update), messages, MCP bundle and the new wordmark. The install folder
  (`%LOCALAPPDATA%\Programs\P6 MCP`), the program and file names (`p6mcp-server.exe`, `P6MCP-Setup-*.exe`), the
  settings and data folders and the `p6mcp` entry of the MCP clients do not change, so updates and client
  registrations keep working.
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
- Settings, **Skills** and **Skill shop** pages: the skills are cards (as many columns as the width allows) or
  list rows aligned in columns, chosen with the Grid and List buttons and kept per page for the next launch (in
  `%LOCALAPPDATA%\P6MCP\settings_ui.json`, not a setting: no Save needed). The Skills page searches the ID,
  title and description and filters by source; a card holds the on / off checkbox (applied on Save, as before),
  the Source and Offered badges, Remove and a menu (View details, Edit, Copy to my skills, Export, Share; the
  Share help is its tooltip). Shop cards show the author, version and state with Install or Update, Remove and
  View details; Install reads the skill first when needed. Both pages share a details dialog (resizable, size
  kept) with the tools the skill calls, the write tools warning and the whole SKILL.md, which replaces the
  SKILL.md panel of the Skills page and the Skill panel of the shop page. Empty states, Retry when the shop
  cannot be reached and a busy bar while it is read. Keyboard: Tab reaches the current card, the arrow keys
  move between cards, Space turns a skill on or off, Enter opens the details, Delete removes (after the
  question), the Menu key opens the menu. Restore core skills is on both toolbars. The actions themselves do
  not change.

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

[3.6.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.6.0
[3.5.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.5.0
[3.4.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.4.0
[3.3.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.3.0
[3.2.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.2.0
[3.1.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.1.1
[3.1.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.1.0
[3.0.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v3.0.0
[2.2.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v2.2.0
[2.1.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v2.1.1
[2.1.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v2.1.0
[2.0.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v2.0.0
[1.9.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.9.0
[1.8.2]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.8.2
[1.8.1]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.8.1
[1.8.0]: https://github.com/nizarhamza/p6-mcp/releases/tag/v1.8.0
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
