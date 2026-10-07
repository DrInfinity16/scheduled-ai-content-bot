"""SQLite image metadata: safe upgrade, persistence rules."""

import sqlite3

from app.storage import ExecutionStatus, ExecutionStore

LEGACY_SCHEMA = """
CREATE TABLE executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    content_hash TEXT,
    generated_content TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    external_post_id TEXT,
    published_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT,
    notion_sync_pending INTEGER NOT NULL DEFAULT 0,
    UNIQUE (content_item_id, platform)
);
"""


def write_legacy_db(path, *, status="published", external_post_id="42"):
    connection = sqlite3.connect(str(path))
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO executions "
        "(content_item_id, platform, execution_status, external_post_id,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("page-1", "x", status, external_post_id, "t", "t"),
    )
    connection.commit()
    connection.close()


def test_legacy_database_upgrades_without_losing_rows(tmp_path):
    path = tmp_path / "content_bot.db"
    write_legacy_db(path)

    store = ExecutionStore(str(path))
    record = store.get_execution("page-1", "x")

    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.external_post_id == "42"
    # New columns exist and default to empty.
    assert record.generated_image_path is None
    assert record.generated_image_hash is None
    assert record.visual_fingerprint is None
    assert record.image_provider is None
    assert record.media_id is None
    store.close()


def test_upgrade_is_idempotent(tmp_path):
    path = tmp_path / "content_bot.db"
    write_legacy_db(path)

    first = ExecutionStore(str(path))
    first.close()
    second = ExecutionStore(str(path))
    record = second.get_execution("page-1", "x")

    assert record.external_post_id == "42"
    second.close()


def test_image_metadata_does_not_change_status(tmp_path):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_generating("page-1", "x")

    record = store.mark_image_generated(
        "page-1",
        "x",
        path="artifacts/images/page-1_abc.png",
        image_hash="h" * 64,
        visual_fingerprint="f" * 64,
        provider="google",
    )

    assert record.execution_status is ExecutionStatus.GENERATING
    assert record.generated_image_path == "artifacts/images/page-1_abc.png"
    assert record.generated_image_hash == "h" * 64
    assert record.visual_fingerprint == "f" * 64
    assert record.image_provider == "google"
    store.close()


def test_mark_published_stores_the_media_id(tmp_path):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))

    record = store.mark_published(
        "page-1", "x", external_post_id="42", media_id="888"
    )

    assert record.media_id == "888"
    assert record.external_post_id == "42"
    store.close()


def test_reaffirmation_without_media_keeps_the_media_id(tmp_path):
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    store.mark_published("page-1", "x", external_post_id="42", media_id="888")

    record = store.mark_published("page-1", "x", external_post_id="42")

    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.media_id == "888"
    store.close()


def test_fresh_database_has_the_image_columns(tmp_path):
    path = tmp_path / "content_bot.db"
    store = ExecutionStore(str(path))

    columns = {
        row["name"]
        for row in store._connection.execute(
            "PRAGMA table_info(executions)"
        ).fetchall()
    }

    assert {
        "generated_image_path",
        "generated_image_hash",
        "visual_fingerprint",
        "image_provider",
        "media_id",
    } <= columns
    store.close()
