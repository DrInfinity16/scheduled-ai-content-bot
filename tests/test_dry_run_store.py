"""DRY RUN combined with the durable technical state."""

import json

from app.execution_log import ExecutionLog
from app.models import ContentStatus
from app.orchestration import ContentOrchestrator
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import (
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)


def build(tmp_path, item, *, dry_run=True):
    source = RecordingSource([item])
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    publisher = RecordingPublisher()
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        dry_run=dry_run,
        store=store,
    )
    return orchestrator, source, store, publisher, tmp_path / "posts.jsonl"


def test_dry_run_records_a_simulated_execution(tmp_path, make_item):
    orchestrator, source, store, publisher, log_path = build(
        tmp_path, make_item()
    )

    result = orchestrator.run(make_item())

    assert result.status is ContentStatus.READY
    assert result.publish_result.status == "simulated"
    record = store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SIMULATED
    assert record.external_post_id is None
    assert record.notion_sync_pending is False
    # Notion never sees Publishing/Published in dry run.
    assert source.statuses == [ContentStatus.GENERATING, ContentStatus.READY]
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["status"] == "simulated"
    assert entries[-1]["external_post_id"] is None


def test_dry_run_never_claims_a_publication(tmp_path, make_item):
    orchestrator, _, store, _, _ = build(tmp_path, make_item())

    result = orchestrator.run(make_item())

    assert result.ok  # READY counts as success, PUBLISHED would too...
    assert result.status is not ContentStatus.PUBLISHED
    assert store.get_execution("item-001", "x").external_post_id is None


def test_repeated_dry_runs_stay_simulated_and_never_block(
    tmp_path, make_item
):
    orchestrator, _, store, publisher, _ = build(tmp_path, make_item())

    orchestrator.run(make_item())
    result = orchestrator.run(make_item())  # Scheduled again next cycle

    assert result.status is ContentStatus.READY
    record = store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SIMULATED
    assert record.external_post_id is None
    assert record.attempt_count == 2
    # The publisher is invoked (it simulates internally) but never for real.
    assert len(publisher.calls) == 2
    assert store.pending_syncs() == []


def test_dry_run_publishing_failure_is_reported_as_failed(
    tmp_path, make_item
):
    from app.models import PublishResult

    orchestrator, source, store, _, _ = build(tmp_path, make_item())
    orchestrator.publisher = RecordingPublisher(
        PublishResult(status="error", error="algo falló", platform="x")
    )

    result = orchestrator.run(make_item())

    assert result.status is ContentStatus.FAILED
    assert store.get_execution("item-001", "x").execution_status is (
        ExecutionStatus.FAILED
    )
    assert source.last["status"] is ContentStatus.FAILED
