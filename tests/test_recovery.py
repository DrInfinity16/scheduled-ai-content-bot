"""Recovery: interrupted, ambiguous and failed executions after a restart."""

from types import SimpleNamespace

from app.execution_log import ExecutionLog
from app.models import ContentStatus
from app.orchestration import ContentOrchestrator
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import (
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)


def build(tmp_path, item, *, dry_run=False, generator=None, source=None):
    source = source or RecordingSource([item])
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    publisher = RecordingPublisher()
    orchestrator = ContentOrchestrator(
        generator=generator or RecordingGenerator(),
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        dry_run=dry_run,
        store=store,
    )
    return SimpleNamespace(
        item=item,
        store=store,
        source=source,
        publisher=publisher,
        orchestrator=orchestrator,
    )


# -- interrupted publication (crash between the SQLite record and X) -------


def test_interrupted_publishing_requires_manual_review(
    tmp_path, make_item, capsys
):
    harness = build(tmp_path, make_item())
    harness.store.mark_publishing("item-001", "x")  # crash happened here

    result = harness.orchestrator.run(harness.item)

    assert result.status is ContentStatus.FAILED
    assert harness.publisher.calls == []  # never republish an unknown outcome
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.MANUAL_REVIEW
    assert "revisión manual requerida" in record.last_error
    out = capsys.readouterr().out
    assert "ITEM BLOQUEADO" in out
    assert "revisión manual requerida" in out


def test_the_blocked_item_is_marked_failed_in_the_source(tmp_path, make_item):
    harness = build(tmp_path, make_item())
    harness.store.mark_publishing("item-001", "x")

    harness.orchestrator.run(harness.item)

    assert harness.source.statuses[-1] is ContentStatus.FAILED
    assert "revisión manual" in harness.source.last["error"]


def test_manual_review_survives_new_runs_and_restarts(tmp_path, make_item):
    harness = build(tmp_path, make_item())
    harness.store.mark_publishing("item-001", "x")
    harness.orchestrator.run(harness.item)
    assert harness.publisher.calls == []

    # Another run on the same process...
    harness.orchestrator.run(make_item())
    # ...and a run after a full restart (new store over the same file).
    harness.store.close()
    harness.store = ExecutionStore(str(tmp_path / "content_bot.db"))
    harness.orchestrator.store = harness.store
    harness.orchestrator.run(make_item())

    assert harness.publisher.calls == []
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.MANUAL_REVIEW


# -- interrupted / failed earlier phases -----------------------------------


def test_interrupted_generation_is_safe_to_rerun(tmp_path, make_item):
    harness = build(tmp_path, make_item())
    harness.store.mark_generating("item-001", "x")  # crash during generation

    result = harness.orchestrator.run(make_item())

    assert result.ok
    assert len(harness.publisher.calls) == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.attempt_count == 1


def test_a_failed_attempt_can_run_again_and_then_publish(
    tmp_path, make_item
):
    broken = RecordingGenerator(error=RuntimeError("gemini cayó"))
    harness = build(tmp_path, make_item(), generator=broken)

    first = harness.orchestrator.run(harness.item)
    assert first.status is ContentStatus.FAILED
    assert harness.store.get_execution("item-001", "x").attempt_count == 1

    harness.orchestrator.generator = RecordingGenerator()
    result = harness.orchestrator.run(make_item())

    assert result.ok
    assert len(harness.publisher.calls) == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.attempt_count == 2
    assert record.last_error is None


def test_simulated_then_real_mode_publishes_once(tmp_path, make_item):
    # A DRY RUN left a `simulated` row; the operator then enables real mode.
    harness = build(tmp_path, make_item())
    harness.store.mark_generating("item-001", "x")
    harness.store.mark_simulated("item-001", "x")

    result = harness.orchestrator.run(make_item())  # dry_run=False

    assert result.ok
    assert len(harness.publisher.calls) == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.external_post_id == "1"


def test_dry_run_after_simulated_stays_simulated(tmp_path, make_item):
    harness = build(tmp_path, make_item(), dry_run=True)
    harness.store.mark_generating("item-001", "x")
    harness.store.mark_simulated("item-001", "x")

    result = harness.orchestrator.run(make_item())

    assert result.status is ContentStatus.READY
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SIMULATED
    assert record.external_post_id is None


# -- reconciliation ---------------------------------------------------------


class MissingPageSource(RecordingSource):
    def get_item(self, item_id):
        return None


class BrokenReadSource(RecordingSource):
    def get_item(self, item_id):
        raise RuntimeError("Notion 503 simulado")


def pending_store(tmp_path, external_post_id="7"):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_publishing("item-001", "x")
    store.mark_published("item-001", "x", external_post_id=external_post_id)
    return store


def test_reconcile_updates_the_source_and_never_publishes(
    tmp_path, make_item
):
    item = make_item(status=ContentStatus.PUBLISHING)
    store = pending_store(tmp_path)
    source = RecordingSource([item])
    publisher = RecordingPublisher()
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        store=store,
    )

    reconciled = orchestrator.reconcile_pending_syncs()

    assert reconciled == ["item-001"]
    assert publisher.calls == []
    assert source.items["item-001"].status is ContentStatus.PUBLISHED
    assert store.pending_syncs() == []


def test_reconcile_without_store_or_source_is_a_noop(tmp_path):
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=RecordingPublisher(),
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
    )

    assert orchestrator.reconcile_pending_syncs() == []


def test_reconcile_tolerates_a_missing_page(tmp_path, make_item, capsys):
    store = pending_store(tmp_path)
    source = MissingPageSource([])
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=RecordingPublisher(),
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        store=store,
    )

    assert orchestrator.reconcile_pending_syncs() == []
    assert "ya no existe" in capsys.readouterr().out
    assert len(store.pending_syncs()) == 1  # keeps waiting, never forgets


def test_reconcile_tolerates_a_source_read_error(tmp_path, capsys):
    store = pending_store(tmp_path)
    source = BrokenReadSource([])
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=RecordingPublisher(),
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        store=store,
    )

    assert orchestrator.reconcile_pending_syncs() == []
    assert "no se pudo leer" in capsys.readouterr().out
    assert len(store.pending_syncs()) == 1


def test_reconcile_still_failing_keeps_the_sync_pending(
    tmp_path, make_item, capsys
):
    item = make_item()
    store = pending_store(tmp_path)
    source = RecordingSource([item], fail_on={"item-001"})
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=RecordingPublisher(),
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        store=store,
    )

    assert orchestrator.reconcile_pending_syncs() == []
    assert "Reconciliación pendiente" in capsys.readouterr().out
    assert len(store.pending_syncs()) == 1
