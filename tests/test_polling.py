"""Polling: APScheduler triggers one query per cycle, never one job per page."""

from datetime import datetime, timezone

from app.scheduler import POLL_JOB_ID, ContentScheduler
from app.sources import NotionContentSource
from tests.conftest import (
    FailingOrchestrator,
    FakeNotionGateway,
    RecordingSource,
    make_notion_page,
)


def build(source_or_items, orchestrator=None, **kwargs):
    from apscheduler.schedulers.background import BackgroundScheduler

    if isinstance(source_or_items, list):
        source = RecordingSource(source_or_items)
    else:
        source = source_or_items
    orchestrator = orchestrator or FailingOrchestrator()
    return ContentScheduler(source, orchestrator, BackgroundScheduler(), **kwargs)


def make_item(item_id="item-001", hour=9, **overrides):
    from app.models import ContentItem
    from datetime import datetime as dt

    defaults = dict(
        id=item_id,
        topic="Tema",
        angle="Ángulo",
        scheduled_at=dt(2026, 10, 5, hour, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return ContentItem(**defaults)


# -- job registration ------------------------------------------------------


def test_add_polling_job_creates_a_single_interval_job():
    scheduler = build([])

    job_id = scheduler.add_polling_job(45)

    assert job_id == POLL_JOB_ID
    assert scheduler.job_ids == [POLL_JOB_ID]
    job = scheduler.scheduler.get_jobs()[0]
    assert job.trigger.interval.total_seconds() == 45
    assert job.max_instances == 1


def test_polling_job_uses_configured_interval_by_default():
    scheduler = build([], poll_interval_seconds=120)

    scheduler.add_polling_job()

    job = scheduler.scheduler.get_jobs()[0]
    assert job.trigger.interval.total_seconds() == 120


def test_no_job_is_created_per_content_item():
    scheduler = build([make_item("a"), make_item("b")])

    scheduler.add_polling_job(60)

    assert scheduler.job_ids == [POLL_JOB_ID]  # not ['a', 'b']


def test_polling_job_can_be_replaced_idempotently():
    from apscheduler.schedulers.background import BackgroundScheduler

    running = BackgroundScheduler()
    running.start()
    try:
        scheduler = ContentScheduler(RecordingSource([]), FailingOrchestrator(), running)
        scheduler.add_polling_job(3600)
        scheduler.add_polling_job(3600)

        assert scheduler.job_ids == [POLL_JOB_ID]
    finally:
        running.shutdown(wait=False)


# -- due selection ---------------------------------------------------------


def notion_scheduler(pages, orchestrator=None):
    gateway = FakeNotionGateway(pages=pages)
    source = NotionContentSource("token", "db-0000", gateway=gateway)
    return build(source, orchestrator), gateway


def test_only_due_scheduled_items_are_processed():
    due = make_notion_page(page_id="due", scheduled_at="2020-01-01T09:00:00Z")
    future = make_notion_page(page_id="future", scheduled_at="2999-01-01T09:00:00Z")
    draft = make_notion_page(page_id="draft", status="Draft")
    ready = make_notion_page(page_id="ready", status="Ready")
    scheduler, gateway = notion_scheduler([due, future, draft, ready])

    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )

    assert processed == ["due"]
    clause = gateway.query_calls[-1]["filter"]["and"][1]
    assert clause["date"]["on_or_before"] == "2026-10-05T00:00:00Z"


def test_future_items_are_not_processed():
    future = make_notion_page(page_id="future", scheduled_at="2999-01-01T09:00:00Z")
    scheduler, _ = notion_scheduler([future])

    assert scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc)) == []


def test_draft_items_are_ignored():
    draft = make_notion_page(page_id="draft", status="Draft")
    scheduler, _ = notion_scheduler([draft])

    assert scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc)) == []


def test_scheduled_item_without_date_is_not_due():
    undated = make_notion_page(page_id="undated", scheduled_at=None)
    scheduler, _ = notion_scheduler([undated])

    assert scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc)) == []


def test_items_are_resolved_from_the_source_before_running():
    due = make_notion_page(page_id="due", scheduled_at="2020-01-01T09:00:00Z")
    scheduler, _ = notion_scheduler([due])

    scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc))

    orchestrator = scheduler.orchestrator
    assert orchestrator.runs[0].id == "due"
    assert orchestrator.runs[0].topic == "Tip de Python"


# -- resilience ------------------------------------------------------------


def test_one_failing_item_does_not_stop_the_cycle(capsys):
    items = [make_item("a"), make_item("b"), make_item("c")]
    orchestrator = FailingOrchestrator(failing_ids={"b"})
    scheduler = build(items, orchestrator)

    processed = scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc))

    assert processed == ["a", "c"]
    assert [item.id for item in orchestrator.runs] == ["a", "b", "c"]
    assert "b" in capsys.readouterr().out


def test_duplicate_page_in_the_same_poll_runs_once():
    item = make_item("dup")
    orchestrator = FailingOrchestrator()

    class DuplicatedSource(RecordingSource):
        def get_scheduled_items(self, start=None, end=None):
            return [item, item, item]

    scheduler = build(DuplicatedSource(), orchestrator)

    processed = scheduler.poll_due_items(now=datetime(2026, 10, 5, tzinfo=timezone.utc))

    assert processed == ["dup"]
    assert len(orchestrator.runs) == 1


def test_source_failure_propagates():
    from app.sources.base import ContentSourceError

    class BrokenSource(RecordingSource):
        def get_scheduled_items(self, start=None, end=None):
            raise ContentSourceError("Notion no responde")

    scheduler = build(BrokenSource(), FailingOrchestrator())

    try:
        scheduler.poll_due_items()
    except ContentSourceError as exc:
        assert "Notion no responde" in str(exc)
    else:
        raise AssertionError("se esperaba ContentSourceError")


def test_legacy_schedule_items_still_works_with_the_yaml_path(calendar_path):
    from app.sources import YAMLContentSource

    source = YAMLContentSource(str(calendar_path))
    scheduler = build(source, FailingOrchestrator())

    job_ids = scheduler.schedule_items()

    assert job_ids == ["calendar-000", "calendar-001", "calendar-002"]
    assert POLL_JOB_ID not in job_ids


# -- reconciliation sweep ---------------------------------------------------


class HookOrchestrator:
    """Orchestrator double exposing the reconcile hook used by the poller."""

    def __init__(self, result=None, error=None):
        self.events = []
        self.runs = []
        self.result = result
        self.error = error

    def reconcile_pending_syncs(self):
        self.events.append("reconcile")
        if self.error is not None:
            raise self.error
        return self.result or []

    def run(self, item):
        self.events.append(f"run:{item.id}")
        self.runs.append(item)
        return item


def test_poll_reconciles_before_running_due_items():
    orchestrator = HookOrchestrator(result=["previo"])
    scheduler = build([make_item("a")], orchestrator)

    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )

    assert processed == ["a"]
    assert orchestrator.events == ["reconcile", "run:a"]


def test_reconciliation_runs_even_with_no_due_items():
    orchestrator = HookOrchestrator()
    scheduler = build([], orchestrator)

    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )

    assert processed == []
    assert orchestrator.events == ["reconcile"]


def test_reconciliation_failure_does_not_break_the_cycle(capsys):
    orchestrator = HookOrchestrator(error=RuntimeError("sqlite bloqueado"))
    scheduler = build([make_item("a")], orchestrator)

    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )

    assert processed == ["a"]
    assert "La reconciliación" in capsys.readouterr().out


def test_orchestrators_without_the_hook_are_skipped():
    scheduler = build([make_item("a")], FailingOrchestrator())

    assert scheduler.reconcile_pending_syncs() == []
    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )
    assert processed == ["a"]


def test_full_stack_poll_heals_a_pending_sync_without_publishing(tmp_path):
    from app.execution_log import ExecutionLog
    from app.models import ContentStatus
    from app.orchestration import ContentOrchestrator
    from app.storage import ExecutionStore
    from tests.conftest import RecordingGenerator, RecordingPublisher

    item = make_item("stale", status=ContentStatus.PUBLISHING)
    source = RecordingSource([item])
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_publishing("stale", "x")
    store.mark_published("stale", "x", external_post_id="5")
    publisher = RecordingPublisher()
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        store=store,
    )
    scheduler = build(source, orchestrator)

    processed = scheduler.poll_due_items(
        now=datetime(2026, 10, 5, tzinfo=timezone.utc)
    )

    # Notion healed to Published, the known post was NOT published again.
    assert processed == ["stale"]
    assert publisher.calls == []
    assert source.items["stale"].status is ContentStatus.PUBLISHED
    assert store.pending_syncs() == []
