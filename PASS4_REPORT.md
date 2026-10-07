# Project 11 — Pass 4 Image Publishing Report

---

## 1. Scope

Pass 4 adds **optional** image generation and image publishing to X on
top of the Pass 3 reliability layer, without weakening any of its
guarantees. `Generate Image = false` behaves exactly as in Pass 3;
`Generate Image = true` generates (or reuses) one local image, uploads
it to X, publishes text + media, persists the result in SQLite, and syncs
Notion.

In scope: an explicit `ImageGenerator` boundary, one real provider
(Google Gemini image models), a visual-prompt strategy (Image Brief vs.
derived), deterministic local artifact storage, additive SQLite metadata
with a safe upgrade for Pass 3 databases, image-aware idempotency and
fingerprinting, optional one-image media in `XPublisher`, DRY_RUN
semantics for the image path, conservative Notion write-back, failure
policies (no silent text-only fallback), tests, failure-injection
validation, docs and this report.

Out of scope (not implemented): Instagram/Facebook, video, multiple
images per post, carousels, image editing UI, galleries, approval
workflows, analytics, campaigns, A/B testing, web UI, auth, multi-user,
cloud deployment, queues/workers, Notion webhooks, URL scraping. The
project stays a local, single-process automation.

## 2. Architecture Changes

```
Notion Editorial Calendar
        ↓
Polling
        ↓
Orchestrator (idempotency gate first — unchanged Pass 3 rules)
        ↓
Text Generator (unchanged)
        ↓
Image Generator [optional]  ← NEW: prompt → reuse-or-generate → validate
        ↓                       metadata persisted, status untouched
Validation
        ↓
X Publisher
   ├── text-only (unchanged call shape, media=None)
   └── text + media (upload once → media_id → create_tweet)  ← NEW
        ↓  mark_published BEFORE Notion (now with media_id)
SQLite Technical State (+ image/media columns)
        ↓
Notion Synchronization (Generated Image stays empty — §13)

JSONL = audit trail (+ image/media fields, still audit-only)
```

`ContentOrchestrator` grew one focused step (`_prepare_image` delegating
to `app/images/service.py`) instead of one giant function. Pass 3
ordering (`mark_publishing` → external call → `mark_published` → Notion
sync) is preserved verbatim, with `media_id` flowing through the same
durable record.

## 3. Image Generator Abstraction

`app/images/base.py` (44 lines): `ImageGenerator` ABC with
`generate_image(visual_prompt, item, *, fingerprint, model=None) ->
GeneratedImage`, plus `ImageGenerationError`. The item scopes the
deterministic filename; the fingerprint names the exact editorial input.
No network, no storage layout and no platform knowledge leak into the
contract, so a future Instagram/Facebook publisher can reuse the same
asset untouched. The publisher contract evolved compatibly:
`PlatformPublisher.publish(item, content, media=None)` — existing
two-argument callers and doubles remain valid.

## 4. Provider Implementation

`app/images/google.py` — `GoogleImageProvider`, the single real
provider. Before writing it, the installed SDK was inspected
(`google-genai 2.28.0`): `Models.generate_images` exists but the SDK
itself marks it deprecated (removal not before 2027) in favour of
`generate_content` with image models, so the provider calls
`client.models.generate_content(model=IMAGE_MODEL,
contents=visual_prompt)` and extracts the first `inline_data` image part
(bytes or base64, MIME preserved). No model name is invented: with no
`IMAGE_MODEL` configured the provider fails fast
(`define IMAGE_MODEL en .env`) instead of guessing. Transport failures
use the shared `with_retries` (transient → bounded retries with `↻`
logging; permanent/empty → single attempt wrapped in
`ImageGenerationError`).

## 5. Visual Prompt Strategy

`app/images/prompting.py`: **Case A** — a non-empty Image Brief is the
primary direction, lightly normalized (whitespace) and never
overwritten, with a no-embedded-text clause appended unless the brief
itself asks for typography (keyword check). **Case B** — no brief: a
prompt is derived from topic, angle, generated copy (first 200 chars)
and reference notes (first 200 chars). `References` URLs are never
fetched, opened or inlined. Every prompt describes an image, not a
caption, and prefers visuals without embedded text because image models
render poor typography.

## 6. Generated Image Model / Artifact Storage

`GeneratedImage` (frozen): `local_path`, `mime_type`, `size_bytes`,
`sha256`, `provider`, `provider_reference` (model name — not a secret),
optional `width`/`height`. Only fields the pipeline actually uses.

`ImageArtifactStore(root)` (`app/images/artifacts.py`, 141 lines):
deterministic layout `<root>/<safe-item-id>_<fingerprint12>.<ext>`
(`artifacts/images/` by default), root auto-created, PNG↔`png` /
JPEG↔`jpg` mapping, filenames sanitized (`[^A-Za-z0-9_-]` → `-`, no
secrets/prompts/credentials inside). Validation before any publish:
MIME supported, file exists, non-empty, ≤ 5 MB (X limit), recorded size
matches. No Pillow was added (not installed; no resizing/conversion —
the provider must return a supported format). Binary blobs never enter
SQLite.

## 7. SQLite Changes

Five additive columns on `executions`: `generated_image_path`,
`generated_image_hash`, `visual_fingerprint`, `image_provider`,
`media_id` (`app/storage.py`). Fresh databases get them from the
schema; Pass 3 files are upgraded on open by `_ensure_columns()`
(`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` with fixed names only —
no migration framework, as Pass 3 documented). A legacy-schema test
proves old rows survive untouched. `mark_image_generated(...)` writes
metadata with `status=None` (never changes execution state);
`mark_published(..., media_id=None)` stores the media id, and a
re-affirmation without media info no longer wipes a stored `media_id`
(guarded, tested). New columns joined the write whitelist; record
mapping extended.

## 8. Image Idempotency

`prepare_item_image()` (`app/images/service.py`): items without the flag
return `None` without touching the provider. Otherwise the fingerprint
(`compute_visual_fingerprint` over brief, topic, angle, copy, notes,
prompt, model) is compared with the stored row; reuse requires **all**
of: matching fingerprint, recorded path + hash present, file validates,
and current file bytes hash to the stored value. File-existence alone
is never trusted (tampered/changed inputs regenerate). Regeneration
reasons covered by tests: missing file, hash mismatch, brief/content
change. Store read/write problems surface as `ImageGenerationError`
(explicit failure, never silent skip). Because image metadata lives in
the same durable row, restarts and re-polls reuse assets exactly like
they block duplicate posts.

## 9. X Media Upload

`tweepy 4.17.0` was inspected: the v2 `Client.create_tweet` supports
`media_ids=[...]`, but the v2 `Client` has **no** media-upload method —
upload lives on the v1.1 `API.media_upload(filename)` (same four OAuth1
credentials already in `XCredentials`). `XPublisher.publish(...,
media=None)`: validates the artifact first (invalid ⇒ error result, zero
X calls), uploads once via `with_retries` (injectable
`media_uploader`; otherwise a lazily built `tweepy.API`), then
`create_tweet(text, media_ids=[id])`. `create_tweet` retries reuse the
already-uploaded id. `PublishResult` gained `media_id`; orphaned media
(tweet fails after upload) is deliberately left on X and still reported
in the result/audit — deleting remote media would add complexity for no
safety gain. Tweepy HTTP errors already classify through the Pass 3
`response.status_code` inspection.

## 10. Orchestration Flow

`run()`: gate → validate → generate text → validate copy → **[if
`generate_image`: `_prepare_image`; any `ImageGenerationError` ⇒
`_fail`, no text-only fallback]** → `mark_publishing` (failure aborts
before X, unchanged) → `publish(item, content, media=media)` →
`_publish_failed` / `_finish_simulated` / `_finish_published`, all
media-aware (audit carries path/hash/media id; `_finish_published`
persists `media_id` in the critical SQLite write). Without an image
generator configured, image items fail explicitly. Reconcile paths never
touch media or the provider: `_already_published` and
`reconcile_pending_syncs` only write Notion state.

## 11. DRY_RUN Semantics

Authoritative and unchanged in spirit, extended honestly: text and image
**may** generate normally, the local artifact **may** be created and its
metadata stored (`simulated` row keeps path/hash/fingerprint) — but
there is **zero** media upload, zero `create_tweet`, no fabricated
`media_id`/`post_id`, empty Published URL, Notion ends `Ready`, SQLite
ends `simulated`. Re-running a simulated image item reuses the stored
artifact (provider called once). The complete generation pipeline is
validatable without touching X (scenario E, §15).

## 12. Failure / Recovery Behavior

- Image transient failure → bounded retry inside the provider; exhausted
  ⇒ workflow `failed`, text preserved in Notion, zero X calls, zero
  fallback tweet.
- Image permanent/empty failure ⇒ immediate `failed`, same guarantees.
- Media upload transient ⇒ retried (3 attempts observed); permanent ⇒
  `failed` **before any tweet** (artifact metadata retained for retry).
- Tweet fails after upload ⇒ `failed` with `media_id` audited; store
  keeps the image metadata and no `external_post_id`; orphan media
  documented, not deleted.
- Tweet + media succeed, SQLite `mark_published` fails ⇒ `AVISO
  CRÍTICO` path inherited from Pass 3 (gate still blocks on `published`
  state).
- Tweet succeeds, Notion final sync fails ⇒ `sync_pending` **with**
  `media_id`; later cycles reconcile Notion only — regression-tested to
  perform zero new uploads and zero new tweets (uploader/tweepy call
  counts frozen across cycles).
- Interrupted `publishing` without id (with or without image intent) ⇒
  terminal `manual_review`, unchanged and never auto-retried.
- Pass 3 ordering is never weakened: image support adds steps *before*
  `mark_publishing` and fields *inside* `mark_published`, never around
  the critical sequence.

## 13. Notion Image Write-Back

Conservative by design. The `Generated Image` property is type **url**
(schema-verified); the provider returns local bytes, not a public URL.
Writing a filesystem path into a URL property would be dishonest, and
the integration has no upload-to-Notion mechanism — so the orchestrator
never passes `generated_image` in write-backs (regression-asserted:
no `generated_image` key in any source update). The property stays
empty unless a future provider returns a real persistent public URL
(documented in README). The local path lives where it belongs:
SQLite technical state + audit metadata.

## 14. Tests

**417 passed, 0 failed, 1 pre-existing warning** (the Gemini SDK
DeprecationWarning from Pass 3), **100% coverage over `app/` (1626
statements)**. No test touches a real API: Gemini text/image, Tweepy
tweet/media-upload and Notion HTTP all use doubles; SQLite and artifact
files use temporary directories.

| File | Tests | Covers |
| --- | ---: | --- |
| `test_images.py` (new) | 32 | Brief vs. derived prompts (incl. typography case, no URL inlining), fingerprint stability/sensitivity, deterministic safe filenames, save/validate/hash round-trip, oversize/empty/missing/unreadable artifacts, service generate/reuse/regenerate-on-change-or-tamper, store read/write failures, Google provider success/base64/empty/no-model/transient-retry/permanent/empty-response/skip-empty-parts |
| `test_x_media.py` (new) | 18 | unchanged text-only call shape, upload-before-tweet order, media id attached, invalid/missing artifact ⇒ zero X calls, permanent (1 attempt) vs. transient (3) upload, missing media_id, orphan id in error result, tweet-retry reuses upload, DRY ignores media, credential-built uploader, registry forwarding, missing-tweepy message, dict-shaped upload responses |
| `test_image_flow.py` (new) | 12 | full text+media E2E (SQLite + Notion + audit), brief→prompt, text-only zero-image-touch, unconfigured generator, image failure ⇒ no fallback, DRY local-only + reuse, upload failure, orphan audit, critical partial failure without re-upload, restart without duplicates, media object handoff |
| `test_storage_images.py` (new) | 6 | legacy-schema upgrade without row loss, idempotent upgrade, metadata-without-status-change, media id persistence, re-affirmation no-wipe |
| `test_config.py` (+5) | 36 | `IMAGE_MODEL`/`IMAGE_OUTPUT_DIR` defaults, env loading, blank handling, rejection |
| `test_main_wiring.py` (+2) | 8 | provider + model + artifacts dir wired through `build_app` |
| Pass 3 suite (unchanged) | 342 | all green, incl. the adapted zero-delay Notion retry test |

One Pass 3 test double needed a signature update
(`SelectivelyBrokenStore.mark_published` now forwards `media_id`) —
the only existing-test change in this pass, and it caught a real drift
rather than hiding one.

## 15. Controlled Validation

`scripts/validate_image_flow.py` (new, 364 lines): production classes,
fake transports, three green suites alongside it —
`validate_dry_run.py` **13/13 OK**, `validate_recovery.py` **10/10 OK**,
`validate_image_flow.py` **8/8 OK** (all exit 0, no network, nothing
published):

- **A (2):** brief → image → upload → tweet with media → SQLite
  `published` + `media_id` → Notion `Published`; `Generated Image`
  property untouched by local paths.
- **B (1):** image transport fails twice → exactly 3 renders with
  visible retries → success via the real provider path.
- **C (1):** permanent upload failure → 1 upload, 0 tweets, workflow
  `failed`, artifact metadata retained.
- **D (3):** tweet + media succeed, final Notion sync fails →
  `sync_pending` with media id and the critical no-republish message;
  cycle 2 retries only the sync (uploads = 1, tweets = 1, rejections =
  2); healed Notion converges with zero new side effects.
- **E (1):** DRY_RUN generates locally (provider called once),
  `simulated` row with metadata, 0 uploads, 0 tweets, Notion `Ready`.

Real-credential validation was **not** performed: `.env` still contains
only placeholders (`GEMINI_API_KEY=your-gemini-api-key`,
`NOTION_TOKEN=secret_xxx`, no `IMAGE_MODEL`), and no secrets were
requested or used. The manual live procedure (one `Scheduled` test page
with `Generate Image = true` + safe brief, `DRY_RUN=true`, verify
discovery/copy/artifact/SQLite/no-X/no-repeat) is documented here for
the operator and remains the only unverified-by-execution step.

## 16. Files Added / Changed

Added: `app/images/__init__.py` (30), `base.py` (44), `models.py`
(28), `prompting.py` (102), `artifacts.py` (141), `google.py` (118),
`service.py` (109); `scripts/validate_image_flow.py` (364);
`tests/test_images.py`, `tests/test_x_media.py`,
`tests/test_image_flow.py`, `tests/test_storage_images.py`;
`PASS4_REPORT.md` (this document).

Changed: `app/models.py` (`PublishResult.media_id`),
`app/publishers/base.py` (optional `media`),
`app/publishers/__init__.py` (`media_uploader` passthrough),
`app/publishers/x.py` (media upload + tweet with media, lazy v1.1 API),
`app/orchestration.py` (image step, media-aware endings),
`app/execution_log.py` (image/media audit fields),
`app/storage.py` (5 columns, safe upgrade, `mark_image_generated`,
`mark_published(..., media_id)`), `app/config.py`
(`IMAGE_MODEL`, `IMAGE_OUTPUT_DIR`), `main.py` (image pipeline
composition + startup line), `tests/conftest.py` (media-aware
`RecordingPublisher`, `FakeTweepyClient(media_ids)`,
`FakeImageGenerator`, `FakeMediaUploader`, `MINIMAL_PNG`),
`tests/test_config.py`, `tests/test_main_wiring.py`,
`tests/test_store_failures.py` (double signature),
`.env.example` (`IMAGE_MODEL`, `IMAGE_OUTPUT_DIR`),
`.gitignore` (`artifacts/`), `README.md` (Pass 4 architecture, image
path, limits).

Final verification: `pytest` → 417 green; `pytest --cov=app` → **100%
(1626 stmts)**; all three validation scripts → `TODOS LOS PUNTOS OK`,
exit 0.

## 17. Security / Git Hygiene

- `.env`, `content_bot.db` and now `artifacts/` are gitignored;
  generated runtime files never enter version control.
- Credentials appear only in config loading (`repr=False`), request
  headers, and SDK constructors (grep-verified); never in prints, audit
  entries, SQLite rows/columns, filenames, or provider references (which
  store the model name, not keys).
- Provider/SDK errors are wrapped into `ImageGenerationError` /
  `PublishResult.error` with `str(exc)` only — no headers, no bodies
  with secrets; Notion write-back fields (`Failed` + `Error`) receive
  the same sanitized strings.
- SQL stays parameterized; new column names come from a fixed
  dictionary; hostile item ids become inert filename stems (tested).
- `IMAGE_MODEL` has **no default**: the absence of a model is an
  explicit error, never a guessed model name, so no unverified model
  string ships as a silent behavior.

## 18. Known Limitations

1. **Best-effort at-most-once, still.** The image path inherits the
   Pass 3 guarantee and its documented window (X responds, process dies
   before `mark_published` ⇒ `manual_review`, never an auto-retry).
2. Orphaned X media (upload ok, tweet failed) is accepted and audited,
   not cleaned up.
3. PNG/JPEG ≤ 5 MB only; without Pillow there is no resize/convert —
   oversized or exotic provider output fails validation explicitly.
4. `Generated Image` in Notion stays empty with byte-returning
   providers (honest limitation, §13).
5. No configurable text-only fallback on image failure (deliberate;
   future work).
6. One image per item; no second platform yet (the abstraction is ready,
   the publishers are not).
7. Additive `ADD COLUMN` is the whole migration story; multi-process
   use remains uncoordinated; live-credential validation is still the
   operator's manual step.

## 19. Recommended Next Pass

**Recommended: Pass 5 — second publisher (Instagram) reusing the image
pipeline.** It is the highest-value step the current design anticipates:
`ImageGenerator`/`GeneratedImage`/`visual_fingerprint` were built
platform-agnostic precisely for this, and the same gate → publish →
persist-before-sync ordering transfers directly.

Acceptance criteria: `SUPPORTED_PLATFORMS = {x, instagram}` with
`create_publisher("instagram")`; Instagram consumes the existing
`GeneratedImage` without regenerating; identity stays
`content_item_id + platform` (an Instagram post never blocks/matches an
X post); DRY_RUN semantics identical; media-requirements differences
(size/aspect) handled by explicit validation, not silent conversion;
full suite green with coverage kept at 100% on `app/`; fake-transport
validation extended; README + report updated. Not implemented in this
pass.
