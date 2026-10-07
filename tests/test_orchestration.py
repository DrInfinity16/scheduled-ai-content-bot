import json

from app.models import ContentStatus, PublishResult
from app.orchestration import ContentOrchestrator
from app.execution_log import ExecutionLog
from tests.conftest import RecordingGenerator, RecordingPublisher


def build(generator, publisher, tmp_path):
    audit_log = ExecutionLog(str(tmp_path / "posts.jsonl"))
    return ContentOrchestrator(generator, publisher, audit_log), audit_log


def read_entries(path):
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


def test_happy_path_generate_validate_publish_log(
    tmp_path, make_item, recording_generator, recording_publisher
):
    orchestrator, audit_log = build(recording_generator, recording_publisher, tmp_path)
    item = make_item()

    result = orchestrator.run(item)

    assert result.ok
    assert result.status is ContentStatus.PUBLISHED
    assert item.status is ContentStatus.PUBLISHED
    assert item.generated_content == "tweet válido"
    assert len(recording_generator.calls) == 1
    assert len(recording_publisher.calls) == 1

    entries = read_entries(audit_log.path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["item_id"] == item.id
    assert entry["topic"] == item.topic
    assert entry["platform"] == "x"
    assert entry["content"] == "tweet válido"
    assert entry["status"] == "published"
    assert entry["workflow_status"] == "published"
    assert entry["external_post_id"] == "1"
    assert entry["error"] is None


def test_generation_failure_stops_workflow(
    tmp_path, make_item, recording_publisher
):
    from app.generator import GenerationError

    generator = RecordingGenerator(error=GenerationError("gemini caído"))
    orchestrator, audit_log = build(generator, recording_publisher, tmp_path)
    item = make_item()

    result = orchestrator.run(item)

    assert not result.ok
    assert result.status is ContentStatus.FAILED
    assert item.status is ContentStatus.FAILED
    assert item.generated_content is None
    assert recording_publisher.calls == []

    entry = read_entries(audit_log.path)[0]
    assert entry["workflow_status"] == "failed"
    assert "gemini caído" in entry["error"]


def test_validation_failure_stops_publish(tmp_path, make_item):
    generator = RecordingGenerator(content="x" * 400)
    publisher = RecordingPublisher()
    orchestrator, audit_log = build(generator, publisher, tmp_path)

    result = orchestrator.run(make_item())

    assert result.status is ContentStatus.FAILED
    assert publisher.calls == []
    entry = read_entries(audit_log.path)[0]
    assert entry["workflow_status"] == "failed"
    assert "límite" in entry["error"]


def test_invalid_item_fails_before_generation(tmp_path, make_item):
    generator = RecordingGenerator()
    publisher = RecordingPublisher()
    orchestrator, audit_log = build(generator, publisher, tmp_path)
    item = make_item(topic="")

    result = orchestrator.run(item)

    assert result.status is ContentStatus.FAILED
    assert generator.calls == []
    assert publisher.calls == []
    assert "topic" in read_entries(audit_log.path)[0]["error"]


def test_publisher_error_is_logged(tmp_path, make_item):
    generator = RecordingGenerator()
    publisher = RecordingPublisher(
        PublishResult(status="error", error="credenciales inválidas")
    )
    orchestrator, audit_log = build(generator, publisher, tmp_path)
    item = make_item()

    result = orchestrator.run(item)

    assert result.status is ContentStatus.FAILED
    assert item.status is ContentStatus.FAILED
    assert result.error == "credenciales inválidas"

    entry = read_entries(audit_log.path)[0]
    assert entry["status"] == "error"
    assert entry["workflow_status"] == "failed"
    assert entry["error"] == "credenciales inválidas"
    assert entry["external_post_id"] is None


def test_simulated_publish_is_a_success(tmp_path, make_item):
    generator = RecordingGenerator()
    publisher = RecordingPublisher(PublishResult(status="simulated", dry_run=True))
    orchestrator, audit_log = build(generator, publisher, tmp_path)

    result = orchestrator.run(make_item())

    # DRY RUN termina en Ready: nunca se afirma una publicación real.
    assert result.ok
    assert result.status is ContentStatus.READY
    assert result.publish_result.status == "simulated"
    entry = read_entries(audit_log.path)[0]
    assert entry["status"] == "simulated"
    assert entry["workflow_status"] == "ready"


def test_unexpected_generator_crash_is_logged(tmp_path, make_item):
    class ExplodingGenerator:
        def generate(self, item):
            raise ValueError("boom")

    orchestrator, audit_log = build(
        ExplodingGenerator(), RecordingPublisher(), tmp_path
    )

    result = orchestrator.run(make_item())

    assert result.status is ContentStatus.FAILED
    assert "boom" in read_entries(audit_log.path)[0]["error"]
