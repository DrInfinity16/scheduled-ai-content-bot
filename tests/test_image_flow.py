"""Orchestrator image path end to end (fake transports, real classes)."""

import json

from app.execution_log import ExecutionLog
from app.images.base import ImageGenerationError
from app.models import ContentStatus, PublishResult
from app.orchestration import ContentOrchestrator
from app.publishers.x import XPublisher
from app.storage import ExecutionStatus, ExecutionStore

from tests.conftest import (
    FakeImageGenerator,
    FakeMediaUploader,
    FakeTweepyClient,
    RecordingGenerator,
    RecordingPublisher,
    RecordingSource,
)
from app.images.artifacts import ImageArtifactStore
from app.retry import RetryPolicy

FAST = RetryPolicy(max_attempts=3, base_delay=0)


class Harness:
    def __init__(self, tmp_path, item, *, dry_run=False, broken_sync=False,
                 image_error=None, upload_error=None, tweet_error=None,
                 image_generator=None):
        from tests.conftest import MINIMAL_PNG  # noqa

        self.item = item
        self.source = RecordingSource([item])
        if broken_sync:
            real_update = self.source.update_item

            def failing_update(item_id, **changes):
                if changes.get("status") is ContentStatus.PUBLISHED:
                    raise RuntimeError("Notion 500 simulado")
                return real_update(item_id, **changes)

            self.source.update_item = failing_update
        self.artifacts = ImageArtifactStore(str(tmp_path / "images"))
        self.image_generator = image_generator or FakeImageGenerator(
            artifacts=self.artifacts, error=image_error
        )
        self.tweepy = FakeTweepyClient(post_id="424242", error=tweet_error)
        self.uploader = FakeMediaUploader(media_id="999", error=upload_error)
        self.publisher = XPublisher(
            dry_run=dry_run,
            client=self.tweepy,
            username="ana",
            retry_policy=FAST,
            media_uploader=self.uploader,
        )
        self.store = ExecutionStore(str(tmp_path / "content_bot.db"))
        self.log_path = tmp_path / "posts.jsonl"
        self.orchestrator = ContentOrchestrator(
            generator=RecordingGenerator(),
            publisher=self.publisher,
            audit_log=ExecutionLog(str(self.log_path)),
            source=self.source,
            dry_run=dry_run,
            store=self.store,
            image_generator=self.image_generator,
            image_model="modelo-falso",
        )

    def restart(self):
        self.store.close()
        self.store = ExecutionStore(str(self.store.path))
        self.orchestrator.store = self.store
        return self.orchestrator

    def entries(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def test_full_image_flow_publishes_text_plus_media(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(generate_image=True))

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.ok
    assert result.publish_result.media_id == "999"
    # Media uploaded once, tweet created once with that media id.
    assert len(harness.uploader.calls) == 1
    assert harness.tweepy.media_ids_calls == [["999"]]
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.external_post_id == "424242"
    assert record.media_id == "999"
    assert record.generated_image_path is not None
    assert len(record.generated_image_hash or "") == 64
    assert len(record.visual_fingerprint or "") == 64
    assert record.image_provider == "fake"
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    # Local paths are never written to Notion (url property stays honest).
    assert all(
        "generated_image" not in changes
        for _, changes in harness.source.updates
    )
    entry = harness.entries()[-1]
    assert entry["status"] == "published"
    assert entry["media_id"] == "999"
    assert entry["media_uploaded"] is True
    assert entry["generated_image_path"] == record.generated_image_path
    assert entry["generated_image_hash"] == record.generated_image_hash


def test_brief_reaches_the_provider_prompt(tmp_path, make_item):
    harness = Harness(
        tmp_path, make_item(generate_image=True, image_brief="zorro azul")
    )

    harness.orchestrator.run(make_item(generate_image=True, image_brief="zorro azul"))

    assert len(harness.image_generator.calls) == 1
    assert harness.image_generator.calls[0]["prompt"].startswith("zorro azul")


def test_text_only_path_never_touches_images(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(generate_image=False))

    result = harness.orchestrator.run(make_item(generate_image=False))

    assert result.ok
    assert harness.image_generator.calls == []
    assert list((tmp_path / "images").iterdir()) == []
    assert harness.uploader.calls == []
    assert harness.tweepy.media_ids_calls == [None]
    record = harness.store.get_execution("item-001", "x")
    assert record.generated_image_path is None
    assert record.media_id is None


def test_missing_image_generator_fails_explicitly(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(generate_image=True))
    harness.orchestrator.image_generator = None

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.FAILED
    assert "no hay generador de imágenes" in result.error
    assert harness.tweepy.calls == []
    assert harness.uploader.calls == []
    assert harness.source.last["status"] is ContentStatus.FAILED


def test_image_failure_never_falls_back_to_text_only(tmp_path, make_item):
    harness = Harness(
        tmp_path,
        make_item(generate_image=True),
        image_error=ImageGenerationError("proveedor caído"),
    )

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.FAILED
    assert "proveedor caído" in result.error
    assert harness.tweepy.calls == []  # no silent text-only tweet
    assert harness.uploader.calls == []
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.FAILED
    assert record.external_post_id is None


def test_dry_run_generates_image_but_touches_nothing(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(generate_image=True), dry_run=True)

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.READY
    assert result.publish_result.status == "simulated"
    assert result.publish_result.id is None
    assert result.publish_result.media_id is None
    # The full generation pipeline ran locally...
    assert len(harness.image_generator.calls) == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SIMULATED
    assert record.generated_image_path is not None
    assert record.external_post_id is None
    assert record.media_id is None
    # ...but X was never touched.
    assert harness.uploader.calls == []
    assert harness.tweepy.calls == []
    assert harness.source.items["item-001"].status is ContentStatus.READY
    assert ContentStatus.PUBLISHED not in harness.source.statuses
    entry = harness.entries()[-1]
    assert entry["status"] == "simulated"
    assert entry["generated_image_path"] == record.generated_image_path
    assert entry["media_id"] is None


def test_dry_run_rerun_reuses_the_image(tmp_path, make_item):
    harness = Harness(tmp_path, make_item(generate_image=True), dry_run=True)

    harness.orchestrator.run(make_item(generate_image=True))
    harness.orchestrator.run(make_item(generate_image=True))

    assert len(harness.image_generator.calls) == 1
    assert harness.uploader.calls == []
    assert harness.tweepy.calls == []


def test_media_upload_failure_fails_the_workflow(tmp_path, make_item):
    harness = Harness(
        tmp_path,
        make_item(generate_image=True),
        upload_error=RuntimeError("413 demasiado grande"),
    )

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.FAILED
    assert "demasiado grande" in result.error
    assert harness.tweepy.calls == []  # no tweet without media
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.FAILED
    assert record.external_post_id is None
    # The artifact metadata survived for a future retry.
    assert record.generated_image_path is not None


def test_orphan_media_is_audited_on_tweet_failure(tmp_path, make_item):
    harness = Harness(
        tmp_path,
        make_item(generate_image=True),
        tweet_error=RuntimeError("duplicate content"),
    )

    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.FAILED
    entry = harness.entries()[-1]
    assert entry["media_id"] == "999"  # uploaded, then the tweet failed
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.FAILED
    assert record.external_post_id is None


def test_critical_partial_failure_never_reuploads(tmp_path, make_item):
    harness = Harness(
        tmp_path, make_item(generate_image=True), broken_sync=True
    )

    first = harness.orchestrator.run(make_item(generate_image=True))

    assert first.ok
    assert len(harness.uploader.calls) == 1
    assert len(harness.tweepy.calls) == 1
    record = harness.store.get_execution("item-001", "x")
    assert record.execution_status is ExecutionStatus.SYNC_PENDING
    assert record.media_id == "999"
    assert ContentStatus.PUBLISHED not in harness.source.statuses

    # Notion still down: reconcile only, zero new uploads or tweets.
    harness.orchestrator.reconcile_pending_syncs()
    assert len(harness.uploader.calls) == 1
    assert len(harness.tweepy.calls) == 1

    # Notion heals: converge without republishing anything.
    harness.source.update_item = RecordingSource.update_item.__get__(
        harness.source, RecordingSource
    )
    harness.orchestrator.reconcile_pending_syncs()
    assert len(harness.uploader.calls) == 1
    assert len(harness.tweepy.calls) == 1
    assert harness.source.items["item-001"].status is ContentStatus.PUBLISHED
    assert harness.store.pending_syncs() == []


def test_published_image_post_survives_restart_without_duplicates(
    tmp_path, make_item
):
    harness = Harness(tmp_path, make_item(generate_image=True))
    harness.orchestrator.run(make_item(generate_image=True))
    assert len(harness.uploader.calls) == 1

    harness.restart()
    result = harness.orchestrator.run(make_item(generate_image=True))

    assert result.status is ContentStatus.PUBLISHED
    assert len(harness.uploader.calls) == 1
    assert len(harness.tweepy.calls) == 1
    assert len(harness.image_generator.calls) == 1  # gate ran first


def test_recording_publisher_receives_the_media_object(tmp_path, make_item):
    publisher = RecordingPublisher(
        PublishResult(status="published", id="7", platform="x")
    )
    source = RecordingSource([make_item(generate_image=True)])
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    artifacts = ImageArtifactStore(str(tmp_path / "images"))
    orchestrator = ContentOrchestrator(
        generator=RecordingGenerator(),
        publisher=publisher,
        audit_log=ExecutionLog(str(tmp_path / "posts.jsonl")),
        source=source,
        dry_run=False,
        store=store,
        image_generator=FakeImageGenerator(artifacts=artifacts),
        image_model="modelo-falso",
    )

    result = orchestrator.run(make_item(generate_image=True))

    assert result.ok
    assert len(publisher.calls) == 1
    _, _, media = publisher.calls[0]
    assert media is not None
    assert media.local_path.endswith(".png")
