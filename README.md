# Project 11 — Scheduled AI Content Bot

A scheduled AI content automation bot that turns a Notion editorial calendar
into published posts: APScheduler polls Notion, Gemini writes the copy,
an optional image is generated with Cloudflare FLUX (or Google), and an X
publisher abstraction posts the result — with durable SQLite execution
state, bounded retries, recovery, and a JSONL audit trail.

**Status:** completed learning / portfolio project. It is not a production
SaaS and is not deployed anywhere.

---

## Why I Built It

The learning objective was the hard part of automation, not the happy path:

- integrating several external APIs behind clean abstractions (Notion, Gemini,
  Cloudflare Workers AI, X)
- scheduling and polling a remote editorial source
- LLM output that is validated before it is ever published
- **durable** automation: idempotency, bounded retries, crash recovery
- honest failure handling (`sync_pending`, `manual_review`) instead of
  optimistic "it probably worked"
- a publisher abstraction designed to survive a second platform later

## Features

**Implemented**

- Notion editorial calendar as the content source (plus a local
  `calendar.yaml` mode)
- scheduled polling with APScheduler (one polling job, per-cycle dedupe)
- Gemini-generated copy with validation
- optional image generation: Cloudflare Workers AI
  (`@cf/black-forest-labs/flux-1-schnell`) or Google Gemini as an
  alternative provider
- local image artifact storage (`artifacts/images/`)
- X text/image publisher abstraction (text post, or text + one image)
- `DRY_RUN` mode that simulates the whole cycle without touching X
- SQLite durable technical state: idempotency gate, retries, recovery
- `sync_pending` reconciliation and conservative `manual_review`
- JSONL audit logging (append-only trace)
- 433 automated tests, ~99% coverage of `app/`

**Validated**

- full test suite and the three validation scripts run green (no network)
- real Cloudflare image generation manually validated in `DRY_RUN`

**Not yet live-validated**

- real X publication (credentials/flow implemented, never exercised against
  the live API in this project)

**Not implemented (out of scope by design)**

- Instagram or any other social platform
- deployment, Docker, web UI, multi-user support

## Architecture

```
Notion editorial calendar
        ↓  Status=Scheduled and Scheduled At <= now
APScheduler polling (1 job every CONTENT_POLL_INTERVAL_SECONDS)
        ↓  1) reconcile pending syncs   2) due items
Content orchestrator (SQLite idempotency gate first)
        ├── Gemini text generator (bounded retries)
        ├── Image generator [optional, Generate Image = true]
        │     ├── Cloudflare Workers AI (FLUX.1 Schnell)
        │     └── Google image provider
        ├── Validator (deterministic rules, incl. image limits)
        └── X publisher (text, or text + uploaded media)
              ↓  external post ID persisted BEFORE final Notion sync
        SQLite durable technical state (content_bot.db)
              ↓  mark_synced | mark_sync_pending → next-cycle reconcile
        Notion synchronization

JSONL (log/posts.jsonl) → append-only audit trail
```

**Source-of-truth boundaries**

| Store | Owns |
| --- | --- |
| Notion | editorial state (topic, schedule, status, published URL) |
| SQLite | durable technical state (idempotency, retries, media/image metadata) |
| JSONL | audit history (one line per attempt, never used for decisions) |

Notion can say `Scheduled` forever; if SQLite already recorded the
publication, the item is never published twice.

## Reliability Design

- **Durable identity:** one row per `content_item_id + platform`. A
  `published` / `sync_pending` / `manual_review` row blocks any new
  publication of that item, whatever Notion says.
- **Ordered publication:** `mark_publishing` → publish on X →
  `mark_published` in SQLite (with the external post ID) → only then
  `Status = Published` in Notion. If the final Notion write fails, the next
  cycle retries **only** the sync — never a second post or a second media
  upload.
- **`sync_pending` reconciliation:** every polling cycle first settles
  outstanding Notion writes.
- **Bounded retries:** transient failures (network, 408/425/429/5xx) use
  exponential backoff up to `RETRY_MAX_ATTEMPTS`; permanent failures
  surface immediately.
- **Ambiguous outcome ⇒ human:** a crash between `publishing` and the X
  response escalates to `manual_review` (terminal) instead of risking a
  duplicate.

**Guarantee wording:** *best-effort at-most-once publication with durable
idempotency safeguards.* The system explicitly does **not** claim
exactly-once delivery — that is impossible in general across an external
API and a local database.

## Image Generation

- Items opt in with the Notion checkbox **`Generate Image`**; an optional
  **`Image Brief`** gives visual direction (otherwise the prompt is derived
  from topic + angle + copy).
- Provider selection: `IMAGE_PROVIDER=cloudflare` (default model
  `@cf/black-forest-labs/flux-1-schnell`) or `IMAGE_PROVIDER=google`
  (model comes from `IMAGE_MODEL`, no default).
- Generated images are stored locally in `artifacts/images/` (gitignored);
  path, SHA-256 and provider metadata live in SQLite.
- A prompt fingerprint allows the same image to be reused instead of
  regenerated.
- If an image is requested and generation fails (after retries), the
  workflow fails loudly — there is **no** silent fallback to text-only.
- Under `DRY_RUN=true` images may be genuinely generated and validated
  locally, but nothing is ever uploaded or published.

## Notion Database Schema

Create the database by hand and share it with your Notion integration;
the app validates the schema at startup and names any missing property.

**Required**

| Property | Type | Role |
| --- | --- | --- |
| `Topic` | Title | post topic |
| `Angle` | Rich text | angle / viewpoint |
| `Scheduled At` | Date | when it should go out (UTC) |
| `Platform` | Select | `x` |
| `Status` | Status or Select | lifecycle (see below) |

**Optional** (missing ones are reported and skipped)

| Property | Type |
| --- | --- |
| `References` | Rich text |
| `Reference Notes` | Rich text |
| `Generate Image` | Checkbox |
| `Image Brief` | Rich text |
| `Generated Copy` | Rich text |
| `Generated Image` | URL |
| `Published URL` | URL |
| `Error` | Rich text |

## Status Flow

```
Scheduled → Generating → Ready → Publishing → Published
                ↓           ↓
             Failed       Failed   (any error, with Error text)
```

- `DRY_RUN=true` ends at **`Ready`**: no `Publishing`, no `Published`, no
  `Published URL`.
- Status values are matched case-insensitively (`draft`, `scheduled`,
  `generating`, `ready`, `publishing`, `published`, `failed`); unknown
  values are an explicit error, never invented.
- `Generated Image` stays empty on purpose: the provider returns local
  bytes, not a public URL, and the app does not write local paths into a
  URL field.

## Setup

Python 3.14 (the version used for development and CI).

```bash
git clone <repo-url>
cd <repo-directory>

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

Configuration errors (missing Gemini key, incomplete Notion credentials,
unsupported image provider, missing Cloudflare credentials) are reported as
short messages at startup, not as stack traces.

## Configuration

All variables read by the code (see `.env.example`):

**Base**

| Variable | Description |
| --- | --- |
| `GEMINI_API_KEY` | required; the app refuses to start without it |
| `MODEL` | Gemini text model (default `gemini-2.0-flash`) |
| `DRY_RUN` | `true` (default) simulates; `false` publishes for real |
| `CONTENT_SOURCE` | `auto` (default) \| `notion` \| `yaml` |
| `CALENDAR_FILE` | yaml calendar path (default `calendar.yaml`) |
| `LOG_FILE` | audit log path (default `log/posts.jsonl`) |

**Notion** (required when `CONTENT_SOURCE=notion`; both or neither in `auto`)

| Variable | Description |
| --- | --- |
| `NOTION_TOKEN` | integration token |
| `NOTION_DATABASE_ID` | editorial database id |
| `CONTENT_POLL_INTERVAL_SECONDS` | polling cadence (default `60`) |

**Image provider**

| Variable | Description |
| --- | --- |
| `IMAGE_PROVIDER` | `google` (code default) \| `cloudflare` |
| `IMAGE_MODEL` | model for the selected provider (no default for Google; Cloudflare defaults to `@cf/black-forest-labs/flux-1-schnell`) |
| `IMAGE_OUTPUT_DIR` | artifact directory (default `artifacts/images`) |
| `CLOUDFLARE_ACCOUNT_ID` | required when `IMAGE_PROVIDER=cloudflare` |
| `CLOUDFLARE_API_TOKEN` | required when `IMAGE_PROVIDER=cloudflare` (Workers AI permission) |

**X** (required only when `DRY_RUN=false`)

| Variable | Description |
| --- | --- |
| `X_USERNAME` | handle without `@`; builds the `Published URL` |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | OAuth 1.0a credentials |

**Reliability**

| Variable | Description |
| --- | --- |
| `DATABASE_PATH` | SQLite state file (default `content_bot.db`) |
| `RETRY_MAX_ATTEMPTS` | total attempts per transient operation (default `3`) |
| `RETRY_BASE_DELAY_SECONDS` | initial backoff delay (default `1.0`) |

Secrets are read in exactly one place (`app/config.py`), are excluded from
`repr()`, and never appear in errors, logs, SQLite or file names.
`.env`, `content_bot.db` and `artifacts/` are gitignored.

## DRY_RUN

**New users should start with `DRY_RUN=true`** (the default):

- reads the editorial source and runs the full cycle: validation, Gemini
  copy, optional real image generation to `artifacts/images/`, SQLite state
  and audit logging
- ends at `Status = Ready` with a `simulated` technical row
- **never** calls the X API, never uploads media, never writes a
  `Published URL`

Set `DRY_RUN=false` only when you have real X credentials and deliberately
want live publication.

## Tests

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

Current state: **433 tests passing, 99% coverage of `app/`** (the uncovered
lines are defensive branches in the Cloudflare provider). No test touches
Gemini, X, Notion or any other external service — SDKs, Tweepy, media
upload and image generation are replaced with fakes; SQLite and artifacts
use temporary directories.

## Validation Scripts

Run without network and without real credentials:

```bash
python scripts/validate_dry_run.py      # full DRY_RUN cycle + SQLite durability
python scripts/validate_recovery.py     # idempotency, retries, crash recovery
python scripts/validate_image_flow.py   # image path A–E, incl. reuse and failure
```

Each prints a checklist and ends with `RESULTADO: TODOS LOS PUNTOS OK`.

## Project Status

This is a **completed learning/portfolio project** for the current scope.
Optional future work (not requirements of the completed project):

- live validation against real X credentials
- an Instagram publisher reusing the existing publisher abstraction
- deployment / continuous operation

## Known Limitations

- local, single-process runtime; SQLite and local files only — no
  multi-process coordination
- no exactly-once guarantee (best-effort at-most-once with durable
  idempotency safeguards)
- real X publication has not been live-validated
- Instagram is not implemented
- no continuous cloud deployment
- `Generated Image` in Notion stays empty (local bytes, no public URL)
- Cloudflare returns JPEG only; no conversion/resizing (no Pillow dependency)
- an uploaded media with a failed tweet is left as documented orphans on X

## Repository Layout

```
app/
  config.py            # .env loading + validation (single source of env vars)
  models.py            # ContentItem, ContentStatus, results
  sources/             # ContentSource abstraction, Notion client/source, YAML
  generator.py         # Gemini text generation with bounded retries
  images/              # provider abstraction, FLUX/Google, artifacts, prompts
  publishers/          # PlatformPublisher abstraction + X implementation
  orchestration.py     # gate → text → image? → publish → sync
  scheduler.py         # polling + reconciliation job
  storage.py           # SQLite execution store (idempotency state)
  retry.py             # retry policy + transient classification
  validation.py        # deterministic validation
  execution_log.py     # JSONL audit trail
main.py                # composition root
calendar.yaml          # local yaml source (CONTENT_SOURCE=yaml)
scripts/               # offline validation scripts
tests/                 # pytest suite (no external calls)
.env.example           # placeholders only
PASS2_REPORT.md        # engineering history (pass 2)
PASS3_REPORT.md        # engineering history (pass 3)
PASS4_REPORT.md        # engineering history (pass 4)
```

## License

Released under the [MIT License](LICENSE).
