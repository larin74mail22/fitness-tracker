# FITNESS Tracker

**English** | [Русский](README.ru.md)

**Your Fitness Data. Not Your AI's Memory.**

FITNESS Tracker is a local-first fitness data layer that lets compatible AI assistants maintain your training
diary, record new information, retrieve historical records and analyze your progress through MCP
(Model Context Protocol). Body measurements, workouts, nutrition, alcohol intake, notes, progress photos and
activity data imported from Android Health Connect (steps, heart rate, sleep) are stored in a SQLite database on
your own computer.

> Your tracker is the persistent source of truth. Your AI is the coach and the interface to your data.

## The core idea

### Persistent fitness history independent of AI memory

AI conversations have limited context, and the memory built into an AI assistant is not a substitute for a
complete, structured record. Details get summarized, forgotten or mixed up, and they stay tied to one model and
one account.

FITNESS Tracker keeps every record in a local SQLite database (`data/fitness.db`) with a proper relational schema:
workouts with exercises and sets, meals with individual products, measurements, and so on. The history does not
depend on a particular AI model, conversation or provider. You can switch assistants, start a new chat or stop
using AI altogether, and the data stays where it is. The local web dashboard shows the same data without any AI.

### An AI assistant that maintains your diary

Through MCP, a connected AI assistant calls the tracker's tools on your behalf. In practice you tell the assistant
in plain language about a completed workout ("bench press 3×8 at 80 kg, RIR 2"), a new body measurement or a
meal, and it saves a structured record. Later you ask about a date or a period, and it retrieves the stored
history, compares results and analyzes your progress. This makes the assistant an ongoing fitness coach that works
from your actual records rather than only from what is in the current chat.

What the MCP tools actually support:

| Operation | Supported |
|---|---|
| Record | Body weight and measurements, workouts (exercises, sets, weight, reps, RIR, duration, distance, discomfort notes), meals (products, quantities, calories and macros with source and "estimate" flag), alcohol, notes with categories and structured fields, progress photos (the file must already be in `data/photos`) |
| Retrieve | Measurements, workouts and nutrition for a date or period, alcohol summaries, search by type, date, keyword and note category, any single record in full, list of known exercise names |
| Analyze | Period statistics across all sections, Health Connect summaries and per-day activity (steps, heart rate, sleep), comparison with the previous period |
| Delete | Any record, in two steps: the assistant shows the record and gets a confirmation code, and the deletion runs only with that code; a database backup is made automatically first |
| Edit an existing record | **Not supported.** To correct a record, delete it and record it again |
| Other | Tracker status, manual database backup, Health Connect import and import log |

Dates are always explicit (`YYYY-MM-DD`) and are never silently set to "today", unknown values are stored as
empty rather than zero, and approximate nutrition values are marked as estimates. The assistant still interprets
your messages, so it is worth checking what it saved. Full tool definitions are in `fitness/tools.py`; an
overview is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### ChatGPT and other MCP clients

The MCP server (`server.py`) uses the stdio transport, so an AI client has to be able to start it as a local
process, directly or through a bridge. The documented and author-tested setup is ChatGPT through the official
tunnel-client (see [Connecting ChatGPT](#connecting-chatgpt-through-tunnel-client)). Other AI clients that support
MCP and can launch a local stdio server should be able to use the same tools, but they have not been tested with
this project. Connection setup differs between clients, and the client decides which tools it exposes and how it
asks for permission, so nothing works without configuration. Attaching a Health Connect file directly to a message
relies on a ChatGPT-specific file parameter; other clients can use the chunked upload tools or the inbox folder.

### Health and smartwatch data through Android Health Connect

Android Health Connect collects data from compatible health apps and watches on the phone. FITNESS Tracker can
import from it, but not directly: a separate Android bridge app (not part of this repository) reads Health Connect
and writes JSON export files in this project's own format
([docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md)). The importer then loads those files into the local
database.

The current implementation imports exactly three kinds of data: **steps, heart rate and sleep** (including sleep
stages), plus deletions of those records made in Health Connect. Other Health Connect data types (workouts,
weight, calories, distance and so on) are not imported. Whether your watch's data reaches the tracker depends on
whether its app writes steps, heart rate or sleep to Health Connect; this is not a direct integration with any
particular smartwatch.

### Local-first storage

The database, photos, imports, backups and logs stay in the `data/` folder of your copy. There is no cloud
database, no account and no telemetry, and the tracker, dashboard and importer work fully offline. When you connect
an AI client, it receives the data it requests through the MCP tools, and that data is then processed by the AI
service under its own terms. Local storage means you own the complete record; it does not mean that data shared
with an external AI service never leaves your computer. See [Privacy and data flow](#privacy-and-data-flow).

## How it works

```
AI client (ChatGPT) ──▶ tunnel-client ──stdio──▶ server.py ──▶ data/fitness.db (SQLite)
                                                                       ▲
Android: Health Connect ─▶ bridge app ─▶ JSON export ─▶ importer ──────┘
                                                                       │
Browser ◀── dashboard.py (127.0.0.1:8765, read-only except photos) ◀───┘
```

- **SQLite database.** `data/fitness.db` is created automatically on the first write. Schema changes are versioned
  SQL migrations (`fitness/migrations/`); an existing database is upgraded only by the explicit
  `python migrate_db.py` command, which makes a backup first. Related records (a workout with its exercises and
  sets) are written in a single transaction.
- **MCP server.** `server.py` is a small MCP implementation over stdio written with the Python standard library
  (no MCP SDK). It supports `initialize`, `notifications/initialized`, `ping`, `tools/list` and `tools/call`.
  stdout carries only protocol messages; diagnostics go to stderr. Tool arguments are structured and validated:
  dates are required and never silently defaulted to "today", units are fixed (kg, cm, ml), unknown values are
  stored as NULL rather than 0, and approximate nutrition estimates are flagged as such. No arbitrary SQL, shell
  commands or code are accepted from tool arguments.
- **AI client.** The AI client does not access the database directly. It calls the MCP tools; with ChatGPT this
  goes through the official tunnel-client, which runs `server.py` locally.
- **Health Connect import.** The importer validates the whole file first and then writes it in one transaction:
  either everything is imported or nothing is. Re-importing the same file changes nothing. Files can be imported
  manually, picked up automatically from an inbox folder (for example, synced from the phone with Syncthing), or
  passed from an AI conversation.

## Features

- **MCP server** with 26 tools for recording, retrieving, searching, analyzing and deleting records and for
  Health Connect import (see [the table above](#an-ai-assistant-that-maintains-your-diary)).
- **Web dashboard** (`dashboard.py`, `127.0.0.1` only): charts and history of measurements, workouts, nutrition
  and alcohol, plus Health, Sync and Photos pages. The database is opened read-only; the Photos page is the only
  one that writes (upload and photo trash).
- **English and Russian interface**: the dashboard and the tray menu are available in English (default) and
  Russian, switched with the `EN | RU` selector in the dashboard header (see
  [Interface language](#interface-language) for what is not translated).
- **Health Connect import** of steps, heart rate and sleep: manual (`import_health.py`), inbox folder
  (`health_inbox.py`, once or `--watch`), or from an AI conversation (`import_health_export`).
- **Windows launcher** (`tracker.pyw`): tray icon, supervised tunnel-client, automatic inbox import, dashboard
  on demand, optional autostart via Task Scheduler.
- Database backups, explicit schema migrations, strict validation of dates, numbers and units.

## Privacy and data flow

- The database, photos, imports, backups and logs stay in the `data/` folder of your copy. There is no cloud
  database, no third-party API and no telemetry.
- **If you connect an AI client, it receives the data it requests through the MCP tools** (for example, your
  workout history or daily steps). That data is then processed by the AI service under the terms of your account
  (for ChatGPT, your OpenAI account). If you do not connect an AI client, the tracker, dashboard and importer work
  fully offline.
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
  environment variable (see below). Other MCP clients need their own configuration to start `server.py`.
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

**MCP server** (normally started by tunnel-client or another MCP client; run it manually only to check that it
starts):

```
python server.py
```

The server reads MCP messages from stdin and exits when stdin is closed.

**Web dashboard:**

```
python dashboard.py            # then open http://127.0.0.1:8765/
python dashboard.py --port 8800
```

### Interface language

On a new installation the dashboard and the tray menu start in English; an existing installation whose
`data/launcher.json` predates this setting stays in Russian. The `EN | RU` switch in the dashboard header changes
the language immediately, without a restart. The choice is stored as `"language": "en"` or `"ru"` in
`data/launcher.json` (never in the fitness database) and is kept after page reloads and restarts.

Known localization limitations: only the dashboard and the tray menu are translated. MCP tool descriptions, error
texts from the core (which the AI assistant relays), command-line messages, [QUICKSTART.md](QUICKSTART.md) and
the detailed documentation in `docs/` are in Russian. Tool names, database fields and your saved records stay as
they are. An AI assistant can still talk to you in English, since it reads the Russian tool descriptions itself.

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

**Other MCP clients.** A client that can run a local stdio MCP server can be pointed at `python` with the absolute
path of `server.py` as its argument. This is not documented step by step here and has not been tested.

**Verification status.** The basic ChatGPT workflow (create a record, read it back, duplicate protection,
deletion) was checked by the author on an early version of the project. The current version has not been
re-verified end to end through ChatGPT, and a clean installation on another computer following these
instructions has not been tested through ChatGPT.

## Tests

```
python -m unittest discover -s tests -t .
```

The suite (641 tests) uses temporary databases and synthetic data only; the working `data/fitness.db` is never
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
- Existing records cannot be edited through MCP; corrections are made by deleting and recording again.
- Only ChatGPT through tunnel-client has been tested as an AI client; see "Verification status" above.
- Health Connect import covers steps, heart rate and sleep only and requires a separate bridge app.
- The dashboard is not designed for network access.
- Schema upgrades require the explicit `python migrate_db.py` command.
- The Health Connect export format is this project's own format (version 1), not a Google standard.
- Only the dashboard and tray menu are localized; tool descriptions, core error texts and most documentation are
  in Russian.
- This is a personal project, not a fully tested product.

## Documentation

All files below are in Russian.

- [QUICKSTART.md](QUICKSTART.md) — Windows launcher
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design, database schema, MCP tools
- [docs/HEALTH_SCHEMA.md](docs/HEALTH_SCHEMA.md), [docs/HEALTH_EXPORT_FORMAT.md](docs/HEALTH_EXPORT_FORMAT.md) — Health Connect data
- [docs/HEALTH_AUTO_IMPORT.md](docs/HEALTH_AUTO_IMPORT.md), [docs/HEALTH_PHONE_TRANSFER.md](docs/HEALTH_PHONE_TRANSFER.md),
  [docs/HEALTH_SYNCTHING.md](docs/HEALTH_SYNCTHING.md), [docs/HEALTH_MCP_IMPORT.md](docs/HEALTH_MCP_IMPORT.md) — transfer and import
- [docs/UPDATE_AND_RECOVERY.md](docs/UPDATE_AND_RECOVERY.md) — updates and recovery

## Author and feedback

Created by Alexey Larin, an independent developer from Russia. Telegram: [@Alex_showw](https://t.me/Alex_showw).

- **Bug reports and feature requests:** please use GitHub Issues in this repository (primary channel).
  Do not attach real databases, health exports or logs: they contain personal data.
- **Telegram:** an additional channel for questions and contact.

## License

[MIT](LICENSE).
