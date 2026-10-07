"""Controlled image-flow validation (Pass 4).

Proves the image path with the *production* classes and in-memory
transports (no network, nothing published to the real X):

  A. image generation + media upload + tweet + Notion sync succeed.
  B. image generation transiently fails twice, then succeeds (real
     provider retry path with a fake Gemini transport).
  C. permanent media-upload failure: no tweet is created.
  D. tweet succeeds but the final Notion sync fails: the next run
     reconciles without uploading media or creating another tweet.
  E. DRY_RUN: the image is generated locally, X is never touched.

Every transport below is fake, so no credential from ``.env`` is ever read
or sent; live validation against the real Notion/Gemini/X APIs remains a
manual operator step.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):  # Windows cp1252 consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.execution_log import ExecutionLog  # noqa: E402
from app.generator import GeminiGenerator  # noqa: E402
from app.images import GoogleImageProvider, ImageArtifactStore  # noqa: E402
from app.models import ContentStatus  # noqa: E402
from app.orchestration import ContentOrchestrator  # noqa: E402
from app.publishers import XCredentials, create_publisher  # noqa: E402
from app.retry import RetryPolicy  # noqa: E402
from app.scheduler import ContentScheduler  # noqa: E402
from app.sources import NotionContentSource  # noqa: E402
from app.storage import ExecutionStatus, ExecutionStore  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeGeminiClient,
    FakeImageGenerator,
    FakeMediaUploader,
    FakeNotionGateway,
    FakeTweepyClient,
    MINIMAL_PNG,
    make_notion_page,
)

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
COPY = "Un pie de foto corto y directo. #python"
FAST = RetryPolicy(max_attempts=3, base_delay=0)

checkpoints: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checkpoints.append((name, bool(ok), detail))


class FlakyWritebackSource(NotionContentSource):
    """Notion source whose final Published write-back can be made to fail."""

    def __init__(self, *args, reject_published: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.reject_published = reject_published
        self.rejected_published = 0

    def update_item(self, item_id, **changes):
        if self.reject_published and changes.get("status") is (
            ContentStatus.PUBLISHED
        ):
            self.rejected_published += 1
            raise RuntimeError("Notion 500 simulado en la escritura final")
        return super().update_item(item_id, **changes)


class FlakyGeminiImageTransport:
    """Fake genai client: fails image renders N times, then returns bytes."""

    def __init__(self, failures: int = 0, error=None):
        self.failures = failures
        self.error = error or ConnectionError("se cayó la red")
        self.calls: list = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents):
        self.calls.append({"model": model, "contents": contents})
        if len(self.calls) <= self.failures:
            raise self.error
        part = SimpleNamespace(
            inline_data=SimpleNamespace(data=MINIMAL_PNG, mime_type="image/png")
        )
        content = SimpleNamespace(parts=[part])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def image_page(page_id, brief="Un zorro geométrico al atardecer"):
    from tests.conftest import _rich

    return make_notion_page(
        page_id=page_id,
        topic=f"Post {page_id}",
        scheduled_at="2026-10-05T09:00:00.000+00:00",
        status="Scheduled",
        properties={
            "Generate Image": {
                "id": "H",
                "type": "checkbox",
                "checkbox": True,
            },
            "Image Brief": {
                "id": "I",
                "type": "rich_text",
                "rich_text": _rich(brief),
            },
        },
    )


def build(tmp, name, *, tweepy=None, uploader=None, image_generator=None,
          image_model="modelo-falso", reject_published=False, dry_run=False):
    db_path = str(Path(tmp) / f"{name}.db")
    page = image_page(f"page-{name}")
    gateway = FakeNotionGateway(pages=[page])
    source = FlakyWritebackSource(
        "secret_fake_token", "db-0000", gateway=gateway,
        reject_published=reject_published,
    )
    source.validate_schema()
    generator = GeminiGenerator(
        api_key="gemini-fake", model="m", client=FakeGeminiClient(text=COPY)
    )
    artifacts = ImageArtifactStore(str(Path(tmp) / name / "images"))
    if image_generator is None:
        image_generator = FakeImageGenerator(artifacts=artifacts)
    tweepy = tweepy or FakeTweepyClient(post_id="424242")
    uploader = uploader or FakeMediaUploader(media_id="999")
    publisher = create_publisher(
        "x",
        dry_run=dry_run,
        username="ana_dev",
        credentials=XCredentials("k", "s", "t", "ts"),
        client=tweepy,
        retry_policy=FAST,
        media_uploader=uploader,
    )
    store = ExecutionStore(db_path)
    orchestrator = ContentOrchestrator(
        generator=generator,
        publisher=publisher,
        audit_log=ExecutionLog(str(Path(tmp) / f"{name}.jsonl")),
        source=source,
        dry_run=dry_run,
        store=store,
        image_generator=image_generator,
        image_model=image_model,
    )
    scheduler = ContentScheduler(
        source=source, orchestrator=orchestrator, poll_interval_seconds=60
    )
    return SimpleNamespace(
        gateway=gateway,
        source=source,
        store=store,
        orchestrator=orchestrator,
        scheduler=scheduler,
        tweepy=tweepy,
        uploader=uploader,
        image_generator=image_generator,
        page_id=f"page-{name}",
    )


def poll(world):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        processed = world.scheduler.poll_due_items(now=NOW)
    return processed, buffer.getvalue()


def generated_image_writes(world) -> list:
    return [
        props.get("Generated Image")
        for _, props in world.gateway.update_calls
        if "Generated Image" in props
    ]


def scenario_a(tmp) -> None:
    world = build(tmp, "a")

    processed, _ = poll(world)
    record = world.store.get_execution(world.page_id, "x")

    check(
        "A.1 Flujo completo con imagen: texto + media publicados",
        processed == [world.page_id]
        and len(world.uploader.calls) == 1
        and world.tweepy.media_ids_calls == [["999"]]
        and record.execution_status is ExecutionStatus.PUBLISHED
        and record.external_post_id == "424242"
        and record.media_id == "999",
        f"uploads={len(world.uploader.calls)}, "
        f"tweets={len(world.tweepy.calls)}, "
        f"status={record.execution_status.value if record else 'None'}",
    )
    check(
        "A.2 SQLite guarda metadatos de imagen y Notion queda Published",
        record.generated_image_path is not None
        and record.generated_image_hash is not None
        and len(record.visual_fingerprint or "") == 64
        and world.source.get_item(world.page_id).status is ContentStatus.PUBLISHED
        and generated_image_writes(world) == [],
        f"path={record.generated_image_path}",
    )
    world.store.close()


def scenario_b(tmp) -> None:
    transport = FlakyGeminiImageTransport(failures=2)
    artifacts = ImageArtifactStore(str(Path(tmp) / "b" / "images"))
    provider = GoogleImageProvider(
        api_key="fake",
        model="modelo-falso",
        artifacts=artifacts,
        client=transport,
        retry_policy=FAST,
    )
    world = build(tmp, "b", image_generator=provider)

    processed, out = poll(world)
    record = world.store.get_execution(world.page_id, "x")

    check(
        "B.1 Dos fallos transitorios de imagen: 3 renders y éxito",
        processed == [world.page_id]
        and len(transport.calls) == 3
        and "reintento" in out
        and record.execution_status is ExecutionStatus.PUBLISHED
        and record.image_provider == "google",
        f"renders={len(transport.calls)}, "
        f"status={record.execution_status.value if record else 'None'}",
    )
    world.store.close()


def scenario_c(tmp) -> None:
    uploader = FakeMediaUploader(error=RuntimeError("413 demasiado grande"))
    world = build(tmp, "c", uploader=uploader)

    processed, _ = poll(world)
    record = world.store.get_execution(world.page_id, "x")

    check(
        "C.1 Subida permanente fallida: cero tweets, workflow failed",
        processed == [world.page_id]
        and len(uploader.calls) == 1
        and world.tweepy.calls == []
        and record.execution_status is ExecutionStatus.FAILED
        and record.external_post_id is None
        and record.generated_image_path is not None,
        f"uploads={len(uploader.calls)}, tweets={len(world.tweepy.calls)}",
    )
    world.store.close()


def scenario_d(tmp) -> None:
    world = build(tmp, "d", reject_published=True)

    processed1, out1 = poll(world)
    record1 = world.store.get_execution(world.page_id, "x")
    check(
        "D.1 Tweet con imagen ok + Notion falla: sync_pending con media",
        processed1 == [world.page_id]
        and len(world.uploader.calls) == 1
        and len(world.tweepy.calls) == 1
        and record1.execution_status is ExecutionStatus.SYNC_PENDING
        and record1.external_post_id == "424242"
        and record1.media_id == "999"
        and "NO se reintentará la publicación" in out1,
        f"uploads={len(world.uploader.calls)}, tweets={len(world.tweepy.calls)}",
    )

    # Notion still down: reconcile only — no new upload, no new tweet.
    _, _ = poll(world)
    check(
        "D.2 Segundo ciclo: sólo reintenta la sincronización",
        len(world.uploader.calls) == 1
        and len(world.tweepy.calls) == 1
        and world.source.rejected_published == 2
        and world.store.get_execution(
            world.page_id, "x"
        ).execution_status is ExecutionStatus.SYNC_PENDING,
        f"uploads={len(world.uploader.calls)}, tweets={len(world.tweepy.calls)}",
    )

    world.source.reject_published = False
    _, _ = poll(world)
    check(
        "D.3 Notion sana: converge sin republicar nada",
        len(world.uploader.calls) == 1
        and len(world.tweepy.calls) == 1
        and world.source.get_item(world.page_id).status is ContentStatus.PUBLISHED
        and world.store.pending_syncs() == [],
        f"uploads={len(world.uploader.calls)}, tweets={len(world.tweepy.calls)}",
    )
    world.store.close()


def scenario_e(tmp) -> None:
    world = build(tmp, "e", dry_run=True)

    processed, _ = poll(world)
    record = world.store.get_execution(world.page_id, "x")

    check(
        "E.1 DRY_RUN genera la imagen local sin tocar X",
        processed == [world.page_id]
        and len(world.image_generator.calls) == 1
        and record.execution_status is ExecutionStatus.SIMULATED
        and record.generated_image_path is not None
        and record.external_post_id is None
        and record.media_id is None
        and world.uploader.calls == []
        and world.tweepy.calls == []
        and world.source.get_item(world.page_id).status is ContentStatus.READY,
        f"status={record.execution_status.value if record else 'None'}, "
        f"uploads={len(world.uploader.calls)}, tweets={len(world.tweepy.calls)}",
    )
    world.store.close()


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        scenario_a(tmp)
        scenario_b(tmp)
        scenario_c(tmp)
        scenario_d(tmp)
        scenario_e(tmp)

    width = max(len(name) for name, _, _ in checkpoints)
    all_ok = True
    print("\n=== Validación controlada del flujo con imagen (Pass 4) ===\n")
    for name, ok, detail in checkpoints:
        mark = "OK  " if ok else "FALLA"
        print(f"  [{mark}] {name.ljust(width)}  {detail}")
        all_ok = all_ok and ok

    print(
        "\n  Nota: transportes falsos (fake Notion / Gemini / Tweepy / "
        "media). Sin red, sin publicaciones reales y sin usar "
        "credenciales de .env.\n"
    )
    print("  RESULTADO:", "TODOS LOS PUNTOS OK" if all_ok else "HAY FALLOS")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
