"""X succeeded but the final Notion update failed: never publish twice."""

import json

from app.execution_log import ExecutionLog
from app.models import ContentStatus, PublishResult
from app.orchestration import ContentOrchestrator
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import (
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)


class SyncBreakingSource(RecordingSource):
    """Notion rejects exactly the final Published write-back (500)."""

    def __init__(self, items=(), broken=True):
        super().__init__(items)
        self.broken = broken
        self.attempts_to_publish_sync = 0

    def update_item(self, item_id, **changes):
        if changes.get("status") is ContentStatus.PUBLISHED:
            self.attempts_to_publish_sync += 1
            if self.broken:
                raise RuntimeError("Notion devolvió un error simulado (500)")
        return super().update_item(item_id, **changes)


class OrderObservingSource(SyncBreakingSource):
    """Captures the SQLite row at the exact moment the Notion update fires."""

    def __init__(self, store, items=(), broken=True):
        super().__init__(items, broken=broken)
        self.store = store
        self.external_post_id_at_sync = []

    def update_item(self, item_id, **changes):
        if changes.get("status") is ContentStatus.PUBLISHED:
            record = self.store.get_execution(item_id, "x")
            self.external_post_id_at_sync.append(
                record.external_post_id if record else None
            )
        return super().update_item(item_id, **changes)


class Harness:
    def __init__(self, tmp_path, item, *, broken=True, publisher=None):
        self.item = item
        self.store = ExecutionStore(str(tmp_path / "content_bot.db"))
        self.source = SyncBreakingSource([item], broken=broken)
        self.source.store = self.store
        self.publisher = publisher or RecordingPublisher()
        self.log_path = tmp_path / "posts.jsonl"
        self.orchestrator = ContentOrchestrator(
            generator=RecordingGenerator(),
            publisher=self.publisher,
            audit_log=ExecutionLog(str(self.log_path)),
            source=self.source,
            dry_run=False,
            store=self.store,
        )

    @property
    def publisher_calls(self):
        return len(self.publisher.calls)

    def entries(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def test_publication_survives_a_failed_final_sync(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())

    result = harness.orchestrator.run(harness.item)

    # The real X post happened and the workflow reports it as such.
    assert result.ok
    assert harness.publisher_calls == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SYNC_PENDING
    assert record.external_post_id == "1"
    assert record.notion_sync_pending is True
    # Notion never received Published.
    assert ContentStatus.PUBLISHED not in harness.source.statuses
    out = capsys.readouterr().out
    assert "NO se reintentará la publicación" in out
    assert any(e["status"] == "sync_error" for e in harness.entries())


def test_sqlite_is_written_before_notion_is_touched(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(), broken=False)
    source = OrderObservingSource(harness.store, [make_item()], broken=True)
    harness.source = source
    harness.orchestrator.source = source

    harness.orchestrator.run(harness.item)

    # When the Notion update was attempted, SQLite already knew the post id.
    assert source.external_post_id_at_sync == ["1"]
    assert harness.publisher_calls == 1


def test_next_cycle_retries_only_the_sync(tmp_path, make_item, capsys):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)  # publish ok, sync broken
    capsys.readouterr()

    # Notion is still down for the Published write: run again.
    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 1  # no second X post
    assert result.status is ContentStatus.PUBLISHED
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SYNC_PENDING
    assert ContentStatus.PUBLISHED not in harness.source.statuses


def test_healed_source_converges_without_republishing(tmp_path, make_item):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)  # publish ok, sync broken
    harness.source.broken = False

    result = harness.orchestrator.run(harness.item)

    assert harness.publisher_calls == 1
    assert result.status is ContentStatus.PUBLISHED
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.notion_sync_pending is False
    assert harness.store.pending_syncs() == []


def test_restart_between_failure_and_heal_never_republishes(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)  # publish ok, sync broken

    # Process restart: brand-new store/handlers over the same SQLite file.
    harness.store.close()
    harness.store = ExecutionStore(str(tmp_path / "content_bot.db"))
    harness.source.store = harness.store
    harness.orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=harness.publisher,
        audit_log=ExecutionLog(str(harness.log_path)),
        source=harness.source,
        dry_run=False,
        store=harness.store,
    )
    harness.source.broken = False

    item = make_item()  # fresh object, as the poller would deliver it
    result = harness.orchestrator.run(item)

    assert harness.publisher_calls == 1
    assert result.status is ContentStatus.PUBLISHED
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert harness.store.pending_syncs() == []


def test_reconcile_heals_the_source_without_running_the_workflow(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item())
    harness.orchestrator.run(harness.item)  # publish ok, sync broken
    harness.source.broken = False

    reconciled = harness.orchestrator.reconcile_pending_syncs()

    assert reconciled == ["item-001"]
    assert harness.publisher_calls == 1
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert harness.store.pending_syncs() == []
    assert any(e["status"] == "reconciled" for e in harness.entries())


def test_stale_publishing_status_in_notion_heals_via_reconcile(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item(), broken=False)
    harness.orchestrator.run(harness.item)
    # Crash right after X+SQLite: Notion still says Publishing.
    harness.source.items["item-001"].status = ContentStatus.PUBLISHING

    reconciled = harness.orchestrator.reconcile_pending_syncs()

    # pending_syncs is empty (already synced), so reconciliation is a no-op;
    # the poll cycle heals this case through the idempotency gate instead.
    assert reconciled == []
    result = harness.orchestrator.run(make_item())
    assert harness.publisher_calls == 1
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert result.status is ContentStatus.PUBLISHED


def test_publisher_error_path_still_marks_failed(tmp_path, make_item):
    failing = RecordingPublisher(
        PublishResult(status="error", error="rate limit", platform="x")
    )
    harness = Harness(tmp_path, make_item(), publisher=failing)

    result = harness.orchestrator.run(harness.item)

    assert result.status is ContentStatus.FAILED
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.FAILED
    assert record.external_post_id is None
