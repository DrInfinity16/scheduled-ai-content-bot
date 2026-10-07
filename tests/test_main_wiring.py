"""main.py wiring: SQLite store + bounded retry policies, end to end."""

from app.config import Config, DEFAULT_MODEL
from app.publishers.x import XPublisher
from app.scheduler import ContentScheduler
from app.sources import YAMLContentSource
from app.storage import ExecutionStore
from main import build_app, build_source

CALENDAR_YAML = """posts:
  - time: "09:00"
    topic: "Tip de Python"
    angle: "Práctico y directo"
"""


def make_config(tmp_path, **overrides):
    calendar = tmp_path / "calendar.yaml"
    calendar.write_text(CALENDAR_YAML, encoding="utf-8")
    defaults = dict(
        gemini_api_key="gemini-key",
        model=DEFAULT_MODEL,
        dry_run=True,
        x_api_key=None,
        x_api_secret=None,
        x_access_token=None,
        x_access_secret=None,
        calendar_file=str(calendar),
        log_file=str(tmp_path / "posts.jsonl"),
        database_path=str(tmp_path / "state" / "content_bot.db"),
        retry_max_attempts=5,
        retry_base_delay_seconds=2.5,
        image_output_dir=str(tmp_path / "images"),
    )
    defaults.update(overrides)
    return Config(**defaults).validate()


def test_build_app_creates_the_sqlite_store(tmp_path):
    config = make_config(tmp_path)

    scheduler = build_app(config)

    assert isinstance(scheduler, ContentScheduler)
    store = scheduler.orchestrator.store
    assert isinstance(store, ExecutionStore)
    assert store.path == str(tmp_path / "state" / "content_bot.db")
    # The directory and file were created eagerly at build time.
    assert (tmp_path / "state" / "content_bot.db").exists()


def test_build_app_wires_the_retry_policy_everywhere(tmp_path):
    config = make_config(tmp_path)

    scheduler = build_app(config)

    orchestrator = scheduler.orchestrator
    assert orchestrator.generator._retry_policy.max_attempts == 5
    assert orchestrator.generator._retry_policy.base_delay == 2.5
    assert orchestrator.publisher._retry_policy.max_attempts == 5
    assert isinstance(orchestrator.publisher, XPublisher)


def test_build_app_honours_dry_run_and_the_yaml_source(tmp_path):
    config = make_config(tmp_path)

    scheduler = build_app(config)

    assert scheduler.orchestrator.dry_run is True
    assert isinstance(scheduler.source, YAMLContentSource)
    assert scheduler.poll_interval_seconds == config.poll_interval_seconds


def test_build_source_yaml_mode_never_touches_notion(tmp_path):
    config = make_config(tmp_path)

    source = build_source(config)

    assert isinstance(source, YAMLContentSource)


def test_the_wired_store_survives_a_close_and_reopen(tmp_path):
    config = make_config(tmp_path)
    scheduler = build_app(config)

    scheduler.orchestrator.store.mark_publishing("page-9", "x")
    scheduler.orchestrator.store.mark_published(
        "page-9", "x", external_post_id="123"
    )
    scheduler.orchestrator.store.close()

    reopened = ExecutionStore(config.database_path)
    try:
        record = reopened.get_execution("page-9", "x")
        assert record.external_post_id == "123"
        assert record.is_published is True
    finally:
        reopened.close()


def test_build_app_can_poll_the_yaml_source(tmp_path):
    config = make_config(tmp_path)
    scheduler = build_app(config)

    items = scheduler.source.get_scheduled_items()

    assert len(items) == 1
    assert items[0].topic == "Tip de Python"


def test_build_app_wires_the_image_pipeline(tmp_path):
    from app.images import GoogleImageProvider

    config = make_config(tmp_path, image_model="modelo-visual")
    scheduler = build_app(config)

    orchestrator = scheduler.orchestrator
    assert isinstance(orchestrator.image_generator, GoogleImageProvider)
    assert orchestrator.image_generator._model == "modelo-visual"
    assert orchestrator.image_model == "modelo-visual"
    assert str(orchestrator.image_generator._artifacts.root) == str(
        tmp_path / "images"
    )
    assert (tmp_path / "images").exists()


def test_build_app_without_image_model_leaves_it_unset(tmp_path):
    config = make_config(tmp_path)
    scheduler = build_app(config)

    assert scheduler.orchestrator.image_model is None
    assert scheduler.orchestrator.image_generator._model is None
