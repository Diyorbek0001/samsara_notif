# Samsara Telegram Fault and Safety Monitor

An asynchronous service that follows Samsara's current Vehicle Stats Feed and
Safety Events Stream, persists truck faults and driver-safety events, and sends
both categories through one Telegram bot. It uses
`GET /fleet/vehicles/stats/feed?types=faultCodes`; it does not use the legacy v1
maintenance endpoint. Safety events use `GET /safety-events/stream`.

## Architecture

The fault monitor fetches one feed page at a time, normalizes valid fault records,
and commits incidents, events, notification-outbox entries, and the page cursor in
one database transaction. The notification worker sends committed outbox entries
to Telegram. The Telegram command bot reads and updates the same incident records.

Timestamps are stored in UTC and displayed in `TIMEZONE`. Diagnostic status and
maintenance workflow status are deliberately separate.

## Requirements and permissions

- Python 3.11 or newer
- Samsara API token with **Read Vehicle Statistics**
- **Read Vehicles** for full VIN/make/model/license metadata
- **Read Safety Events & Scores** for safety alerts
- **Read Camera Media** for safety-event video
- **Read Media Retrieval** and **Write Media Retrieval** to request dashcam
  footage around speeding alerts
- Telegram bot that can send messages in the destination group/topic
- PostgreSQL for production; SQLite is suitable for one local process

Tag-scoped Samsara tokens only expose vehicles covered by the selected tags.

## Installation

macOS/Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `.env`. Never commit it. `TELEGRAM_FAULT_TOPIC_ID` may remain empty.
`TELEGRAM_SAFETY_TOPIC_ID` and `TELEGRAM_SYSTEM_TOPIC_ID` are also optional.
All categories use `TELEGRAM_GROUP_ID`; empty topic IDs route to the main group.
Telegram user authorization is based only on numeric IDs in
`ADMIN_TELEGRAM_IDS` and `SUPPORT_TELEGRAM_IDS`.

## Safe mock test

Enable these values:

```env
SAMSARA_MOCK_MODE=true
SAMSARA_MOCK_FIXTURE=tests/fixtures/samsara/fault_feed_initial.json
```

Then run:

```bash
python -m app.main doctor
python -m app.main poll-once
python -m app.main show-cursor
pytest
```

The initial poll stores baseline incidents and a cursor but does not queue alerts
when `SEND_INITIAL_ACTIVE_FAULTS=false`. To test Telegram routing without a real
incident, start the bot and use `/fault_test` as an authorized user.

## Live operation

```bash
python -m app.main doctor
python -m app.main poll-faults-once
python -m app.main poll-safety-once
python -m app.main run
```

`run` starts the fault monitor, safety monitor, Telegram command polling, and
notification outbox. `monitor` starts only Samsara polling. A process lock rejects
duplicate local monitor starts. Stop with Ctrl+C.

## Database-managed accounts and routing

The Railway worker starts with no Samsara credentials or Telegram destinations in
its environment. Administrators manage them from Telegram:

- `/addSamsara` in the bot's private chat validates and encrypts a new API token.
- `/samsaras` lists configured account names without exposing tokens.
- `/removeSamsara <name>` disables an account and all of its routes.
- `/activate` in a group or forum thread selects Fault codes, Safety alerts, or
  Both, then asks for the custom Samsara account name.
- `/deactivate` disables routes only in the exact group/thread where it is sent.

Each group thread is an independent route. Activating one topic never activates
another topic in the same supergroup. Only users configured in
`ADMIN_TELEGRAM_IDS` may manage accounts or routes.

## Railway deployment

Deploy this repository as one worker service and add a Railway PostgreSQL service.
Set these worker variables:

```env
BOT_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789,@optional_username
DATABASE_URL=${{Postgres.DATABASE_URL}}
TOKEN_ENCRYPTION_KEY=...
TIMEZONE=America/New_York
LOG_LEVEL=INFO
LOG_FILE=
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=30
```

Generate the encryption key locally:

```bash
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Do not change `TOKEN_ENCRYPTION_KEY` after adding accounts; existing tokens would
become unreadable. The service is a polling worker and does not require a public
domain. `railway.json` and `Dockerfile` define its build and start command.

The feed cursor advances only after the corresponding page is persisted. While
`hasNextPage` is true, pagination continues immediately. Otherwise the monitor
waits for `SAMSARA_FAULT_POLL_SECONDS` (minimum five seconds).

The initial cursorless call is treated as a baseline. Existing faults are stored
without alerting by default. Set `SEND_INITIAL_ACTIVE_FAULTS=true` only if the
maintenance group explicitly wants those initial alerts.

## Commands and workflow

- `/active_faults`, `/faults [truck]`, `/critical_faults`
- `/fault FLT-YYYYMMDD-NNNN`
- `/fault_summary`, `/fault_stats`
- `/fault_test`, `/fault_help`
- `/safety`, `/safety_today`, `/safety_active`
- `/safety_summary`, `/safety_stats`, `/safety_test`

Alerts include acknowledge, start-work, note, assign, ignore, and refresh controls.
Acknowledgement, start-work, and confirmed-ignore changes are persisted and
authorized by numeric Telegram ID. Note/assignment guided input is reserved for a
subsequent workflow extension; the buttons currently give an operator prompt
without silently changing data.

Safety alerts include acknowledge, reviewed, coached, dismiss, refresh, and map
controls. New events are deduplicated by Samsara event ID and receive
`SAFE-YYYYMMDD-NNNN` IDs. The first safety call reads the configured recent
lookback, saves its cursor, and suppresses historical alerts by default. When
Samsara provides a usable media URL, the sender attempts video delivery and falls
back to the complete text alert if Telegram cannot retrieve it.

Speeding alerts do not include dashcam video in the alert webhook itself. When a
speeding safety event has no attached media, the monitor requests a 20-second
high-resolution clip (10 seconds before through 10 seconds after the event) from
both road-facing and driver-facing cameras. The dashcam must have been installed,
recording, and able to upload the requested interval; offline devices may not make
the clip available before the initial notification is sent.

Camera footage is sensitive. Restrict the Telegram group, Samsara token, database,
and temporary storage to authorized staff. Signed camera URLs must never be
copied into chat text or logs.

## Clearing and duplicate behavior

Fault identity combines vehicle ID with J1939 SPN+FMI, normalized OBD-II code, OEM
code, or the strongest raw identity available. Occurrence counts and timestamps
are excluded. A repeated active code updates the existing incident.

The service clears an incident only on an explicit inactive/cleared signal.
Feed-record absence is not assumed to be a complete snapshot and therefore does
not clear an incident. This conservative rule prevents false clear messages until
a real fleet payload confirms stronger semantics.

Every notification has a unique outbox idempotency key. Failed sends retry with
bounded exponential backoff. Telegram itself has no atomic send-and-record API, so
an abrupt process loss between Telegram accepting a message and the database
commit remains a small reconciliation edge case.

## Cursor administration

```bash
python -m app.main show-cursor
python -m app.main reset-cursor
```

Reset requires typing `RESET` in the terminal. After reset, the next call is again
treated as baseline.

## Database and migrations

Startup creates missing tables for a new installation. Alembic is configured for
managed schema evolution:

```bash
alembic revision --autogenerate -m "schema change"
alembic upgrade head
```

Back up SQLite by stopping the service and copying `fleet_faults.db`. For
PostgreSQL, use regular `pg_dump` backups. Preserve the `integration_state`,
incident, event, sequence, and outbox tables when restoring.

## Deployment

Railway should use a PostgreSQL `DATABASE_URL`, environment variables, one worker
replica, and the Procfile command. Do not use ephemeral SQLite storage. This
version prevents duplicate loops inside one process; enforce one Railway replica
until a PostgreSQL advisory-lock implementation is enabled.

For a VPS, install under `/opt/samsara-fault-monitor`, create a non-root
`samsara-monitor` user, put secrets in `/etc/samsara-fault-monitor.env` with
restricted permissions, and install `deploy/samsara-fault-monitor.service`.
Application logs rotate at five files of approximately 5 MB each.

## Troubleshooting and security

Run `python -m app.main doctor`. HTTP 401 means the token is invalid; 403 commonly
means missing scopes or tag access. Telegram failures commonly mean the bot was
removed, cannot post, or the topic ID is invalid. Credentials and authorization
headers are never logged. Do not paste tokens into Telegram or commit `.env`.
