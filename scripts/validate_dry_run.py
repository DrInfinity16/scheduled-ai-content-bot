"""Controlled DRY_RUN validation (Pass 2 → extended in Pass 3).

Runs the *production* classes — Config, NotionContentSource, GeminiGenerator,
XPublisher, ExecutionLog, ContentOrchestrator, ContentScheduler,
ExecutionStore — against in-memory transports (fake Notion gateway, fake
Gemini, fake Tweepy). No network call is made and nothing is published.

What it proves: discovery → parse → generate → write-back → transitions →
no publish → final Ready → audit entry → no re-processing on the next cycle →
durable SQLite state (simulated row, content hash, survives reopen).

This script never needs real credentials: every transport below is fake, so
no NOTION_TOKEN / GEMINI_API_KEY value from ``.env`` is ever read or sent.
Live validation against the real APIs remains a manual operator step.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):  # Windows cp1252 consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Config  # noqa: E402
from app.execution_log import ExecutionLog  # noqa: E402
from app.generator import GeminiGenerator  # noqa: E402
from app.models import ContentStatus  # noqa: E402
from app.orchestration import ContentOrchestrator  # noqa: E402
from app.publishers import XCredentials, create_publisher  # noqa: E402
from app.scheduler import ContentScheduler  # noqa: E402
from app.sources import NotionContentSource  # noqa: E402
from app.storage import ExecutionStatus, ExecutionStore  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeGeminiClient,
    FakeNotionGateway,
    FakeTweepyClient,
    make_notion_page,
)

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
COPY = "Python: usa dataclasses para config inmutable. #python"

checkpoints: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checkpoints.append((name, bool(ok), detail))


def main() -> int:
    # 1. Configuración real, en modo simulación, con credenciales de Notion
    #    inventadas sólo para pasar la validación: el gateway es falso.
    config = Config(
        gemini_api_key="gemini-fake",
        model="gemini-2.0-flash",
        dry_run=True,
        x_api_key="k",
        x_api_secret="s",
        x_access_token="t",
        x_access_secret="ts",
        notion_token="secret_fake_token",
        notion_database_id="db-0000",
        content_source="notion",
        poll_interval_seconds=60,
        x_username="ana_dev",
    ).validate()

    due_page = make_notion_page(
        page_id="page-0001",
        topic="Dataclasses para configuración",
        scheduled_at="2026-10-05T09:00:00.000+00:00",
        status="Scheduled",
    )
    future_page = make_notion_page(
        page_id="page-0002",
        topic="Post futuro (no debe procesarse)",
        scheduled_at="2026-12-01T09:00:00.000+00:00",
        status="Scheduled",
    )
    gateway = FakeNotionGateway(pages=[due_page, future_page])
    source = NotionContentSource(config.notion_token, config.notion_database_id,
                                 gateway=gateway)
    source.validate_schema()

    gemini = FakeGeminiClient(text=COPY)
    generator = GeminiGenerator(
        api_key=config.gemini_api_key, model=config.model, client=gemini
    )
    tweepy_spy = FakeTweepyClient()
    publisher = create_publisher(
        "x",
        dry_run=config.dry_run,
        username=config.x_username,
        credentials=XCredentials(None, None, None, None),
        client=tweepy_spy,
    )

    with tempfile.TemporaryDirectory() as tmp:
        audit_log = ExecutionLog(str(Path(tmp) / "posts.jsonl"))
        db_path = Path(tmp) / "content_bot.db"
        store = ExecutionStore(str(db_path))
        orchestrator = ContentOrchestrator(
            generator=generator,
            publisher=publisher,
            audit_log=audit_log,
            source=source,
            dry_run=config.dry_run,
            store=store,
        )
        scheduler = ContentScheduler(
            source=source, orchestrator=orchestrator,
            poll_interval_seconds=config.poll_interval_seconds,
        )

        # Checkpoint 1: descubrimiento.
        processed = scheduler.poll_due_items(now=NOW)
        check(
            "1. Descubrimiento (poll encuentra lo vencido)",
            processed == ["page-0001"],
            f"procesados={processed}",
        )

        item = source.get_item("page-0001")

        # Checkpoint 2: parseo.
        check(
            "2. Parseo (página Notion -> ContentItem)",
            item is not None
            and item.topic == "Dataclasses para configuración"
            and item.platform == "x"
            and item.status is ContentStatus.READY,
            f"status final={item.status.value if item else 'None'}",
        )

        # Checkpoint 3: generación (Gemini real con transporte falso).
        check(
            "3. Generación (GeminiGenerator llamado una vez)",
            len(gemini.calls) == 1
            and gemini.calls[0]["contents"].find("Dataclasses") != -1,
            f"llamadas={len(gemini.calls)}",
        )

        written = [(page_id, props) for page_id, props in gateway.update_calls]
        flat = {name: prop for _, props in written for name, prop in props.items()}

        # Checkpoint 4: Generated Copy escrito en Notion.
        copy_payload = flat.get("Generated Copy") or {}
        stored_copy = "".join(
            chunk["text"]["content"] for chunk in copy_payload.get("rich_text", [])
        )
        check(
            "4. Escritura de Generated Copy en Notion",
            stored_copy == COPY,
            f"texto guardado={stored_copy[:40]!r}",
        )

        # Checkpoint 5: transiciones de estado en orden.
        status_sequence = [
            props["Status"]["status"]["name"]
            for _, props in written
            if "Status" in props
        ]
        check(
            "5. Transiciones (Generating -> Ready, sin Publishing)",
            status_sequence[:2] == ["Generating", "Ready"]
            and "Publishing" not in status_sequence
            and "Published" not in status_sequence,
            f"secuencia={status_sequence}",
        )

        # Checkpoint 6: ninguna llamada real a X.
        check(
            "6. Sin publicación real (cero create_tweet)",
            tweepy_spy.calls == [],
            f"create_tweet={len(tweepy_spy.calls)}",
        )

        # Checkpoint 7: estado final Ready, jamás Published, sin URL falsa.
        check(
            "7. Estado final Ready y sin Published URL",
            item is not None
            and item.status is ContentStatus.READY
            and item.published_url is None
            and "Published URL" not in flat,
            f"status={item.status.value if item else 'None'}, "
            f"published_url={getattr(item, 'published_url', None)}",
        )

        # Checkpoint 8: entrada de auditoría.
        log_path = Path(tmp) / "posts.jsonl"
        entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        last = entries[-1] if entries else {}
        check(
            "8. Auditoría (simulated / ready / sin post id)",
            last.get("status") == "simulated"
            and last.get("workflow_status") == "ready"
            and last.get("external_post_id") is None
            and last.get("published_url") is None,
            f"entry={last}",
        )

        # Checkpoint 9: el siguiente ciclo no reprocesa el item.
        second_cycle = scheduler.poll_due_items(now=NOW)
        check(
            "9. Segundo ciclo no reprocesa (status ya no es Scheduled)",
            second_cycle == [],
            f"procesados={second_cycle}",
        )

        # -- Pass 3: estado técnico durable en SQLite ----------------------

        # Checkpoint 10: la ejecución quedó registrada como simulada.
        record = store.get_execution("page-0001", "x")
        check(
            "10. SQLite registra la ejecución como simulated",
            db_path.exists()
            and record is not None
            and record.execution_status is ExecutionStatus.SIMULATED
            and record.external_post_id is None
            and record.attempt_count == 1,
            f"status={record.execution_status.value if record else 'None'}, "
            f"post_id={record.external_post_id if record else 'None'}, "
            f"intentos={record.attempt_count if record else 'None'}",
        )

        # Checkpoint 11: hash defensivo + snapshot del texto generado.
        check(
            "11. content_hash SHA-256 y snapshot del copy guardados",
            record is not None
            and bool(record.content_hash)
            and len(record.content_hash) == 64
            and record.generated_content == COPY,
            f"hash={record.content_hash[:12] + '...' if record else 'None'}",
        )

        # Checkpoint 12: el estado sobrevive a cerrar y reabrir el archivo
        # (simulación de reinicio: el orquestador queda apuntando al nuevo).
        store.close()
        reopened = ExecutionStore(str(db_path))
        orchestrator.store = reopened
        reopened_record = reopened.get_execution("page-0001", "x")
        check(
            "12. Estado durable (reabrir el archivo conserva la fila)",
            reopened_record is not None
            and reopened_record.execution_status is ExecutionStatus.SIMULATED
            and reopened.pending_syncs() == [],
            f"status={reopened_record.execution_status.value if reopened_record else 'None'}, "
            f"pendientes={len(reopened.pending_syncs())}",
        )

        # Checkpoint 13: reprogramar una simulated vuelve a simular
        # (Case C intencional: el operador re-lanza el item), sin post real.
        gateway.pages["page-0001"]["properties"]["Status"]["status"]["name"] = (
            "Scheduled"
        )
        third_cycle = scheduler.poll_due_items(now=NOW)
        reopened_record = reopened.get_execution("page-0001", "x")
        check(
            "13. Re-ejecución segura: sigue simulated, cero create_tweet",
            third_cycle == ["page-0001"]
            and reopened_record is not None
            and reopened_record.execution_status is ExecutionStatus.SIMULATED
            and reopened_record.attempt_count == 2
            and tweepy_spy.calls == [],
            f"procesados={third_cycle}, "
            f"intentos={reopened_record.attempt_count if reopened_record else 'None'}, "
            f"create_tweet={len(tweepy_spy.calls)}",
        )
        reopened.close()

    width = max(len(name) for name, _, _ in checkpoints)
    all_ok = True
    print("\n=== Validación controlada DRY_RUN ===\n")
    for name, ok, detail in checkpoints:
        mark = "OK  " if ok else "FALLA"
        print(f"  [{mark}] {name.ljust(width)}  {detail}")
        all_ok = all_ok and ok

    print(
        "\n  Nota: validación con transportes falsos (sin red). No se usó "
        "NOTION_TOKEN / GEMINI_API_KEY reales ni se publicó en X.\n"
    )
    print("  RESULTADO:", "TODOS LOS PUNTOS OK" if all_ok else "HAY FALLOS")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
