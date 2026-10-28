# Accessibility

P6 MCP should be usable by everyone who plans and schedules with P6. This page says what is supported
today, what is not, and how to report a barrier.

## Scope

- **Settings and tray app** (P6 MCP Settings): the only window of P6 MCP.
- **Installer**: a standard Windows setup wizard (Inno Setup).
- **Excel dashboard** written by `p6_export_dashboard`.

The conversation itself happens in your MCP client (Claude Desktop, Cursor, VS Code...): its
accessibility is that client's.

## What is supported

- The Settings window uses standard Qt widgets, which Windows assistive technologies (Narrator, NVDA)
  reach through UI Automation. The window works with the keyboard: Tab moves between controls and the
  arrow keys move through the page list.
- High DPI displays: the window follows the Windows display scale, including fractional scales such as
  125 % and 150 %, and its layouts are sized from the font, not in fixed pixels.
- The window uses the Windows palette that Qt provides; on a dark palette the logo switches to its light
  version.
- Languages: English and French, or the Windows display language. The layout is ready for right-to-left
  languages.
- The tray icon never gives its state by color alone: its tooltip and menu say whether the server is
  running, starting, stopped or in error.
- Excel dashboard: values are written as cell values, never as images. Statuses (Critical, Delayed,
  At risk, On track, Completed) are written as words beside their colors, the finish variance bars show
  their value in working days, and their color key names each color.

## Known limitations

- The Settings window has not yet been tested end to end with a screen reader.
- There are no keyboard shortcuts (access keys) for buttons and pages.
- The status and float colors of the Excel dashboard are fixed; only its primary and accent colors can
  be chosen at export.
- The dashboard sheet is laid out for printing on A3; on a small screen it needs zooming.

## Report a barrier

Open a **Bug report** issue whose title starts with `Accessibility:`, and say which part (Settings,
installer, dashboard), what you use (screen reader, magnifier, keyboard only, display scale) and what
blocks you. Do not attach real project data.
