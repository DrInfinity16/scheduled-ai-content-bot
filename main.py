"""Composition root.

Builds the concrete collaborators (Notion or YAML source, Gemini generator,
X publisher, audit log), wires the orchestrator and the scheduler, then
starts the application. No business logic lives here.
"""

from __future__ import annotations

import sys

from app.config import Config, ConfigError, load_config
from app.execution_log import ExecutionLog
from app.generator import GeminiGenerator
from app.images import (
    CloudflareImageProvider,
    GoogleImageProvider,
    ImageArtifactStore,
)
from app.orchestration import ContentOrchestrator
from app.publishers import XCredentials, create_publisher
from app.scheduler import ContentScheduler
from app.sources import (
    ContentSourceError,
    NotionContentSource,
    YAMLContentSource,
)
from app.storage import ExecutionStore


def build_source(config: Config):
    """Notion when configured, YAML otherwise (see CONTENT_SOURCE)."""
    if not config.wants_notion:
        return YAMLContentSource(config.calendar_file)

    source = NotionContentSource(
        config.notion_token,
        config.notion_database_id,
        retry_policy=config.retry_policy,
    )
    source.validate_schema()  # fail fast, naming the offending property
    return source


def build_app(config: Config) -> ContentScheduler:
    source = build_source(config)
    store = ExecutionStore(config.database_path)
    generator = GeminiGenerator(
        api_key=config.gemini_api_key,
        model=config.model,
        retry_policy=config.retry_policy,
    )
    publisher = create_publisher(
        "x",
        dry_run=config.dry_run,
        username=config.x_username,
        credentials=XCredentials(
            api_key=config.x_api_key,
            api_secret=config.x_api_secret,
            access_token=config.x_access_token,
            access_secret=config.x_access_secret,
        ),
        retry_policy=config.retry_policy,
    )
    audit_log = ExecutionLog(config.log_file)
    image_artifacts = ImageArtifactStore(config.image_output_dir)
    if config.image_provider == "cloudflare":
        image_generator = CloudflareImageProvider(
            account_id=config.cloudflare_account_id,
            api_token=config.cloudflare_api_token,
            model=config.image_model,
            artifacts=image_artifacts,
            retry_policy=config.retry_policy,
        )
    else:
        image_generator = GoogleImageProvider(
            api_key=config.gemini_api_key,
            model=config.image_model,
            artifacts=image_artifacts,
            retry_policy=config.retry_policy,
        )
    orchestrator = ContentOrchestrator(
        generator=generator,
        publisher=publisher,
        audit_log=audit_log,
        source=source,
        dry_run=config.dry_run,
        store=store,
        image_generator=image_generator,
        image_model=config.image_model,
    )
    return ContentScheduler(
        source=source,
        orchestrator=orchestrator,
        poll_interval_seconds=config.poll_interval_seconds,
    )


def _configure_console() -> None:
    """Legacy Windows consoles default to cp1252 and crash on '→'/'✓'."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main() -> int:
    _configure_console()
    try:
        config = load_config().validate()
    except ConfigError as exc:
        print(f"\n  Error: {exc}\n")
        return 1

    try:
        scheduler = build_app(config)
    except ContentSourceError as exc:
        print(f"\n  Error: {exc}\n")
        return 1

    mode = "DRY RUN (simulación)" if config.dry_run else "PUBLICACIÓN REAL"
    print(f"\n  content-bot — {mode}")
    store = getattr(scheduler.orchestrator, "store", None)
    if store is not None:
        print(f"  estado técnico: {store.path} (SQLite, idempotencia durable)")
    if config.image_model:
        print(
            f"  imágenes: {config.image_provider} ({config.image_model}) → {config.image_output_dir} "
            "(sólo items con Generate Image)"
        )

    try:
        if config.wants_notion:
            print("  fuente: Notion (calendario editorial)")
            print(
                f"  polling: cada {config.poll_interval_seconds}s "
                "→ items con Status=Scheduled y Scheduled At <= ahora\n"
            )
            scheduler.add_polling_job()
            try:
                scheduler.poll_due_items()
            except ContentSourceError as exc:
                print(
                    f"\n  Aviso: el primer ciclo de polling falló ({exc}); "
                    f"se reintentará cada {config.poll_interval_seconds}s\n"
                )
        else:
            items = scheduler.source.get_scheduled_items()
            print("  fuente: calendar.yaml (modo local)")
            print(f"  {len(items)} publicaciones programadas:\n")
            scheduler.schedule_items(items)

        scheduler.start()
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
