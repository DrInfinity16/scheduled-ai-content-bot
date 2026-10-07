"""SQLite write failures at the most dangerous moments of the workflow."""

import sqlite3

from app.execution_log import ExecutionLog
from app.models import ContentStatus
from app.orchestration import ContentOrchestrator
from app.publishers.x import XPublisher
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import (
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)


class SelectivelyBrokenStore(ExecutionStore):
    """Real SQLite store whose chosen operations raise like a full disk."""

    def __init__(self, path, fail_on=()):
        super().__init__(path)
        self.fail_on = set(fail_on)
        self.failed_calls: list[str] = []

    def _explode(self, method: str) -> None:
        if method in self.fail_on:
            self.failed_calls.append(method)
            raise sqlite3.OperationalError("disco lleno (simulado)")

    def mark_publishing(self, content_item_id, platform):
        self._explode("mark_publishing")
        return super().mark_publishing(content_item_id, platform)

    def mark_published(self, content_item_id, platform, *, external_post_id, media_id=None):
        self._explode("mark_published")
        return super().mark_published(
            content_item_id,
            platform,
            external_post_id=external_post_id,
            media_id=media_id,
        )

    def increment_attempt(self, content_item_id, platform):
        self._explode("increment_attempt")
        return super().increment_attempt(content_item_id, platform)


class BrokenAudit:
    """Audit trail that is down; the workflow must not care."""

    def record(self, *args, **kwargs):
        raise RuntimeError("audit roto")

    def record_publish(self, *args, **kwargs):
        raise RuntimeError("audit roto")


class RaisingUrlPublisher(RecordingPublisher):
    def build_post_url(self, post_id):
        raise RuntimeError("no se pudo armar la URL")


def build(tmp_path, item, *, store=None, publisher=None, audit_log=None):
    source = RecordingSource([item])
    store = store or ExecutionStore(str(tmp_path / "content_bot.db"))
    publisher = publisher or RecordingPublisher()
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=publisher,
        audit_log=audit_log or ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        dry_run=False,
        store=store,
    )
    return orchestrator, source, store, publisher


def test_failure_to_record_publishing_aborts_before_x(
    tmp_path, make_item, capsys
):
    store = SelectivelyBrokenStore(
        str(tmp_path / "content_bot.db"), fail_on={"mark_publishing"}
    )
    orchestrator, source, store, publisher = build(
        tmp_path, make_item(), store=store
    )

    result = orchestrator.run(make_item())

    assert result.status is ContentStatus.FAILED
    assert "no se pudo registrar el intento en SQLite" in result.error
    assert publisher.calls == []  # X was never asked to post
    assert store.failed_calls == ["mark_publishing"]
    out = capsys.readouterr().out
    assert "SQLite no pudo ejecutar 'mark_publishing'" in out
    assert ContentStatus.PUBLISHING not in source.statuses


def test_failure_to_persist_the_publication_still_blocks_a_republish(
    tmp_path, make_item, capsys
):
    store = SelectivelyBrokenStore(
        str(tmp_path / "content_bot.db"), fail_on={"mark_published"}
    )
    orchestrator, source, store, publisher = build(
        tmp_path, make_item(), store=store
    )

    first = orchestrator.run(make_item())

    out = capsys.readouterr().out
    assert first.status is ContentStatus.PUBLISHED
    assert "X publicó el post pero SQLite no pudo guardarlo" in out
    # Notion received the publication even though SQLite lost the write.
    assert source.items["item-001"].status is ContentStatus.PUBLISHED
    record = store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.external_post_id is None  # the id was lost with the write

    # The gate still blocks: the row says PUBLISHED even without the id.
    second = orchestrator.run(make_item())

    assert second.status is ContentStatus.PUBLISHED
    assert len(publisher.calls) == 1  # never a second X post
    assert "post externo desconocido" in capsys.readouterr().out


def test_non_critical_store_write_failure_is_logged_not_fatal(
    tmp_path, make_item, capsys
):
    store = SelectivelyBrokenStore(
        str(tmp_path / "content_bot.db"), fail_on={"increment_attempt"}
    )
    orchestrator, source, store, publisher = build(
        tmp_path, make_item(), store=store
    )

    result = orchestrator.run(make_item())

    assert result.ok
    assert len(publisher.calls) == 1
    assert "SQLite no pudo ejecutar 'increment_attempt'" in capsys.readouterr().out
    # Later writes still landed.
    assert store.get_execution("item-001", "x").external_post_id == "1"


def test_a_broken_audit_trail_never_breaks_the_duplicate_gate(
    tmp_path, make_item
):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_publishing("item-001", "x")
    store.mark_published("item-001", "x", external_post_id="7")
    item = make_item()
    orchestrator, source, store, publisher = build(
        tmp_path, item, store=store, audit_log=BrokenAudit()
    )

    result = orchestrator.run(item)

    assert result.status is ContentStatus.PUBLISHED
    assert publisher.calls == []
    assert store.pending_syncs() == []  # the sync itself completed


def test_url_rebuild_fills_the_published_url_on_reconcile(tmp_path, make_item):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_publishing("item-001", "x")
    store.mark_published("item-001", "x", external_post_id="777")
    item = make_item(status=ContentStatus.PUBLISHING)
    publisher = XPublisher(dry_run=True, username="ana")
    orchestrator, source, store, _ = build(
        tmp_path, item, store=store, publisher=publisher
    )

    reconciled = orchestrator.reconcile_pending_syncs()

    assert reconciled == ["item-001"]
    expected_url = "https://x.com/ana/status/777"
    assert source.last["published_url"] == expected_url
    assert item.published_url == expected_url
    assert store.pending_syncs() == []


def test_url_rebuild_failure_keeps_the_reconciliation_safe(
    tmp_path, make_item, capsys
):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_publishing("item-001", "x")
    store.mark_published("item-001", "x", external_post_id="777")
    item = make_item(status=ContentStatus.PUBLISHING)
    publisher = RaisingUrlPublisher()
    orchestrator, source, store, _ = build(
        tmp_path, item, store=store, publisher=publisher
    )

    reconciled = orchestrator.reconcile_pending_syncs()

    assert reconciled == ["item-001"]  # sync succeeded without a URL
    assert "published_url" not in source.last
    assert store.pending_syncs() == []
