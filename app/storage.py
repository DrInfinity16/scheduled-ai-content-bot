"""Durable technical execution state (SQLite).

Notion owns the editorial content; this module owns *technical* facts that
must survive process and scheduler restarts: has this page already been
published, which external post id was returned, how many attempts ran,
what failed and whether Notion still needs to be synchronised.

Nothing editorial (topic, angle, dates, generated copy as source of truth)
is stored here beyond a defensive snapshot of the generated text used for
the content hash.

The schema is created idempotently (``CREATE TABLE IF NOT EXISTS``); no
migration framework exists at this stage. All statements are
parameterised: no dynamic SQL ever embeds user content.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Optional

DEFAULT_DATABASE_PATH = "content_bot.db"


class ExecutionStatus(str, Enum):
    """Technical state of one (content_item_id, platform) execution.

    Deliberately *not* the editorial vocabulary: it exists for
    idempotency and recovery, not as a second workflow.
    """

    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    SIMULATED = "simulated"
    SYNC_PENDING = "sync_pending"
    MANUAL_REVIEW = "manual_review"

    @classmethod
    def coerce(cls, value: "ExecutionStatus | str") -> "ExecutionStatus":
        if isinstance(value, cls):
            return value
        return cls(str(value))


# States that must never be demoted by a later run: they mean "an external
# side effect (a real X post) already happened" or "a human must look".
PROTECTED_STATUSES = frozenset(
    {ExecutionStatus.PUBLISHED, ExecutionStatus.SYNC_PENDING, ExecutionStatus.MANUAL_REVIEW}
)

# The publication family: knowledge of a real external post.
PUBLISHED_FAMILY = frozenset({ExecutionStatus.PUBLISHED, ExecutionStatus.SYNC_PENDING})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
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
    generated_image_path TEXT,
    generated_image_hash TEXT,
    visual_fingerprint TEXT,
    image_provider TEXT,
    media_id TEXT,
    UNIQUE (content_item_id, platform)
);
CREATE INDEX IF NOT EXISTS idx_executions_notion_sync_pending
    ON executions (notion_sync_pending);
"""

# Columns added after Pass 3. Fresh databases get them from ``_SCHEMA``;
# older files receive them through ``_ensure_columns`` (small, explicit
# upgrade — no migration framework for these few additive columns).
_IMAGE_COLUMNS = {
    "generated_image_path": "TEXT",
    "generated_image_hash": "TEXT",
    "visual_fingerprint": "TEXT",
    "image_provider": "TEXT",
    "media_id": "TEXT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_content_hash(
    content_item_id: str, platform: str, content: Optional[str]
) -> str:
    """SHA-256 over identity + copy: defence in depth, never an identity.

    The durable identity of a publication is ``content_item_id +
    platform``; the hash only helps to *detect* that copy changed.
    """
    payload = "\x1e".join([content_item_id, platform, content or ""])
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionRecord:
    """One durable execution record (technical facts only)."""

    id: int
    content_item_id: str
    platform: str
    execution_status: ExecutionStatus
    content_hash: Optional[str]
    generated_content: Optional[str]
    attempt_count: int
    external_post_id: Optional[str]
    published_at: Optional[str]
    last_error: Optional[str]
    created_at: str
    updated_at: str
    last_attempt_at: Optional[str]
    notion_sync_pending: bool
    generated_image_path: Optional[str] = None
    generated_image_hash: Optional[str] = None
    visual_fingerprint: Optional[str] = None
    image_provider: Optional[str] = None
    media_id: Optional[str] = None

    @property
    def is_published(self) -> bool:
        """True when a real publication is durably known."""
        if self.external_post_id:
            return True
        return self.execution_status in PUBLISHED_FAMILY

    @property
    def needs_notion_sync(self) -> bool:
        return self.notion_sync_pending or self.execution_status is (
            ExecutionStatus.SYNC_PENDING
        )

class ExecutionStore:
    """Small explicit API over the ``executions`` table.

    Thread-safe (APScheduler runs the poll job in a worker thread) and
    process-safe for reads/updates: the file survives restarts, which is
    what makes idempotency durable.
    """

    def __init__(self, path: str = DEFAULT_DATABASE_PATH, *, timeout: float = 10.0):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, timeout=timeout
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()
            self._ensure_columns()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __del__(self) -> None:
        # Best-effort: avoid ResourceWarning for handles nobody closed
        # (short-lived stores in tests, or a process exiting mid-cycle).
        try:
            self._connection.close()
        except Exception:
            pass

    def __enter__(self) -> "ExecutionStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _ensure_columns(self) -> None:
        """Additive upgrade for databases created before the new columns.

        Only ``ADD COLUMN`` with fixed names from ``_IMAGE_COLUMNS`` is
        ever executed, so a Pass 3 file opens without crashing and without
        losing a single row.
        """
        existing = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(executions)"
            ).fetchall()
        }
        for name, ddl in _IMAGE_COLUMNS.items():
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE executions ADD COLUMN {name} {ddl}"
                )
        self._connection.commit()

    # -- reading -----------------------------------------------------------

    def get_execution(
        self, content_item_id: str, platform: str
    ) -> Optional[ExecutionRecord]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM executions "
                "WHERE content_item_id = ? AND platform = ?",
                (content_item_id, platform),
            ).fetchone()
        return self._to_record(row) if row else None

    def pending_syncs(self) -> list[ExecutionRecord]:
        """Records whose Notion side is not confirmed yet (reconciliation)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM executions "
                "WHERE notion_sync_pending = 1 OR execution_status = ? "
                "ORDER BY id",
                (ExecutionStatus.SYNC_PENDING.value,),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def create_or_get_execution(
        self, content_item_id: str, platform: str
    ) -> ExecutionRecord:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO executions "
                "(content_item_id, platform, execution_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    content_item_id,
                    platform,
                    ExecutionStatus.PENDING.value,
                    _now(),
                    _now(),
                ),
            )
            self._connection.commit()
        record = self.get_execution(content_item_id, platform)
        assert record is not None
        return record

    # -- transitions -------------------------------------------------------

    def increment_attempt(
        self, content_item_id: str, platform: str
    ) -> ExecutionRecord:
        self.create_or_get_execution(content_item_id, platform)
        moment = _now()
        with self._lock:
            self._connection.execute(
                "UPDATE executions SET attempt_count = attempt_count + 1, "
                "last_attempt_at = ?, updated_at = ? "
                "WHERE content_item_id = ? AND platform = ?",
                (moment, moment, content_item_id, platform),
            )
            self._connection.commit()
        record = self.get_execution(content_item_id, platform)
        assert record is not None
        return record

    def mark_generating(self, content_item_id: str, platform: str) -> ExecutionRecord:
        return self._update(
            content_item_id, platform, status=ExecutionStatus.GENERATING
        )

    def mark_ready(
        self,
        content_item_id: str,
        platform: str,
        *,
        generated_content: Optional[str],
        content_hash: Optional[str],
    ) -> ExecutionRecord:
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.READY,
            extra={
                "generated_content": generated_content,
                "content_hash": content_hash,
            },
        )

    def mark_publishing(
        self, content_item_id: str, platform: str
    ) -> ExecutionRecord:
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.PUBLISHING,
            extra={"last_attempt_at": _now()},
        )

    def mark_published(
        self,
        content_item_id: str,
        platform: str,
        *,
        external_post_id: str,
        media_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """Persist an external publication **before** Notion is touched."""
        extra: dict = {
            "external_post_id": external_post_id,
            "published_at": _now(),
            "notion_sync_pending": 1,
            "last_error": None,
        }
        if media_id is not None:
            # A re-affirmation without media info must never wipe it.
            extra["media_id"] = media_id
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.PUBLISHED,
            extra=extra,
        )

    def mark_image_generated(
        self,
        content_item_id: str,
        platform: str,
        *,
        path: str,
        image_hash: str,
        visual_fingerprint: str,
        provider: str,
    ) -> ExecutionRecord:
        """Record image metadata without touching the execution status."""
        return self._update(
            content_item_id,
            platform,
            status=None,
            extra={
                "generated_image_path": path,
                "generated_image_hash": image_hash,
                "visual_fingerprint": visual_fingerprint,
                "image_provider": provider,
            },
        )

    def mark_simulated(self, content_item_id: str, platform: str) -> ExecutionRecord:
        return self._update(
            content_item_id, platform, status=ExecutionStatus.SIMULATED
        )

    def mark_failed(
        self, content_item_id: str, platform: str, *, error: Optional[str]
    ) -> ExecutionRecord:
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.FAILED,
            extra={"last_error": error},
        )

    def mark_manual_review(
        self, content_item_id: str, platform: str, *, error: Optional[str]
    ) -> ExecutionRecord:
        """Terminal state: publication outcome unknown, human decides."""
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.MANUAL_REVIEW,
            extra={"last_error": error},
        )

    def mark_sync_pending(
        self, content_item_id: str, platform: str, *, error: Optional[str] = None
    ) -> ExecutionRecord:
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.SYNC_PENDING,
            extra={"notion_sync_pending": 1, "last_error": error},
        )

    def mark_synced(self, content_item_id: str, platform: str) -> ExecutionRecord:
        return self._update(
            content_item_id,
            platform,
            status=ExecutionStatus.PUBLISHED,
            extra={"notion_sync_pending": 0, "last_error": None},
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _to_record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            id=row["id"],
            content_item_id=row["content_item_id"],
            platform=row["platform"],
            execution_status=ExecutionStatus.coerce(row["execution_status"]),
            content_hash=row["content_hash"],
            generated_content=row["generated_content"],
            attempt_count=row["attempt_count"],
            external_post_id=row["external_post_id"],
            published_at=row["published_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_attempt_at=row["last_attempt_at"],
            notion_sync_pending=bool(row["notion_sync_pending"]),
            generated_image_path=row["generated_image_path"],
            generated_image_hash=row["generated_image_hash"],
            visual_fingerprint=row["visual_fingerprint"],
            image_provider=row["image_provider"],
            media_id=row["media_id"],
        )

    @staticmethod
    def _can_transition(current: ExecutionStatus, new: ExecutionStatus) -> bool:
        """Never demote a known publication or a manual-review record."""
        if current is ExecutionStatus.MANUAL_REVIEW:
            return False
        if current in PROTECTED_STATUSES:
            if new is ExecutionStatus.SYNC_PENDING:
                return current in PUBLISHED_FAMILY
            if new is ExecutionStatus.PUBLISHED:
                # mark_published on an already-published record is a no-op
                # re-affirmation; mark_synced resolves sync_pending → published.
                return current in PUBLISHED_FAMILY
            return False
        return True

    # Only these columns are ever written, and only with a bound value:
    # the SQL shape is fixed in code, never built from user content.
    _WRITABLE_COLUMNS = frozenset(
        {
            "attempt_count",
            "content_hash",
            "execution_status",
            "external_post_id",
            "generated_content",
            "generated_image_hash",
            "generated_image_path",
            "image_provider",
            "last_attempt_at",
            "last_error",
            "media_id",
            "notion_sync_pending",
            "published_at",
            "updated_at",
            "visual_fingerprint",
        }
    )

    def _update(
        self,
        content_item_id: str,
        platform: str,
        *,
        status: Optional[ExecutionStatus],
        extra: Optional[dict] = None,
    ) -> ExecutionRecord:
        record = self.create_or_get_execution(content_item_id, platform)
        if status is not None and not self._can_transition(
            record.execution_status, status
        ):
            if status is not record.execution_status:
                print(
                    "  ⚠ Estado técnico "
                    f"'{record.execution_status.value}' de {content_item_id} no se "
                    f"degrada a '{status.value}' (protección anti-duplicado)"
                )
            return record

        assignments: dict = {"updated_at": _now()}
        if status is not None:
            assignments["execution_status"] = status.value
        assignments.update(extra or {})

        unknown = sorted(set(assignments) - self._WRITABLE_COLUMNS)
        if unknown:
            raise ValueError(f"columna no escribible en executions: {unknown}")

        columns = ", ".join(f"{column} = ?" for column in assignments)
        with self._lock:
            self._connection.execute(
                f"UPDATE executions SET {columns} "
                f"WHERE content_item_id = ? AND platform = ?",
                (*assignments.values(), content_item_id, platform),
            )
            self._connection.commit()
        updated = self.get_execution(content_item_id, platform)
        assert updated is not None
        return updated


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "ExecutionRecord",
    "ExecutionStatus",
    "ExecutionStore",
    "PUBLISHED_FAMILY",
    "PROTECTED_STATUSES",
    "compute_content_hash",
]
