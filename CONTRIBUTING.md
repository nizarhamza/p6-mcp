# Contributing

Thank you for helping improve MCP for Primavera P6. The project has one maintainer and ships as a Windows product with
releases, so every change is agreed in an issue before any code is written.

Everyone follows the [code of conduct](CODE_OF_CONDUCT.md).

## Start with an issue

1. Search the existing issues first.
2. Open an issue with the **Bug report** or **Feature request** template: the problem or the need, your
   P6 version and MCP client. For a bug, reproduce it with the synthetic test database when you can.
3. Wait until the maintainer agrees on the change and its approach, then open a pull request that links
   the issue. A pull request without an agreed issue may be closed.

Security problems never go in a public issue: see [SECURITY.md](SECURITY.md).

## Never share real project data

Do not attach, commit or paste a real schedule, P6 database, XER file, log, backup, or a screenshot that
names real projects, clients or people: not in issues, pull requests or commits. CI refuses `.xer`, `.db`,
`.sqlite`, `.sqlite3`, `.log` and `.jsonl` files. Tests use the synthetic P6 database and XER files built
by `tests/fixtures/make_db.py`.

## Set up

Windows and Python 3.10 or later (CI tests Python 3.12 and 3.13 on Windows):

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest -q
.venv\Scripts\ruff check .
.venv\Scripts\python -m p6mcp.settings_app      # Settings window
```

## Making a change

- One change per pull request, from a branch of your fork based on `main`.
- Add or update tests with every change. `pytest` and `ruff check .` must pass: CI runs both.
- Follow the conventions of `p6mcp/server.py`: check optional columns before using them (they differ
  between P6 versions), read through read-only connections, and keep list tools paged.
- Write tools keep the two-step flow (dry run, then the confirmation token), the backup and the edit log,
  and never write planned, early or late dates or float: P6 recalculates them (F9).
- A new tool needs its kind (read, file, write or destructive) in `TOOL_KIND` of `p6mcp/policy.py`.
- Text of the Settings app goes in both `p6mcp/locales/en.json` and `p6mcp/locales/fr.json`, with the
  same keys and placeholders, straight quotes and no long dashes (`tests/test_i18n.py` checks it).
- Update README.md or SECURITY.md when the behavior they describe changes.
- Describe the user-visible change in the pull request. The maintainer writes CHANGELOG.md and makes the
  releases ([docs/RELEASING.md](docs/RELEASING.md)).

## License

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE),
as its section 5 states.
