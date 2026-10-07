from datetime import datetime

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app.models import ContentStatus
from app.scheduler import ContentScheduler
from app.sources import YAMLContentSource


class StubOrchestrator:
    def __init__(self):
        self.runs = []

    def run(self, item):
        self.runs.append(item)
        return item


class StubSource:
    def __init__(self, items):
        self.items = {item.id: item for item in items}

    def get_scheduled_items(self, start=None, end=None):
        return list(self.items.values())

    def get_item(self, item_id):
        return self.items.get(item_id)

    def update_item(self, item_id, **changes):
        return self.items[item_id]


def build(items, source=None):
    source = source or StubSource(items)
    orchestrator = StubOrchestrator()
    scheduler = ContentScheduler(
        source=source,
        orchestrator=orchestrator,
        scheduler=BackgroundScheduler(),
    )
    return scheduler, orchestrator, source


def trigger_value(trigger, name: str):
    field = next(f for f in trigger.fields if f.name == name)
    raw = str(field)
    try:
        return int(raw)
    except ValueError:
        return raw


def build_item(item_id="item-001", hour=9, minute=30, **overrides):
    from app.models import ContentItem

    defaults = dict(
        id=item_id,
        topic="Tema",
        angle="Ángulo",
        scheduled_at=datetime(2026, 10, 5, hour, minute),
    )
    defaults.update(overrides)
    return ContentItem(**defaults)


def test_valid_content_items_become_scheduled_jobs():
    items = [build_item("a", 9, 0), build_item("b", 14, 30)]
    scheduler, _, _ = build(items)

    job_ids = scheduler.schedule_items(items)

    assert job_ids == ["a", "b"]
    jobs = scheduler.scheduler.get_jobs()
    assert [job.id for job in jobs] == ["a", "b"]
    assert trigger_value(jobs[0].trigger, 'hour') == 9
    assert trigger_value(jobs[0].trigger, 'minute') == 0
    assert trigger_value(jobs[1].trigger, 'hour') == 14
    assert trigger_value(jobs[1].trigger, 'minute') == 30


def test_raw_source_structures_never_reach_the_scheduler():
    items = [build_item("a", 9, 0)]
    scheduler, _, _ = build(items)

    scheduler.schedule_items(items)

    job = scheduler.scheduler.get_jobs()[0]
    assert all(isinstance(arg, str) for arg in job.args)
    assert list(job.args) == ["a"]
    assert not any(isinstance(arg, dict) for arg in job.args)


def test_scheduler_pulls_items_from_the_source_when_not_given():
    items = [build_item("a", 8, 0)]
    scheduler, _, source = build(items)

    job_ids = scheduler.schedule_items()

    assert job_ids == ["a"]
    assert scheduler.job_ids == ["a"]


def test_items_without_schedule_are_skipped():
    unscheduled = build_item("sin-horario", scheduled_at=None)
    scheduled = build_item("con-horario", hour=10, minute=0)
    scheduler, _, _ = build([unscheduled, scheduled])

    job_ids = scheduler.schedule_items([unscheduled, scheduled])

    assert job_ids == ["con-horario"]
    assert scheduler.job_ids == ["con-horario"]


def test_non_content_items_are_rejected():
    scheduler, _, _ = build([])
    with pytest.raises(TypeError):
        scheduler.schedule_items([{"topic": "crudo"}])


def test_run_job_resolves_item_from_source_and_runs_workflow():
    item = build_item("a")
    scheduler, orchestrator, _ = build([item])

    scheduler.run_job("a")

    assert len(orchestrator.runs) == 1
    assert orchestrator.runs[0] is item
    assert orchestrator.runs[0].id == "a"


def test_run_job_with_unknown_id_does_not_raise():
    scheduler, orchestrator, _ = build([])

    scheduler.run_job("desconocido")

    assert orchestrator.runs == []


def test_scheduler_works_with_yaml_source(tmp_path, calendar_path):
    calendar = tmp_path / "calendar.yaml"
    calendar.write_text(
        "posts:\n  - time: '07:15'\n    topic: 'T'\n    angle: 'A'\n",
        encoding="utf-8",
    )
    source = YAMLContentSource(str(calendar))
    orchestrator = StubOrchestrator()
    scheduler = ContentScheduler(source, orchestrator, BackgroundScheduler())

    items = source.get_scheduled_items()
    job_ids = scheduler.schedule_items(items)

    assert job_ids == [items[0].id]
    job = scheduler.scheduler.get_jobs()[0]
    assert trigger_value(job.trigger, 'hour') == 7
    assert trigger_value(job.trigger, 'minute') == 15
    assert list(job.args) == [items[0].id]

    scheduler.run_job(items[0].id)
    assert orchestrator.runs[0].topic == "T"
    assert orchestrator.runs[0].status is ContentStatus.SCHEDULED


class FakeBlockingScheduler:
    def __init__(self, interrupt=False, running=False):
        self.interrupt = interrupt
        self.running = running
        self.started = False
        self.jobs = []
        self.shutdown_calls = []

    def add_job(self, func, **kwargs):
        from types import SimpleNamespace

        self.jobs.append(SimpleNamespace(id=kwargs.get("id"), trigger=kwargs.get("trigger"), args=kwargs.get("args")))

    def get_jobs(self):
        return self.jobs

    def start(self):
        self.started = True
        if self.interrupt:
            raise KeyboardInterrupt

    def shutdown(self, wait=False):
        self.shutdown_calls.append(wait)
        self.running = False


def test_default_scheduler_is_blocking():
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = ContentScheduler(StubSource([]), StubOrchestrator())
    assert isinstance(scheduler.scheduler, BlockingScheduler)
    assert scheduler.scheduler.running is False


def test_start_handles_keyboard_interrupt(capsys):
    fake = FakeBlockingScheduler(interrupt=True)
    scheduler = ContentScheduler(StubSource([]), StubOrchestrator(), fake)

    scheduler.start()

    assert fake.started is True
    assert "Bot detenido" in capsys.readouterr().out


def test_shutdown_only_runs_when_scheduler_is_running():
    running = FakeBlockingScheduler(running=True)
    ContentScheduler(StubSource([]), StubOrchestrator(), running).shutdown(wait=True)
    assert running.shutdown_calls == [True]

    stopped = FakeBlockingScheduler(running=False)
    ContentScheduler(StubSource([]), StubOrchestrator(), stopped).shutdown()
    assert stopped.shutdown_calls == []
