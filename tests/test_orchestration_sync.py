"""Orchestrator ↔ content source synchronization (write-back)."""

import json

from app.execution_log import ExecutionLog
from app.generator import GenerationError
from app.models import ContentItem, ContentStatus, PublishResult
from app.orchestration import ContentOrchestrator
from app.publishers.x import XPublisher
from app.validation import X_MAX_LENGTH
from tests.conftest import (
    FakeTweepyClient,
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)


def default_item(**overrides) -> ContentItem:
    defaults = dict(
        id="item-001",
        topic="Tip de Python",
        angle="Práctico y directo",
    )
    defaults.update(overrides)
    return ContentItem(**defaults)


def build(generator=None, publisher=None, source=None, tmp_path=None, dry_run=False):
    generator = generator or RecordingGenerator()
    publisher = publisher or RecordingPublisher()
    audit_log = ExecutionLog(str(tmp_path / "posts.jsonl"))
    orchestrator = ContentOrchestrator(
        generator,
        publisher,
        audit_log,
        source=source,
        dry_run=dry_run,
    )
    return orchestrator, audit_log


def read_entries(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
    ]


# -- happy path ------------------------------------------------------------


def test_full_lifecycle_is_written_back(tmp_path):
    source = RecordingSource([default_item()])
    orchestrator, _ = build(source=source, tmp_path=tmp_path)

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.PUBLISHED
    assert source.statuses == [
        ContentStatus.GENERATING,
        ContentStatus.READY,
        ContentStatus.PUBLISHING,
        ContentStatus.PUBLISHED,
    ]
    assert source.last["error"] is None


def test_published_url_is_written_back_when_available(tmp_path):
    source = RecordingSource([default_item()])
    publisher = RecordingPublisher(
        PublishResult(status="published", id="7", url="https://x.com/ana/status/7")
    )
    orchestrator, _ = build(publisher=publisher, source=source, tmp_path=tmp_path)

    orchestrator.run(source.get_item("item-001"))

    assert source.last["status"] is ContentStatus.PUBLISHED
    assert source.last["published_url"] == "https://x.com/ana/status/7"


def test_generated_copy_is_written_back(tmp_path):
    source = RecordingSource([default_item()])
    generator = RecordingGenerator(content="copia generada")
    orchestrator, _ = build(generator=generator, source=source, tmp_path=tmp_path)

    orchestrator.run(source.get_item("item-001"))

    ready_update = source.updates[1][1]
    assert ready_update["generated_content"] == "copia generada"
    assert ready_update["status"] is ContentStatus.READY


def test_generation_failure_marks_failed_with_error(tmp_path):
    source = RecordingSource([default_item()])
    generator = RecordingGenerator(error=GenerationError("Gemini caído"))
    orchestrator, audit_log = build(
        generator=generator, source=source, tmp_path=tmp_path
    )

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.FAILED
    assert source.statuses == [ContentStatus.GENERATING, ContentStatus.FAILED]
    assert source.last["error"] == "Gemini caído"
    entry = read_entries(audit_log.path)[0]
    assert entry["error"] == "Gemini caído"


def test_validation_failure_marks_failed_with_error(tmp_path):
    source = RecordingSource([default_item()])
    generator = RecordingGenerator(content="x" * (X_MAX_LENGTH + 1))
    orchestrator, _ = build(generator=generator, source=source, tmp_path=tmp_path)

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.FAILED
    assert source.last["status"] is ContentStatus.FAILED
    assert "límite" in source.last["error"]


def test_invalid_item_fails_before_generation(tmp_path):
    source = RecordingSource([default_item(topic="")])
    generator = RecordingGenerator()
    orchestrator, _ = build(generator=generator, source=source, tmp_path=tmp_path)

    result = orchestrator.run(source.get_item("item-001"))

    assert generator.calls == []
    assert source.statuses == [ContentStatus.FAILED]
    assert "topic" in source.last["error"]


def test_publish_failure_marks_failed_with_error(tmp_path):
    source = RecordingSource([default_item()])
    publisher = RecordingPublisher(
        PublishResult(status="error", error="rate limit de X")
    )
    orchestrator, _ = build(publisher=publisher, source=source, tmp_path=tmp_path)

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.FAILED
    assert source.statuses[-1] is ContentStatus.FAILED
    assert source.last["error"] == "rate limit de X"


# -- DRY RUN ---------------------------------------------------------------


def test_dry_run_ends_at_ready_and_never_publishes(tmp_path):
    source = RecordingSource([default_item()])
    publisher = RecordingPublisher(PublishResult(status="simulated", dry_run=True))
    orchestrator, audit_log = build(
        publisher=publisher, source=source, tmp_path=tmp_path, dry_run=True
    )

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.READY
    assert result.ok
    assert ContentStatus.PUBLISHING not in source.statuses
    assert ContentStatus.PUBLISHED not in source.statuses
    assert source.last["status"] is ContentStatus.READY
    assert all("published_url" not in changes for _, changes in source.updates)
    entry = read_entries(audit_log.path)[0]
    assert entry["status"] == "simulated"
    assert entry["workflow_status"] == "ready"
    assert entry["external_post_id"] is None


def test_dry_run_with_the_real_x_publisher_never_calls_x(tmp_path):
    source = RecordingSource([default_item()])
    tweepy = FakeTweepyClient()
    orchestrator, audit_log = build(
        publisher=XPublisher(dry_run=True, client=tweepy),
        source=source,
        tmp_path=tmp_path,
        dry_run=True,
    )

    result = orchestrator.run(source.get_item("item-001"))

    assert result.status is ContentStatus.READY
    assert tweepy.calls == []
    assert source.last["status"] is ContentStatus.READY
    entry = read_entries(audit_log.path)[0]
    assert entry["status"] == "simulated"
    assert entry["workflow_status"] == "ready"


def test_dry_run_still_writes_generated_copy(tmp_path):
    source = RecordingSource([default_item()])
    generator = RecordingGenerator(content="copia en modo simulación")
    orchestrator, _ = build(
        generator=generator, source=source, tmp_path=tmp_path, dry_run=True
    )

    orchestrator.run(source.get_item("item-001"))

    item = source.get_item("item-001")
    assert item.generated_content == "copia en modo simulación"
    assert item.status is ContentStatus.READY


def test_dry_run_validation_failure_still_marks_failed(tmp_path):
    source = RecordingSource([default_item()])
    generator = RecordingGenerator(content="")
    orchestrator, _ = build(
        generator=generator, source=source, tmp_path=tmp_path, dry_run=True
    )

    result = orchestrator.run(source.get_item("item-001"))

    # Un generador vacío es un error, no una simulación exitosa.
    assert result.status is ContentStatus.FAILED
    assert source.last["status"] is ContentStatus.FAILED


def test_dry_run_never_reports_a_publication_even_if_the_publisher_claims_one(
    tmp_path
):
    source = RecordingSource([default_item()])
    publisher = RecordingPublisher(
        PublishResult(status="published", id="9", url="https://x.com/u/status/9")
    )
    orchestrator, audit_log = build(
        publisher=publisher, source=source, tmp_path=tmp_path, dry_run=True
    )

    result = orchestrator.run(source.get_item("item-001"))

    # El resultado del publisher se corrige: DRY_RUN no afirma publicaciones.
    assert result.status is ContentStatus.READY
    assert result.publish_result.status == "simulated"
    assert result.publish_result.url is None
    assert result.publish_result.id is None
    assert source.last["status"] is not ContentStatus.PUBLISHED
    assert all("published_url" not in changes for _, changes in source.updates)

    entry = read_entries(audit_log.path)[0]
    assert entry["status"] == "simulated"
    assert entry["workflow_status"] == "ready"
    assert entry["external_post_id"] is None


def test_a_broken_sync_audit_entry_never_breaks_the_workflow(tmp_path):
    item = default_item()
    source = RecordingSource([item], fail_on={item.id})
    orchestrator, audit_log = build(source=source, tmp_path=tmp_path)
    original = audit_log.record
    attempts = []

    def exploding_record(*args, **kwargs):
        if kwargs.get("publish_status") == "sync_error":
            attempts.append(kwargs)
            raise RuntimeError("disco lleno")
        return original(*args, **kwargs)

    audit_log.record = exploding_record

    result = orchestrator.run(item)

    assert result.status is ContentStatus.PUBLISHED
    assert attempts, "el intento de registrar el fallo de sync sí ocurrió"


# -- write-back failures ---------------------------------------------------


def test_write_back_failure_never_retries_the_publish(tmp_path, capsys):
    item = default_item()
    source = RecordingSource([item], fail_on={item.id})
    publisher = RecordingPublisher(
        PublishResult(status="published", id="7", url="https://x.com/u/status/7")
    )
    orchestrator, audit_log = build(
        publisher=publisher, source=source, tmp_path=tmp_path
    )

    result = orchestrator.run(item)

    # La publicación ocurre una sola vez aunque la sincronización falle.
    assert len(publisher.calls) == 1
    assert result.status is ContentStatus.PUBLISHED
    output = capsys.readouterr().out
    assert "AVISO" in output
    assert "NO se reintentará" in output

    entries = read_entries(audit_log.path)
    assert any(entry["status"] == "sync_error" for entry in entries)
    assert any(
        "sincronización fallida" in (entry["error"] or "") for entry in entries
    )


def test_write_back_failure_is_logged_not_raised(tmp_path, capsys):
    item = default_item()
    source = RecordingSource([item], fail_on={item.id})
    orchestrator, _ = build(source=source, tmp_path=tmp_path)

    result = orchestrator.run(item)

    assert result.status is ContentStatus.PUBLISHED
    assert "AVISO" in capsys.readouterr().out


def test_source_none_keeps_the_original_behaviour(tmp_path):
    orchestrator, audit_log = build(tmp_path=tmp_path)

    result = orchestrator.run(default_item())

    assert result.ok
    assert read_entries(audit_log.path)[0]["workflow_status"] == "published"
