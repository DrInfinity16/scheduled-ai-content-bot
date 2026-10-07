"""JSONL audit trail.

This is an append-only audit log, not the system of record. Durable
execution state (idempotency, retries, history) will live in SQLite in a
future pass.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from app.config import DEFAULT_LOG_FILE
from app.models import ContentItem, ContentStatus, PublishResult

StatusLike = Union[ContentStatus, str]


class ExecutionLog:
    def __init__(self, path: str = DEFAULT_LOG_FILE):
        self.path = Path(path)

    def record(
        self,
        item: ContentItem,
        status: StatusLike,
        *,
        content: Optional[str] = None,
        publish_status: Optional[str] = None,
        external_post_id: Optional[str] = None,
        error: Optional[str] = None,
        generated_image_path: Optional[str] = None,
        generated_image_hash: Optional[str] = None,
        media_id: Optional[str] = None,
        media_uploaded: Optional[bool] = None,
    ) -> dict:
        """Append one entry and return it.

        ``status`` keeps the historical publish vocabulary
        (``published`` / ``simulated`` / ``error``) so entries written by the
        original prototype remain comparable; ``workflow_status`` carries the
        domain lifecycle value.
        """
        workflow = ContentStatus.coerce(status)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "item_id": item.id,
            "topic": item.topic,
            "platform": item.platform,
            "content": content if content is not None else item.generated_content,
            "status": publish_status or workflow.value,
            "workflow_status": workflow.value,
            "external_post_id": external_post_id,
            "error": error,
            "generate_image": item.generate_image,
            "generated_image_path": generated_image_path,
            "generated_image_hash": generated_image_hash,
            "media_id": media_id,
            "media_uploaded": media_uploaded,
        }
        self._append(entry)
        return entry

    def record_publish(
        self,
        item: ContentItem,
        publish_result: PublishResult,
        workflow_status: ContentStatus,
        error: Optional[str] = None,
        *,
        generated_image_path: Optional[str] = None,
        generated_image_hash: Optional[str] = None,
    ) -> dict:
        return self.record(
            item,
            workflow_status,
            publish_status=publish_result.status,
            external_post_id=publish_result.id,
            error=error if error is not None else publish_result.error,
            generated_image_path=generated_image_path,
            generated_image_hash=generated_image_hash,
            media_id=publish_result.media_id,
            media_uploaded=(
                bool(publish_result.media_id)
                if publish_result.media_id is not None
                else None
            ),
        )

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


__all__ = ["ExecutionLog"]
