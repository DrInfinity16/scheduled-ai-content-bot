"""Controlled reliability validation (Pass 3).

Proves the crash-recovery guarantees with the *production* classes and
in-memory transports (no network, nothing published to the real X):

  S1. X succeeds but the final Notion update fails:
      - SQLite records the publication BEFORE Notion is touched
      - the failure is loud, the row stays sync_pending
      - later cycles retry only the sync, never the publish
      - after a process restart the publication is still known
  S2. A crash between the SQLite "publishing" record and the X response
      forces manual review: nothing is ever republished automatically.
  S3. Transient X failures are retried with bounded backoff (3 attempts).
  S4. Permanent X failures are NOT retried (1 attempt) and are recorded.

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
from app.models import ContentStatus  # noqa: E402
from app.orchestration import ContentOrchestrator  # noqa: E402
from app.publishers import XCredentials, create_publisher  # noqa: E402
from app.retry import RetryPolicy  # noqa: E402
from app.scheduler import ContentScheduler  # noqa: E402
from app.sources import NotionContentSource  # noqa: E402
from app.storage import ExecutionStatus, ExecutionStore  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeGeminiClient,
    FakeNotionGateway,
    make_notion_page,
)

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
COPY = "Publicar es fácil cuando el estado es durable. #python"
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


class FlakyTweepy:
    """Fake tweepy.Client that fails the first N calls, then succeeds."""

    def __init__(self, post_id="777001", failures=0, make_exc=None):
        self.post_id = post_id
        self.failures = failures
        self.make_exc = make_exc or (lambda: ConnectionError("se cayó la red"))
        self.calls: list[str] = []

    def create_tweet(self, text: str):
        self.calls.append(text)
        if len(self.calls) <= self.failures:
            raise self.make_exc()
        return SimpleNamespace(data={"id": self.post_id})


def build(tmp, name, page_id, *, tweepy, reject_published=False):
    db_path = str(Path(tmp) / f"{name}.db")
    page = make_notion_page(
        page_id=page_id,
        topic=f"Post {page_id}",
        scheduled_at="2026-10-05T09:00:00.000+00:00",
        status="Scheduled",
    )
    gateway = FakeNotionGateway(pages=[page])
    source = FlakyWritebackSource(
        "secret_fake_token",
        "db-0000",
        gateway=gateway,
        reject_published=reject_published,
    )
    source.validate_schema()
    generator = GeminiGenerator(
        api_key="gemini-fake",
        model="gemini-2.0-flash",
        client=FakeGeminiClient(text=COPY),
    )
    publisher = create_publisher(
        "x",
        dry_run=False,
        username="ana_dev",
        credentials=XCredentials("k", "s", "t", "ts"),
        client=tweepy,
        retry_policy=FAST,
    )
    store = ExecutionStore(db_path)
    orchestrator = ContentOrchestrator(
        generator=generator,
        publisher=publisher,
        audit_log=ExecutionLog(str(Path(tmp) / f"{name}.jsonl")),
        source=source,
        dry_run=False,
        store=store,
    )
    scheduler = ContentScheduler(
        source=source,
        orchestrator=orchestrator,
        poll_interval_seconds=60,
    )
    return SimpleNamespace(
        gateway=gateway,
        source=source,
        store=store,
        orchestrator=orchestrator,
        scheduler=scheduler,
        tweepy=tweepy,
        db_path=db_path,
        page_id=page_id,
    )


def status_writes(world) -> list[str]:
    return [
        props["Status"]["status"]["name"]
        for _, props in world.gateway.update_calls
        if "Status" in props
    ]


def poll(world) -> tuple[list[str], str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        processed = world.scheduler.poll_due_items(now=NOW)
    return processed, buffer.getvalue()


def scenario_1(tmp) -> None:
    tweepy = FlakyTweepy(post_id="777001")
    world = build(tmp, "s1", "page-0001", tweepy=tweepy, reject_published=True)

    # -- cycle 1: X ok, final Notion sync fails --------------------------
    processed, out1 = poll(world)
    record = world.store.get_execution("page-0001", "x")
    check(
        "S1.1 X publicó y SQLite lo registró ANTES de tocar Notion",
        processed == ["page-0001"]
        and len(tweepy.calls) == 1
        and record is not None
        and record.execution_status is ExecutionStatus.SYNC_PENDING
        and record.external_post_id == "777001"
        and record.notion_sync_pending is True,
        f"tweets={len(tweepy.calls)}, "
        f"status={record.execution_status.value if record else 'None'}, "
        f"post_id={record.external_post_id if record else 'None'}",
    )
    check(
        "S1.2 La escritura final a Notion falló en forma visible",
        "NO se reintentará la publicación" in out1
        and "Published" not in status_writes(world)
        and world.source.rejected_published == 1,
        f"rechazos={world.source.rejected_published}",
    )

    # -- cycle 2: Notion still broken: sync retried, publish never --------
    processed2, _ = poll(world)
    check(
        "S1.3 Ciclo 2: sólo reintenta la sincronización (cero posts nuevos)",
        processed2 == []  # la página ya no está Scheduled: sólo reconcile
        and len(tweepy.calls) == 1
        and world.source.rejected_published == 2
        and world.store.get_execution(
            "page-0001", "x"
        ).execution_status is ExecutionStatus.SYNC_PENDING,
        f"tweets={len(tweepy.calls)}, rechazos={world.source.rejected_published}",
    )

    # -- cycle 3: Notion heals, reconciliation converges ------------------
    world.source.reject_published = False
    processed3, _ = poll(world)
    record3 = world.store.get_execution("page-0001", "x")
    check(
        "S1.4 Notion sana: reconcile publica el estado sin repetir el post",
        processed3 == []
        and len(tweepy.calls) == 1
        and "Published" in status_writes(world)
        and record3.execution_status is ExecutionStatus.PUBLISHED
        and record3.notion_sync_pending is False
        and world.store.pending_syncs() == [],
        f"tweets={len(tweepy.calls)}, "
        f"status={record3.execution_status.value if record3 else 'None'}",
    )

    # -- restart: new store over the same file, page scheduled again ------
    world.store.close()
    world.store = ExecutionStore(world.db_path)
    world.orchestrator.store = world.store
    world.gateway.pages[world.page_id]["properties"]["Status"]["status"][
        "name"
    ] = "Scheduled"
    processed4, out4 = poll(world)
    check(
        "S1.5 Reinicio + reprogramación: el gate bloquea el duplicado",
        processed4 == [world.page_id]
        and len(tweepy.calls) == 1
        and "ya publicado" in out4
        and world.store.get_execution(
            world.page_id, "x"
        ).external_post_id
        == "777001"
        and world.source.get_item(world.page_id).status is ContentStatus.PUBLISHED,
        f"tweets={len(tweepy.calls)}",
    )
    world.store.close()


def scenario_2(tmp) -> None:
    tweepy = FlakyTweepy(post_id="999")
    world = build(tmp, "s2", "page-0010", tweepy=tweepy)
    # Crash happened right after the publishing record was written.
    world.store.mark_publishing("page-0010", "x")

    processed1, out1 = poll(world)
    record1 = world.store.get_execution("page-0010", "x")
    check(
        "S2.1 Publicación interrumpida → revisión manual, sin republicar",
        processed1 == ["page-0010"]
        and tweepy.calls == []
        and record1.execution_status is ExecutionStatus.MANUAL_REVIEW
        and "revisión manual requerida" in (record1.last_error or ""),
        f"status={record1.execution_status.value}, tweets={len(tweepy.calls)}",
    )
    check(
        "S2.2 El ítem queda bloqueado y marcado Failed en Notion",
        "ITEM BLOQUEADO" in out1
        and world.source.get_item('page-0010').status is ContentStatus.FAILED,
        f"status_fuente={world.source.get_item('page-0010').status.value}",
    )

    # Reprogramming it later must still never publish automatically.
    world.gateway.pages["page-0010"]["properties"]["Status"]["status"][
        "name"
    ] = "Scheduled"
    processed2, _ = poll(world)
    check(
        "S2.3 Segunda ejecución (tras reprogramar) sigue bloqueada",
        processed2 == ["page-0010"]
        and tweepy.calls == []
        and world.store.get_execution(
            "page-0010", "x"
        ).execution_status is ExecutionStatus.MANUAL_REVIEW,
        f"tweets={len(tweepy.calls)}",
    )
    world.store.close()


def scenario_3(tmp) -> None:
    tweepy = FlakyTweepy(post_id="777003", failures=2)
    world = build(tmp, "s3", "page-0020", tweepy=tweepy)

    processed, out = poll(world)
    record = world.store.get_execution("page-0020", "x")

    check(
        "S3.1 Fallos transitorios: 3 intentos acotados y publicación ok",
        processed == ["page-0020"]
        and len(tweepy.calls) == 3
        and "reintento" in out
        and record.execution_status is ExecutionStatus.PUBLISHED
        and record.external_post_id == "777003",
        f"intentos_create_tweet={len(tweepy.calls)}, "
        f"status={record.execution_status.value}",
    )
    world.store.close()


def scenario_4(tmp) -> None:
    tweepy = FlakyTweepy(
        post_id="0", failures=99, make_exc=lambda: RuntimeError("401 malformado")
    )
    world = build(tmp, "s4", "page-0030", tweepy=tweepy)

    processed, out = poll(world)
    record = world.store.get_execution("page-0030", "x")

    check(
        "S4.1 Fallo permanente: un solo intento y estado failed durable",
        processed == ["page-0030"]
        and len(tweepy.calls) == 1
        and "reintento" not in out
        and record.execution_status is ExecutionStatus.FAILED
        and record.external_post_id is None
        and world.source.get_item('page-0030').status is ContentStatus.FAILED,
        f"intentos_create_tweet={len(tweepy.calls)}, "
        f"status={record.execution_status.value}",
    )
    world.store.close()


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        scenario_1(tmp)
        scenario_2(tmp)
        scenario_3(tmp)
        scenario_4(tmp)

    width = max(len(name) for name, _, _ in checkpoints)
    all_ok = True
    print("\n=== Validación controlada de recuperación (Pass 3) ===\n")
    for name, ok, detail in checkpoints:
        mark = "OK  " if ok else "FALLA"
        print(f"  [{mark}] {name.ljust(width)}  {detail}")
        all_ok = all_ok and ok

    print(
        "\n  Nota: transportes falsos (fake Notion / Gemini / Tweepy). "
        "Sin red, sin publicaciones reales y sin usar credenciales "
        "de .env.\n"
    )
    print("  RESULTADO:", "TODOS LOS PUNTOS OK" if all_ok else "HAY FALLOS")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
