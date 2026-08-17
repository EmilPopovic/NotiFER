# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**NotiFER** is a web service that monitors FER (Faculty of Electrical Engineering and Computing, Zagreb) student timetable calendars via ICS feeds and sends email notifications when schedule changes are detected.

## Development Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Start development environment (API + PostgreSQL + Worker)
docker compose -f compose.dev.yaml up --build -d

# Initialize database tables
make initdb COMPOSE_FILE=compose.dev.yaml
```

## Database Management

```bash
make initdb COMPOSE_FILE=compose.dev.yaml    # Create tables
make resetdb COMPOSE_FILE=compose.dev.yaml   # Drop and recreate all tables
make dropdb COMPOSE_FILE=compose.dev.yaml    # Drop all tables
make checkdb COMPOSE_FILE=compose.dev.yaml   # Check table status
make encryptdb COMPOSE_FILE=compose.dev.yaml # Encrypt plaintext calendar_auth values (idempotent)
make verifydb COMPOSE_FILE=compose.dev.yaml  # Check every calendar_auth decrypts under ENCRYPTION_KEY
```

These commands run `db_manager.py` inside the running container.

## Server Migration

```bash
make exportdb FILE=bundle.nfer               # Download an encrypted bundle of all server data
make importdb FILE=bundle.nfer DRY_RUN=1     # Validate a bundle, write nothing
make importdb FILE=bundle.nfer               # Restore a bundle, replacing existing data
```

`shared/migration.py` builds and applies passphrase-encrypted bundles carrying the
`user_calendars` and `audit_log` tables plus the cached ICS baselines. The same code
backs `/dashboard/migration`. See the README for the full runbook.

Key points when working on this:

- Bundles are **re-keyed at the boundary** — `calendar_auth` is decrypted with the
  source machine's `ENCRYPTION_KEY` on export and re-encrypted with the target's on
  import, so the two deployments need not share a key.
- `migration.py` uses **relative imports only**. `db_manager` runs as `src.db_manager`
  while the API runs with `/app/src` on the path, so an absolute `shared.*` import
  would load a second copy of the ORM models with a different declarative `Base`.
- `_USER_COLUMNS` / `_LOG_COLUMNS` are checked against the live table on every
  export. **Adding a model column will fail the export loudly** until the format is
  updated — this is deliberate, so a schema change cannot silently drop data.
- Audit log rows restore with their original ids, so the serial sequence is
  resynced with `setval` afterwards or the next insert collides.
- Importing rewrites `previous_calendar_path` for the local machine and recomputes
  `previous_calendar_hash` from the restored content. Where a bundle carries no
  cached calendar, both are cleared so the worker re-baselines silently instead of
  holding a hash it cannot compare against.
- `shared/maintenance.py` is the gate the dashboard import uses to hold the worker
  off. API and worker are threads of one process, so an `Event` suffices; it lives
  in `shared` so neither package imports the other.

## Architecture

The app runs two concurrent threads from `src/run.py`:

- **API thread**: FastAPI server on port 8026 handling HTTP requests
- **Worker thread**: Background polling loop that checks calendars for changes and queues notification emails

### Source layout

```text
src/
├── run.py                    # Entry point — starts API + Worker threads
├── config.py                 # Pydantic settings loaded from env vars
├── db_manager.py             # CLI for DB init/reset/drop/check/encrypt
├── api/
│   ├── main.py               # FastAPI app + route registration
│   ├── routers/
│   │   ├── subscriptions.py  # Student self-service endpoints
│   │   ├── admin.py          # Admin endpoints (token-protected)
│   │   ├── health.py         # Health + metrics
│   │   ├── dashboard.py      # Admin web UI incl. data export/import
│   │   └── frontend.py       # Static HTML serving
│   └── servi
## Contact

For questions, support, or a demo, please contact:
**Emil Popović**
<admin@emilpopovic.me>

_NotiFER is currently developed and maintained by Emil Popović, a student at FER._
ces/
│       ├── subscription_service.py
│       ├── email_service.py
│       └── template_service.py  # Jinja2 + i18n rendering
├── worker/
│   ├── main.py               # Standalone worker entry point
│   └── services/
│       ├── worker_service.py     # Main polling loop
│       └── calendar_service.py  # Change detection + email queuing
└── shared/
    ├── models.py             # SQLAlchemy ORM — single `user_calendars` table
    ├── crud.py               # All DB queries
    ├── database.py           # Engine + session factory
    ├── encryption.py         # Fernet TypeDecorator for calendar_auth at-rest encryption
    ├── migration.py          # Encrypted server-migration bundles (build/read/apply)
    ├── maintenance.py        # Worker pause gate used during data import
    ├── email_client.py       # Thread-safe email queue
    ├── email_sender.py       # SMTP sending
    ├── email_templates.py    # HTML email templates (Croatian/English)
    ├── calendar_utils.py     # ICS parsing + diff/change detection
    ├── token_utils.py        # JWT generation/validation
    └── storage_manager.py    # Local ICS file caching
```

### Key data flow

1. Student subscribes → pending `user_calendars` row created, activation email sent (JWT link)
2. Student clicks activation link → row marked active
3. Worker polls at `WORKER_INTERVAL` seconds → downloads ICS, hashes content, compares to stored hash
4. On change detected → notification email queued via `email_client.py`, hash updated in DB
5. Email sender drains queue at `EMAIL_RATE_LIMIT_PER_SECOND`

### Database

Single table `user_calendars` with composite primary key `(username, domain)`. Stores calendar auth token, activation state, language preference, ICS hash, and change history timestamps.

### Feature flags (env vars)

Features can be toggled independently:

- `STUDENT_SIGNUP`, `STUDENT_PAUSE`, `STUDENT_RESUME`, `STUDENT_DELETE` — self-service operations
- `ADMIN_API`, `FRONTEND` — module toggles
- `WORKER` — enable/disable background polling
- `DATA_EXPORT` (default on), `DATA_IMPORT` (default off) — migration bundle endpoints

## Environment Variables

Copy `.env.example` and fill in values. Key vars:

| Variable | Purpose |
| --- | --- |
| `POSTGRES_*` | Database connection |
| `SMTP_*` | Email sending |
| `JWT_KEY` | Token signing |
| `ENCRYPTION_KEY` | Fernet key for `calendar_auth` at-rest encryption — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, then run `make encryptdb` on existing deployments. Must carry over when moving machines |
| `MIGRATION_PASSPHRASE` | Optional. Bundle passphrase for scripted `make exportdb` / `importdb`; prompted for when unset |
| `NOTIFER_API_TOKEN_HASH` | SHA256 hash of admin API token |
| `API_URL` | Base URL used in email links |
| `BASE_CALENDAR_URL` | FER ICS calendar URL template |
| `WORKER_INTERVAL` | Seconds between calendar checks (default 3600) |
| `RECIPIENT_DOMAIN` | Allowed email domain (default `fer.hr`) |

## Deployment

```bash
# Production
docker compose -f compose.yaml up -d
make initdb

# CI/CD builds and pushes to GHCR on pushes to master
# Image: ghcr.io/emilpopovic/notifer:latest
```

## Internationalization

Email templates and UI support Croatian (`hr`) and English (`en`). Templates live in `templates/` and are rendered via `api/services/template_service.py` using Jinja2. Email-specific templates are in `shared/email_templates.py`.
