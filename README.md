# P6 MCP for Primavera P6

A local MCP server that lets an LLM client (Claude Desktop, Cursor, VS Code, LM Studio, Cline...) analyse
and, when you allow it, edit a **Primavera P6 Professional standalone (SQLite)** database on your PC.

> Not affiliated with, endorsed by or sponsored by Oracle. Primavera and P6 are trademarks of Oracle.

Status: under development (milestone 1 of 7: configuration, tool policy, two-step writes). The installer,
Settings app and client registration come in the next milestones.

## Privacy

The server runs only on your PC and makes no network call. **The data returned by its tools goes to the LLM
your client uses.** For confidential schedules, use a local model.

## Safety model

- No database is used until you select it and confirm it after a connection test.
- Tool profiles: **Basic** (a small read-only set) or **Full**.
- Writes need the Full profile, "Allow write operations", a known (or trusted) P6 schema, a dry run, and a
  second call carrying the confirmation token of that dry run. Deleting a project or an activity and
  restoring a backup also need a confirmation text. A backup is taken before every write.
- Writes go straight to the database and bypass P6's application logic: close P6 first, and press F9 in P6
  afterwards.

## Development

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest -q
```

Tests run on a synthetic database (`tests/fixtures/make_db.py`). Never commit .db, .xer, logs or backups.
