# Accessibility

MCP for Primavera P6 should be usable by everyone who plans and schedules with P6. This page says what is supported
today, what is not, and how to report a barrier.

## Scope

- **Settings and tray app** (MCP for Primavera P6 Settings): the only window of MCP for Primavera P6.
- **Installer**: a standard Windows setup wizard (Inno Setup).
- **Excel dashboard** written by `p6_export_dashboard`.

The conversation itself happens in your MCP client (Claude Desktop, Cursor, VS Code...): its
accessibility is that client's.

## What is supported

### Settings window and screen readers

- The Settings window uses standard Qt widgets, which Windows assistive technologies (Narrator, NVDA) reach through
  UI Automation. Every button, check box, radio button, field, list, table, card and the page list has a name: a
  field is named by its label (Alt + the label's underlined letter also moves to it), a list or table by the box it
  sits in, an icon button by its tooltip.
- Texts that change (a test result, the port check, the server and remote access state, a copy in progress, the
  update state, the skill shop catalog, the banner at the top, "Copied" and "Saved" messages) are read out when
  they change, through a UI Automation notification: the focus stays where it is. Errors are read at once, other
  changes after what the screen reader is saying.
- Skills and Skill shop pages: the cards (or list rows) are one Tab stop, the arrow keys move between them
  (Home and End go to the first and the last), Space turns a skill on or off, Enter opens its details, Delete
  removes it after a question and the Menu key (or Shift+F10) opens its other actions. Each card gives
  screen readers its ID, title, description, states and these keys; the icon buttons have a name and a
  tooltip, and the states are written on the badges, not given by their color alone.

### Keyboard

- Tab and Shift+Tab move through the controls of a page in reading order (top to bottom, then left to right; right
  to left in a right-to-left language); the arrow keys move through the page list.
- Access keys: Alt + the underlined letter (Windows underlines it once Alt is pressed, or always when "Underline
  access keys" is on in the Windows keyboard settings) opens a page, presses Save, Revert or one of a page's main
  buttons and choices, or moves to the Language list. A letter is used once among the controls shown together, in
  English and in French. Screen readers announce the key of a button, and of a page in the page list.
- Ctrl+S saves the settings. Esc closes every dialog and answers No to a question (No is also the default answer).
- The control that has the keyboard focus is circled by a ring in the Windows accent color (or the text color
  when the accent is too pale), at least 3:1 against the background; cards show their focus in the same color.
- Every action has a button or a key: nothing is reachable only by the mouse (double clicks and right clicks have
  a button or a key that does the same).

### Display

- Contrast (WCAG 2.1 AA): text at least 4.5:1 against its background, card borders and the focus ring at least
  3:1. Secondary text uses the Windows placeholder color, made darker (or lighter) where it reaches only 3.9:1;
  status pills and badges have fixed colors (5.7:1 to 8.2:1) and always say the state in words. The primary
  buttons (Install, Update) use the Windows accent color with a black or white label at 4.5:1 or more, also when
  hovered or pressed.
- Windows high contrast (contrast themes): the window switches to the classic Windows style and the colors of the
  contrast theme, and draws none of its own colors (pills and badges become framed text). Turning the mode on or
  off while the window is open redraws it.
- High DPI displays and large text: the window follows the Windows display scale, including fractional scales such
  as 125 % and 150 %, and the Windows text size; its layouts are sized from the font, not in fixed pixels. Long
  check box and radio button labels wrap onto more lines instead of widening the page, and the window opens wide
  enough for its widest page (within the screen).
- The window uses the Windows palette that Qt provides; on a dark palette the logo switches to its light
  version.
- Languages: English and French, or the Windows display language. The layout is ready for right-to-left
  languages.
- The tray icon never gives its state by color alone: its tooltip and menu say whether the server is
  running, starting, stopped or in error.

### Excel dashboard

- Values are written as cell values, never as images. Statuses (Critical, Delayed, At risk, On track, Completed)
  are written as words beside their colors, the finish variance bars show their value in working days, and their
  color key names each color.

## How it was checked

- Automated tests (`tests/test_accessibility.py`, run on every build) walk every page of the window, in English and
  in French and for each data source, and fail when an interactive control has no name, when the Tab order goes
  against the reading order, or when an access key is used twice among the controls shown together. They also
  check Ctrl+S, Esc in every dialog, the status announcements and the high contrast mode, and recompute the
  contrast ratios of the colors the window draws on the Windows 11 light and dark palettes and with pale accent
  colors.
- Screenshots of every page at 100 %, 150 % and 200 % display scale, at 225 % Windows text size (the largest),
  in French, on the dark palette and with a simulated contrast theme were looked at for cut text.
- Windows high contrast was simulated (the mode forced on, with a contrast theme's colors); the real mode, turned
  on in the Windows settings, has not been tried yet.

## Known limitations

- The Settings window has not yet been tested end to end with a real screen reader (NVDA or Narrator): what
  screen readers receive was checked through Qt's accessibility interface in the tests, not by listening. The
  manual check below is the next step; reports are welcome.
- With large text on a high display scale (for example 150 % text at 200 % scale on a 1920 x 1080 screen), the
  widest pages (MCP clients, Logs and updates in French) can need a horizontal scroll bar.
- Table cells cut long text with an ellipsis (the full file path is in the cell's tooltip).
- Some explanations of a disabled control (why "Allow write operations" is off) are only in its tooltip, which
  screen readers read as its description but keyboard users do not see.
- The status and float colors of the Excel dashboard are fixed; only its primary and accent colors can
  be chosen at export.
- The dashboard sheet is laid out for printing on A3; on a small screen it needs zooming.

## Manual check with a screen reader

With NVDA (or Narrator: Ctrl+Windows+Enter), open MCP for Primavera P6 Settings and check:

1. The window opens on the page list: the reader says the list name ("Pages"), the page ("Database") and its
   access key (Alt+D). The banner at the top, when there is one, is read.
2. Tab through the Database page: each radio button, button and field is read with its name and state; the
   database table is read as "Databases"; Alt + an underlined letter moves to its control.
3. Add a database, select it and press Test connection: "Testing..." then the result is read without moving the
   focus.
4. Server page: choose Local HTTP, Tab to the port field (read as "Port"), press Check: its state ("Free") is read;
   Copy says that the token was copied.
5. Skills page: Tab to the cards and use the arrow keys: each card's ID, title and state are read; Space turns it
   on or off; Enter opens the details dialog and Esc closes it.
6. Change a setting: "Unsaved changes" is read; Ctrl+S saves and "Settings saved" is read.
7. Logs and updates page: press Check now; the update state is read.
8. With a contrast theme on (Windows Settings, Accessibility, Contrast themes), every text, border and the focus
   ring stay visible, on every page.

## Report a barrier

Open a **Bug report** issue whose title starts with `Accessibility:`, and say which part (Settings,
installer, dashboard), what you use (screen reader, magnifier, keyboard only, display scale) and what
blocks you. Do not attach real project data.
