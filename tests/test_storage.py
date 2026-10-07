"""SQLite technical state: schema, transitions, durability, protection."""

import sqlite3

import pytest

from app.storage import (
    DEFAULT_DATABASE_PATH,
    ExecutionStatus,
    ExecutionStore,
    compute_content_hash,
)


def store_for(tmp_path, name="content_bot.db"):
    return ExecutionStore(str(tmp_path / name))


# -- initialization ---------------------------------------------------------


def test_database_file_is_created(tmp_path):
    path = tmp_path / "state" / "content_bot.db"
    store = ExecutionStore(str(path))

    assert path.exists()
    store.close()


def test_schema_initialisation_is_idempotent(tmp_path):
    store = store_for(tmp_path)
    store.close()
    reopened = ExecutionStore(str(tmp_path / "content_bot.db"))
    reopened.close()

    third = store_for(tmp_path)
    record = third.create_or_get_execution("page-1", "x")
    assert record.execution_status is ExecutionStatus.PENDING
    third.close()


def test_default_path_matches_config():
    from app.config import DEFAULT_DATABASE_PATH as CONFIG_DEFAULT

    assert DEFAULT_DATABASE_PATH == CONFIG_DEFAULT == "content_bot.db"


def test_in_memory_database_works():
    store = ExecutionStore(":memory:")
    store.mark_generating("p", "x")
    assert store.get_execution("p", "x").execution_status is (
        ExecutionStatus.GENERATING
    )
    store.close()


def test_context_manager_closes(tmp_path):
    with store_for(tmp_path) as store:
        store.create_or_get_execution("p", "x")
    with pytest.raises(sqlite3.ProgrammingError):
        store.get_execution("p", "x")


# -- identity ---------------------------------------------------------------


def test_create_or_get_returns_the_same_row(tmp_path):
    store = store_for(tmp_path)

    first = store.create_or_get_execution("page-1", "x")
    second = store.get_execution("page-1", "x")

    assert first.id == second.id
    assert store.pending_syncs() == []


def test_same_page_on_another_platform_is_a_different_record(tmp_path):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="1")

    other = store.get_execution("page-1", "instagram")

    assert other is None


def test_sql_metacharacters_in_ids_are_data_not_sql(tmp_path):
    store = store_for(tmp_path)
    hostile = "page'; DROP TABLE executions;--"

    store.mark_failed(hostile, "x", error="no pasa nada")
    record = store.get_execution(hostile, "x")

    assert record.execution_status is ExecutionStatus.FAILED
    assert record.last_error == "no pasa nada"


# -- transitions ------------------------------------------------------------


def test_full_success_transition_sequence(tmp_path):
    store = store_for(tmp_path)
    content_hash = compute_content_hash("page-1", "x", "hola mundo")

    store.increment_attempt("page-1", "x")
    store.mark_generating("page-1", "x")
    store.mark_ready(
        "page-1", "x", generated_content="hola mundo", content_hash=content_hash
    )
    store.mark_publishing("page-1", "x")
    store.mark_published("page-1", "x", external_post_id="42")
    record = store.mark_synced("page-1", "x")

    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.attempt_count == 1
    assert record.generated_content == "hola mundo"
    assert record.content_hash == content_hash
    assert record.external_post_id == "42"
    assert record.published_at is not None
    assert record.last_attempt_at is not None
    assert record.notion_sync_pending is False
    assert record.is_published is True
    assert record.needs_notion_sync is False


def test_failure_transition_records_the_error(tmp_path):
    store = store_for(tmp_path)
    store.mark_generating("page-1", "x")
    record = store.mark_failed("page-1", "x", error="gemini caído")

    assert record.execution_status is ExecutionStatus.FAILED
    assert record.last_error == "gemini caído"
    assert record.is_published is False


def test_simulated_has_no_external_post_id(tmp_path):
    store = store_for(tmp_path)
    store.mark_generating("page-1", "x")
    store.mark_ready("page-1", "x", generated_content="x", content_hash="h")
    record = store.mark_simulated("page-1", "x")

    assert record.execution_status is ExecutionStatus.SIMULATED
    assert record.external_post_id is None
    assert record.is_published is False


def test_increment_attempt_survives_each_call(tmp_path):
    store = store_for(tmp_path)
    store.increment_attempt("page-1", "x")
    record = store.increment_attempt("page-1", "x")

    assert record.attempt_count == 2
    assert record.last_attempt_at is not None


# -- durability / restart ---------------------------------------------------


def test_published_state_survives_a_new_connection(tmp_path):
    path = str(tmp_path / "content_bot.db")
    store = ExecutionStore(path)
    store.mark_publishing("page-1", "x")
    store.mark_published("page-1", "x", external_post_id="424242")
    store.close()

    # Simulates a process restart: a brand-new store over the same file.
    restarted = ExecutionStore(path)
    record = restarted.get_execution("page-1", "x")

    assert record.external_post_id == "424242"
    assert record.is_published is True
    assert record.notion_sync_pending is True
    restarted.close()


def test_manual_review_state_survives_restart(tmp_path):
    path = str(tmp_path / "content_bot.db")
    store = ExecutionStore(path)
    store.mark_manual_review("page-1", "x", error="revisar manualmente")
    store.close()

    restarted = ExecutionStore(path)
    record = restarted.get_execution("page-1", "x")
    assert record.execution_status is ExecutionStatus.MANUAL_REVIEW
    restarted.close()


# -- pending syncs ----------------------------------------------------------


def test_pending_syncs_lists_only_unconfirmed_publications(tmp_path):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="1")
    store.mark_published("page-2", "x", external_post_id="2")
    store.mark_generating("page-3", "x")
    store.mark_simulated("page-4", "x")

    pending = [record.content_item_id for record in store.pending_syncs()]
    assert pending == ["page-1", "page-2"]

    store.mark_synced("page-1", "x")
    assert [record.content_item_id for record in store.pending_syncs()] == ["page-2"]


def test_mark_sync_pending_is_listed_too(tmp_path):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="1")
    record = store.mark_sync_pending("page-1", "x", error="notion caído")

    assert record.execution_status is ExecutionStatus.SYNC_PENDING
    assert record.needs_notion_sync is True
    assert store.pending_syncs() == [record]


# -- anti-duplication protection -------------------------------------------


def test_published_record_is_never_demoted(tmp_path, capsys):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="42")

    record = store.mark_generating("page-1", "x")

    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.external_post_id == "42"
    assert "no se degrada" in capsys.readouterr().out


def test_failed_and_simulated_cannot_clobber_a_publication(tmp_path):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="42")

    assert store.mark_failed("page-1", "x", error="x").external_post_id == "42"
    assert store.mark_simulated("page-1", "x").external_post_id == "42"
    assert store.mark_generating("page-1", "x").external_post_id == "42"


def test_manual_review_is_terminal(tmp_path, capsys):
    store = store_for(tmp_path)
    store.mark_manual_review("page-1", "x", error="interrumpido")

    assert store.mark_generating("page-1", "x").execution_status is (
        ExecutionStatus.MANUAL_REVIEW
    )
    assert store.mark_failed("page-1", "x", error="otro").execution_status is (
        ExecutionStatus.MANUAL_REVIEW
    )
    assert "no se degrada" in capsys.readouterr().out


def test_sync_pending_may_resolve_to_published(tmp_path):
    store = store_for(tmp_path)
    store.mark_published("page-1", "x", external_post_id="42")
    store.mark_sync_pending("page-1", "x", error="fallo")

    record = store.mark_synced("page-1", "x")

    assert record.execution_status is ExecutionStatus.PUBLISHED
    assert record.notion_sync_pending is False
    assert record.last_error is None


def test_normal_failure_after_publishing_is_allowed(tmp_path):
    store = store_for(tmp_path)
    store.mark_publishing("page-1", "x")

    record = store.mark_failed("page-1", "x", error="rate limit")

    assert record.execution_status is ExecutionStatus.FAILED


# -- content hash -----------------------------------------------------------


def test_content_hash_is_stable_and_sensitive_to_content():
    first = compute_content_hash("page-1", "x", "hola")
    again = compute_content_hash("page-1", "x", "hola")
    changed = compute_content_hash("page-1", "x", "hola!")
    other_item = compute_content_hash("page-2", "x", "hola")
    other_platform = compute_content_hash("page-1", "instagram", "hola")

    assert first == again
    assert len(first) == 64  # SHA-256
    assert first != changed
    assert first != other_item
    assert first != other_platform


def test_content_hash_treats_missing_content_as_empty():
    assert compute_content_hash("p", "x", None) == compute_content_hash("p", "x", "")


# -- internal guards --------------------------------------------------------


def test_update_rejects_columns_outside_the_whitelist(tmp_path):
    store = store_for(tmp_path)
    store.create_or_get_execution("page-1", "x")

    with pytest.raises(ValueError):
        store._update(
            "page-1", "x", status=None, extra={"content_item_id": "otro"}
        )
    store.close()


def test_coerce_accepts_enum_and_string_values():
    assert ExecutionStatus.coerce(ExecutionStatus.PUBLISHED) is (
        ExecutionStatus.PUBLISHED
    )
    assert ExecutionStatus.coerce("manual_review") is (
        ExecutionStatus.MANUAL_REVIEW
    )


def test_del_is_best_effort_even_with_a_broken_handle(tmp_path):
    store = store_for(tmp_path)
    del store._connection

    store.__del__()  # must never raise (resource cleanup is best-effort)
