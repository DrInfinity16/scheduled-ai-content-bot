"""Durable idempotency: a known publication is never executed twice."""

import json

import pytest

from app.execution_log import ExecutionLog
from app.models import ContentStatus, PublishResult
from app.orchestration import ContentOrchestrator
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import RecordingGenerator, RecordingPublisher, RecordingSource


class Harness:
    """One full workflow stack over one SQLite file (no network anywhere)."""

    def __init__(self, tmp_path, item, *, dry_run=False, db_name="content_bot.db"):
        self.item = item
        self.source = RecordingSource([item])
        self.generator = RecordingGenerator()
        self.publisher = RecordingPublisher()
        self.log_path = str(tmp_path / "posts.jsonl")
        self.db_path = str(tmp_path / db_name)
        self.store = ExecutionStore(self.db_path)
        self.orchestrator = self._new_orchestrator(dry_run)

    def _new_orchestrator(self, dry_run):
        return ContentOrchestrator(
            generator=self.generator,
            publisher=self.publisher,
            audit_log=ExecutionLog(self.log_path),
            source=self.source,
            dry_run=dry_run,
            store=self.store,
        )

    def restart(self, *, dry_run=False):
        """Simulates a process restart: new store, new orchestrator, same file."""
        self.store.close()
        self.store = ExecutionStore(self.db_path)
        self.orchestrator = self._new_orchestrator(dry_run)
        return self.orchestrator

    @property
    def publisher_calls(self):
        return len(self.publisher.calls)

    def entries(self):
        with open(self.log_path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def test_first_run_publishes_and_records_the_post(tmp_path, make_item):
    harness = Harness(tmp_path, make_item())

    result = harness.orchestrator.run(harness.item)

    assert result.ok
    assert harness.publisher_calls == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.external_post_id == "1"
    assert record.notion_sync_pending is False
    assert harness.source.statuses[-1] is ContentStatus.PUBLISHED


def test_second_run_never_republishes(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)
    updates_before = len(harness.source.updates)

    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 1
    assert result.status is ContentStatus.PUBLISHED
    assert result.publish_result.id == "1"
    assert "ya publicado" in capsys.readouterr().out
    # Source already correct: no redundant write-back either.
    assert len(harness.source.updates) == updates_before
    assert [e["status"] for e in harness.entries()].count("duplicate_blocked") == 1


def test_restart_does_not_republish(tmp_path, make_item):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)

    orchestrator = harness.restart()
    item = make_item()  # a fresh object, as Notion would deliver it
    result = orchestrator.run(item)

    assert harness.publisher_calls == 1
    assert result.status is ContentStatus.PUBLISHED
    assert harness.store.get_execution("item-001", "x").external_post_id == "1"


def test_edited_source_content_still_blocks(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)

    edited = make_item(generated_content="texto distinto editado en Notion")
    result = harness.orchestrator.run(edited)

    assert harness.publisher_calls == 1
    assert result.status is ContentStatus.PUBLISHED
    assert "cambió desde la publicación" in capsys.readouterr().out


def test_stale_source_status_is_reconciled_not_republished(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)
    # Notion manually reverted to Scheduled after the publication.
    harness.source.items["item-001"].status = ContentStatus.SCHEDULED

    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 1
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert result.status is ContentStatus.PUBLISHED
    assert (
        harness.store.get_execution("item-001", "x").notion_sync_pending is False
    )


def test_pending_sync_record_reposts_nothing_and_syncs(tmp_path, make_item):
    harness = Harness(tmp_path, make_item())
    harness.store.mark_published("item-001", "x", external_post_id="9")
    harness.store.mark_sync_pending("item-001", "x", error="notion caído")
    harness.source.items["item-001"].status = ContentStatus.PUBLISHING

    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 0
    assert result.status is ContentStatus.PUBLISHED
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert harness.store.pending_syncs() == []


def test_unreadable_store_fails_conservatively(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())
    harness.store.close()  # every read now raises

    result = harness.orchestrator.run(harness.item)

    assert result.status is ContentStatus.FAILED
    assert harness.publisher_calls == 0
    assert "no se pudo leer el estado técnico" in result.error
    assert "store_error" in [e["status"] for e in harness.entries()]
    assert "no se pudo leer" in capsys.readouterr().out


def test_identity_is_item_id_plus_platform(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)

    # Same id on another platform must NOT match the known publication:
    # it falls through the gate (and fails later at platform validation).
    other_platform = make_item(platform="instagram")
    result = harness.orchestrator.run(other_platform)

    out = capsys.readouterr().out
    assert "ya publicado" not in out
    assert "plataforma no soportada" in out
    assert result.status is ContentStatus.FAILED
    assert harness.publisher_calls == 1
    assert harness.store.get_execution("item-001", "x").external_post_id == "1"
    other_record = harness.store.get_execution("item-001", "instagram")
    assert other_record.external_post_id is None
    assert other_record.execution_status is ExecutionStatus.FAILED


def test_duplicate_audit_entry_carries_the_external_id(tmp_path, make_item):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)
    harness.orchestrator.run(harness.item)

    blocked = [
        e
        for e in harness.entries()
        if e["status"] == "duplicate_blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0]["external_post_id"] == "1"
    assert blocked[0]["workflow_status"] == "published"


def test_without_store_the_gate_is_skipped(tmp_path, make_item):
    # Legacy behaviour (no store wired) still works, for transition safety.
    generator = RecordingGenerator()
    publisher = RecordingPublisher()
    source = RecordingSource([make_item()])
    orchestrator = ContentOrchestrator(
        generator=generator,
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
    )

    orchestrator.run(make_item())
    orchestrator.run(make_item())

    assert len(publisher.calls) == 2


def test_published_family_statuses_are_protected_from_stale_rows(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item())
    harness.store.mark_manual_review("item-001", "x", error="interrumpido")

    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 0
    assert result.status is ContentStatus.FAILED
    assert (
        harness.store.get_execution("item-001", "x").execution_status
        is ExecutionStatus.MANUAL_REVIEW
    )
