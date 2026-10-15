# FITNESS Tracker

**English** | [Русский](README.ru.md)

A local fitness tracker that keeps your body measurements, workouts, nutrition, alcohol intake, notes, progress
photos and smartwatch data (steps, heart rate, sleep from Android Health Connect) in a SQLite database on your
own computer, and lets ChatGPT read and write that data through a local MCP server.

> The application's user interface, messages and most of the detailed documentation in `docs/` are in Russian.
> Code identifiers and MCP tool names are in English.

## Why

Logging fitness data usually means either a cloud service that owns your data or a spreadsheet you have to
maintain by hand. FITNESS Tracker takes a different approach:

- the data lives in one SQLite file on your machine;
- you talk to ChatGPT in plain language ("bench press 3×8 at 80 kg, RIR 2"), and ChatGPT stores structured
  records through MCP tools, retrieves history and analyses trends;
- a local web dashboard shows charts and history without ChatGPT.

## How it works

```
ChatGPT ──(app/connector)──▶ tunnel-client ──stdio──▶ server.py ──▶ data/fitness.db (SQLite)
                                                                         ▲
Android phone: Health Connect ─▶ bridge app ─▶ JSON export ─▶ importer ──┘
                                                                         │
Browser ◀── dashboard.py (127.0.0.1:8765, read-only except photos) ◀─────┘
```

- **SQLite database.** `data/fitness.db` is created automatically on the first write. Schema changes are versioned
  SQL migrations (`fitness/migrations/`); an existing database is upgraded only by the explicit
  `python migrate_db.py` command, which makes a backup first. Related records (a workout with its exercises and
  sets) are written in a single transaction.
- **MCP server.** `server.py` is a small MCP implementation over stdio written with the Python standard library
  (no MCP SDK). It supports `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call`.
  stdout carries only protocol messages; diagnostics go to stderr. Tool arguments are structured and validated:
  dates are required and never silently defaulted to "today", units are fixed (kg, cm, ml), unknown values are
  stored as NULL rather than 0, and approximate nutrition estimates are flagged as such.
- **ChatGPT.** ChatGPT does not access the database directly. It calls the MCP tools through the official
  tunnel-client, which runs `server.py` locally.
- **Health Connect import.** A separate Android bridge app (not part of this repository) exports steps, heart rate
  and sleep to JSON files in the project's own format ([docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md)).
  The importer validates the whole file first and then writes it in one transaction: either everything is
  imported or nothing is. Re-importing the same file changes nothing. Files can be imported manually, picked up
  automatically from an inbox folder (for example, synced from the phone with Syncthing), or passed from a
  ChatGPT conversation.

## Features

- **MCP server** with 26 tools: log and query body measurements, workouts (exercises, sets, weight, reps, RIR,
  discomfort notes), meals (calories and macros with an "estimate" flag), alcohol, notes; search, period
  statistics, daily activity, Health Connect summaries and imports; two-step deletion with a confirmation code
  and an automatic backup. Full tool descriptions: `fitness/tools.py`; overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Web dashboard** (`dashboard.py`, `127.0.0.1` only): charts and history, Health, Sync and Photos pages.
  The database is opened read-only; the Photos page is the only one that writes (upload and photo trash).
- **Health Connect import**: manual (`import_health.py`), inbox folder (`health_inbox.py`, once or `--watch`),
  or from ChatGPT (`import_health_export`).
- **Windows launcher** (`tracker.pyw`): tray icon, supervised tunnel-client, automatic inbox import, dashboard
  on demand, optional autostart via Task Scheduler.
- Database backups, explicit schema migrations, strict validation of dates, numbers and units.

## Privacy and data flow

- The database, photos, imports, backups and logs stay in the `data/` folder of your copy. There is no cloud
  database, no third-party API and no telemetry.
- **If you connect ChatGPT, it receives the data it requests through the MCP tools** (for example, your workout
  history or daily steps). That data is then processed by ChatGPT under the terms of your OpenAI account.
  If you do not connect ChatGPT, the tracker, dashboard and importer work fully offline.
- The dashboard listens on `127.0.0.1` only, checks the `Host` header on every request and the `Origin` header on
  write requests.
- The tunnel-client key is read from an environment variable and is never written to the project.
- `data/` and real Health Connect exports (`fitness-health-*.json`) are excluded by `.gitignore`. Do not commit
  them, and do not attach real databases, exports or logs to bug reports.

## Requirements

- Python **3.11** or newer. No third-party packages; no `pip install` is needed.
- Tested on Windows 10/11 only. The core, MCP server, dashboard and importer have no Windows-specific code but have
  not been tested on other operating systems. The launcher (`tracker.pyw`, tray, autostart, `.bat` and `.ps1`
  scripts) is Windows-only.
- To use ChatGPT: the official tunnel-client for connecting a local MCP server, a profile and an API key in an
  environment variable (see below).
- For smartwatch data: an Android phone with Health Connect and a bridge app that writes files in the documented
  format. The bridge app is **not** included in this repository.

## Installation

```
git clone https://github.com/larin74mail22/fitness-tracker.git
cd fitness-tracker
python --version            # must be 3.11 or newer
```

Nothing else needs to be installed. The `data/` folder and `data/fitness.db` are created on the first write.
To use a different database location, set the `FITNESS_DB_PATH` environment variable.

## Running

**MCP server** (normally started by tunnel-client; run it manually only to check that it starts):

```
python server.py
```

The server reads MCP messages from stdin and exits when stdin is closed.

**Web dashboard:**

```
python dashboard.py            # then open http://127.0.0.1:8765/
python dashboard.py --port 8800
```

**Health Connect import.** The importer works only with an existing database: it neither creates the database nor
changes its schema. Make at least one record through MCP first; upgrade an older database with
`python migrate_db.py`.

```
python import_health.py --dry-run examples/health-sample.json   # validate the synthetic sample without writing
python import_health.py PATH_TO_FILE_OR_FOLDER
python health_inbox.py --watch                                   # watch data/health_inbox
```

**Windows launcher:** double-click `tracker.pyw` (see [QUICKSTART.md](QUICKSTART.md), in Russian). Settings are
created in `data/launcher.json` on first start; see [launcher.example.json](launcher.example.json) for the format.

## Connecting ChatGPT through tunnel-client

1. Install tunnel-client and create the ChatGPT app (connector) as described in its documentation.
2. Copy [examples/tunnel-client/fitness-local.example.yaml](examples/tunnel-client/fitness-local.example.yaml) to
   `%APPDATA%\tunnel-client\fitness-local.yaml` and fill in your own values. Set `command` to the absolute path
   of `server.py` in your copy.
3. Put the key in the user environment variable `CONTROL_PLANE_API_KEY`, not in the profile and not in code.
4. Start tunnel-client with the `fitness-local` profile (or start `tracker.pyw`, which does it for you).
5. Refresh the app/connector in ChatGPT so that it picks up the tool list.

**Verification status.** The basic ChatGPT workflow (create a record, read it back, duplicate protection,
deletion) was checked by the author on an early version of the project. The current version has not been
re-verified end to end through ChatGPT, and a clean installation on another computer following these
instructions has not been tested through ChatGPT.

## Tests

```
python -m unittest discover -s tests -t .
```

The suite (612 tests) uses temporary databases and synthetic data only; the working `data/fitness.db` is never
opened. The tests have been run on Windows only.

## Data locations

| Path | Contents |
|---|---|
| `data/fitness.db` | SQLite database |
| `data/backups/` | backups (before migrations and deletions) |
| `data/photos/` | photos |
| `data/health_inbox/`, `health_processed/`, `health_failed/` | Health Connect import queue |
| `data/logs/`, `data/run/` | launcher logs and runtime state |

## Limitations

- Single-user local application: no user accounts and no database encryption. Anyone with access to your
  Windows profile can read the data.
- The dashboard is not designed for network access.
- Schema upgrades require the explicit `python migrate_db.py` command.
- The Health Connect export format is this project's own format (version 1), not a Google standard.
- The user interface and most documentation are in Russian.
- This is a personal project, not a fully tested product: see "Verification status" above.

## Documentation

All files below are in Russian.

- [QUICKSTART.md](QUICKSTART.md) — Windows launcher
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design, database schema, MCP tools
- [docs/HEALTH_SCHEMA.md](docs/HEALTH_SCHEMA.md), [docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md) — Health Connect data
- [docs/HEALTH_AUTO_IMPORT.md](docs/HEALTH_AUTO_IMPORT.md), [docs/HEALTH_PHONE_TRANSFER.md](docs/HEALTH_PHONE_TRANSFER.md),
  [docs/HEALTH_SYNCTHING.md](docs/HEALTH_SYNCTHING.md), [docs/HEALTH_MCP_IMPORT.md](docs/HEALTH_MCP_IMPORT.md) — transfer and import
- [docs/UPDATE_AND_RECOVERY.md](docs/UPDATE_AND_RECOVERY.md) — updates and recovery

## Author and feedback

Author: Alexey Larin, Telegram [@Alex_showw](https://t.me/Alex_showw).

- **Bug reports and feature requests:** please use GitHub Issues in this repository (primary channel).
  Do not attach real databases, health exports or logs: they contain personal data.
- **Telegram:** an additional channel for questions and contact.

## License

[MIT](LICENSE).
