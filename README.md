# Scheduled AI Content Bot

A Python automation pipeline that turns a Notion editorial calendar into
AI-generated social posts: Gemini writes the copy, an optional Cloudflare
FLUX image is generated, and an X publisher posts the result — backed by
durable SQLite execution state, bounded retries, recovery, and a JSONL
audit trail.

## Features

- Notion editorial calendar as the content source (plus a local
  `calendar.yaml` mode)
- APScheduler polling with per-cycle dedupe and pending-sync reconciliation
- Gemini copy generation with deterministic validation before publishing
- Optional image generation (`Generate Image`): Cloudflare Workers AI with
  `@cf/black-forest-labs/flux-1-schnell`; Google provider supported as an
  alternative; images stored locally in `artifacts/images/`
- X publisher abstraction (text, or text + one image)
- SQLite durable state: idempotency gate, crash recovery, bounded retries
- `DRY_RUN` mode: full pipeline without ever touching the X API
- JSONL audit trail (append-only, one line per attempt)
- 433 automated tests with no external services, three offline validation
  scripts, and a CI workflow

## Architecture

```
Notion
  ↓
APScheduler Poller
  ↓
Orchestrator
  ├── Gemini text
  ├── Image generator [optional]
  │     └── Cloudflare FLUX / Google
  ├── Validator
  └── X Publisher
        ↓
SQLite technical state
        ↓
Notion sync

JSONL → audit trail
```

- **Notion** = editorial state (topics, schedule, status, published URL)
- **SQLite** = durable technical state (idempotency, retries, media/image
  metadata)
- **JSONL** = audit history (never used for decisions)

## Reliability

- **Durable identity:** one row per `content_item_id + platform`; a
  recorded publication blocks any re-publish regardless of what Notion says.
- **Ordering:** the successful publication (with its external post ID) is
  persisted to SQLite *before* the final Notion sync; a failed sync is
  retried later without re-posting or re-uploading media.
- **`sync_pending` reconciliation:** every polling cycle first settles
  outstanding Notion writes.
- **Bounded retries:** transient failures (network, 408/425/429/5xx) use
  exponential backoff up to `RETRY_MAX_ATTEMPTS`; permanent failures
  surface immediately.
- **Ambiguous outcomes escalate:** a crash between `publishing` and the X
  response moves the row to terminal `manual_review` instead of risking a
  duplicate.

Guarantee: **best-effort at-most-once publication with durable idempotency
safeguards.** Exactly-once delivery is not claimed.

## Notion Database Schema

Create the database by hand and share it with your Notion integration;
the app validates the schema at startup and names any missing property.

| Property | Type |
| --- | --- |
| `Topic` | Title (required) |
| `Angle` | Rich text (required) |
| `Scheduled At` | Date, UTC (required) |
| `Platform` | Select, `x` (required) |
| `Status` | Status or Select (required) |
| `References` | Rich text |
| `Reference Notes` | Rich text |
| `Generate Image` | Checkbox |
| `Image Brief` | Rich text |
| `Generated Copy` | Rich text |
| `Generated Image` | URL |
| `Published URL` | URL |
| `Error` | Rich text |

Missing optional properties are reported and skipped.

## Status Flow

```
Scheduled → Generating → Ready → Publishing → Published
                ↓           ↓
             Failed       Failed
```

- `DRY_RUN=true` ends at **Ready**; `Published URL` stays empty.
- Unknown status values are an explicit error, never invented.

## Setup

Python 3.14 (the version used for development and CI).

```bash
git clone https://github.com/DrInfinity16/scheduled-ai-content-bot.git
cd scheduled-ai-content-bot

python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

copy .env.example .env      # Windows; use `cp` on macOS/Linux
# edit .env and fill in your credentials

python main.py
```

Configuration mistakes (missing Gemini key, incomplete Notion credentials,
unsupported image provider) are reported as short startup messages, not
stack traces.

## Configuration

Copy `.env.example` for the full set. Grouped overview:

```dotenv
# Core
GEMINI_API_KEY=your-gemini-api-key
MODEL=gemini-2.0-flash
DRY_RUN=true                  # true = simulate, false = publish on X
CONTENT_SOURCE=auto           # auto | notion | yaml

# Notion (both or neither in auto mode)
NOTION_TOKEN=your-notion-token
NOTION_DATABASE_ID=your-notion-database-id
CONTENT_POLL_INTERVAL_SECONDS=60

# Image (only for items with Generate Image)
IMAGE_PROVIDER=cloudflare     # cloudflare | google
IMAGE_MODEL=@cf/black-forest-labs/flux-1-schnell
CLOUDFLARE_ACCOUNT_ID=your-cloudflare-account-id
CLOUDFLARE_API_TOKEN=your-cloudflare-api-token
IMAGE_OUTPUT_DIR=artifacts/images

# X (required only when DRY_RUN=false)
X_USERNAME=your_handle
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_SECRET=...

# Reliability
DATABASE_PATH=content_bot.db
RETRY_MAX_ATTEMPTS=3
RETRY_BASE_DELAY_SECONDS=1.0
```

Cloudflare is the recommended example configuration used by this
repository; Google remains supported as an alternative provider (the code
default when `IMAGE_PROVIDER` is unset is `google`).

**Start in simulation mode.** With `DRY_RUN=true` (the default) the full
cycle runs — validation, Gemini copy, optional image generation to
`artifacts/images/`, SQLite state, audit log — but the X API is never
called and the cycle ends at `Ready`. Set `DRY_RUN=false` only with real
X credentials and the intent to publish live.

Secrets are read in one place (`app/config.py`), excluded from `repr()`,
and never appear in errors, logs, SQLite, or file names. `.env`,
`content_bot.db`, and `artifacts/` are gitignored.

## Tests and Validation Scripts

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

**433 tests passing, 99% coverage of `app/`** (the uncovered lines are
defensive branches in the Cloudflare provider). Tests replace every
external service with fakes. Three offline scripts demo the engineering
behavior end-to-end without network or credentials:

```bash
python scripts/validate_dry_run.py      # DRY_RUN cycle + SQLite durability
python scripts/validate_recovery.py     # idempotency, retries, crash recovery
python scripts/validate_image_flow.py   # image path incl. reuse and failures
```

## Status and Limitations

- Completed learning / portfolio project; not a production SaaS.
- Real X publication has not been live-validated; Cloudflare image
  generation was validated manually in `DRY_RUN`.
- Instagram and other platforms are not implemented (out of scope).
- Local single-process runtime with SQLite and local artifact storage; no
  multi-process coordination and no continuous deployment.
- No exactly-once guarantee (see Reliability).
- `Generated Image` in Notion stays empty: providers return local bytes,
  not a public URL.

## License

Released under the [MIT License](LICENSE).
