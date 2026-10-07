"""End-to-end wiring test: YAML → orchestrator → dry-run publish → JSONL.

No network access: the generator and the Tweepy client are both fakes while
the real YAML source, orchestrator, publisher, scheduler and audit log are
exercised together.
"""

import json

from app.execution_log import ExecutionLog
from app.orchestration import ContentOrchestrator
from app.publishers import XPublisher
from app.scheduler import ContentScheduler
from app.sources import YAMLContentSource
from app.models import ContentStatus
from apscheduler.schedulers.background import BackgroundScheduler
from tests.conftest import FakeGeminiClient, FakeTweepyClient
from app.generator import GeminiGenerator


def test_yaml_to_dry_run_publish_flow(tmp_path, calendar_path):
    source = YAMLContentSource(str(calendar_path))
    generator = GeminiGenerator(
        api_key="test-key",
        model="gemini-test",
        client=FakeGeminiClient(text="Tweet generado para el smoke"),
    )
    publisher = XPublisher(dry_run=True, client=FakeTweepyClient())
    audit_log = ExecutionLog(str(tmp_path / "posts.jsonl"))
    orchestrator = ContentOrchestrator(generator, publisher, audit_log)
    scheduler = ContentScheduler(source, orchestrator, BackgroundScheduler())

    items = source.get_scheduled_items()
    job_ids = scheduler.schedule_items(items)

    assert len(job_ids) == 3
    scheduler.run_job(job_ids[0])

    entry = json.loads(audit_log.path.read_text(encoding="utf-8").strip())
    assert entry["topic"] == items[0].topic
    assert entry["content"] == "Tweet generado para el smoke"
    assert entry["status"] == "simulated"
    assert entry["workflow_status"] == "ready"
    assert entry["external_post_id"] is None
    assert entry["error"] is None
    assert items[0].status is ContentStatus.READY
    assert items[0].generated_content == "Tweet generado para el smoke"
