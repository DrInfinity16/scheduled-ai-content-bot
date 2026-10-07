"""Content source abstraction.

This is the boundary that lets the editorial origin of the content change
(YAML today, Notion later) without touching the scheduler, generator,
validator, publisher or orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.models import ContentItem, ContentStatus

UPDATEABLE_FIELDS = frozenset(
    {
        "topic",
        "angle",
        "scheduled_at",
        "platform",
        "references",
        "reference_notes",
        "generate_image",
        "image_brief",
        "generated_content",
        "generated_image",
        "published_url",
        "error",
        "status",
    }
)


class ContentSourceError(Exception):
    """Base error for every content source."""


class ContentParseError(ContentSourceError):
    """The underlying document could not be turned into ContentItems."""


class ContentItemNotFoundError(ContentSourceError):
    """No item exists with the requested id."""


class ContentSource(ABC):
    """Where scheduled editorial content comes from."""

    @abstractmethod
    def get_scheduled_items(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[ContentItem]:
        """Return the known items, optionally bounded by a date range."""

    @abstractmethod
    def get_item(self, item_id: str) -> Optional[ContentItem]:
        """Return a single item or ``None`` when it does not exist."""

    @abstractmethod
    def update_item(self, item_id: str, **changes) -> ContentItem:
        """Apply changes to an item and return the updated copy."""


def apply_changes(item: ContentItem, changes: dict) -> ContentItem:
    """Shared, source-agnostic mutation helper.

    Unknown fields raise immediately so a typo never silently disappears.
    """
    unknown = sorted(set(changes) - UPDATEABLE_FIELDS)
    if unknown:
        raise ContentSourceError(
            f"campo(s) desconocido(s) en update_item: {', '.join(unknown)}"
        )
    for key, value in changes.items():
        setattr(item, key, value)
    if not isinstance(item.status, ContentStatus):
        item.status = ContentStatus.coerce(item.status)
    return item


def filter_by_range(
    items: Iterable[ContentItem],
    start: Optional[datetime],
    end: Optional[datetime],
) -> list[ContentItem]:
    """Filter items bounded by ``start`` / ``end``.

    Datetimes are normalised to UTC before comparing so a naive value and a
    timezone-aware value are never compared directly (Notion returns
    offsets; YAML has no offset and is treated as UTC).
    """

    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    lower = _as_utc(start) if start is not None else None
    upper = _as_utc(end) if end is not None else None

    selected: list[ContentItem] = []
    for item in items:
        if item.scheduled_at is None:
            if lower is None and upper is None:
                selected.append(item)
            continue
        moment = _as_utc(item.scheduled_at)
        if lower is not None and moment < lower:
            continue
        if upper is not None and moment > upper:
            continue
        selected.append(item)
    return selected
