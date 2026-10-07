"""The temporary YAML editorial calendar.

All ``calendar.yaml`` knowledge lives here: field names, required fields,
time parsing and defaults. Nothing else in the application ever sees a raw
YAML dictionary.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from typing import Optional

import yaml

from app.config import DEFAULT_CALENDAR_FILE
from app.models import ContentItem, ContentStatus
from app.sources.base import (
    ContentItemNotFoundError,
    ContentParseError,
    ContentSource,
    ContentSourceError,
    apply_changes,
    filter_by_range,
)

REQUIRED_FIELDS = ("time", "topic", "angle")


class YAMLContentSource(ContentSource):
    """Reads scheduled items from a YAML file."""

    def __init__(self, path: str = DEFAULT_CALENDAR_FILE, refresh: bool = False):
        self.path = path
        self._refresh_on_access = refresh
        self._items: Optional[list[ContentItem]] = None

    # -- internals ---------------------------------------------------------

    def _parse(self) -> list[ContentItem]:
        if not os.path.exists(self.path):
            raise ContentSourceError(f"no se encontró {self.path}")

        with open(self.path, "r", encoding="utf-8") as handle:
            try:
                data = yaml.safe_load(handle)
            except yaml.YAMLError as exc:
                raise ContentParseError(f"{self.path} no es YAML válido: {exc}") from exc

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ContentParseError(
                f"{self.path}: la raíz debe ser un mapa con la clave 'posts'"
            )

        posts = data.get("posts", [])
        if posts is None:
            posts = []
        if not isinstance(posts, list):
            raise ContentParseError(f"{self.path}: 'posts' debe ser una lista")

        items: list[ContentItem] = []
        for index, raw in enumerate(posts):
            items.append(self._parse_entry(index, raw))
        return items

    def _parse_entry(self, index: int, raw: object) -> ContentItem:
        if not isinstance(raw, dict):
            raise ContentParseError(
                f"posts[{index}] debe ser un mapa, se obtuvo {type(raw).__name__}"
            )

        for name in REQUIRED_FIELDS:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ContentParseError(
                    f"posts[{index}] falta el campo obligatorio '{name}'"
                )

        try:
            parsed_time = time.fromisoformat(raw["time"].strip())
        except ValueError as exc:
            raise ContentParseError(
                f"posts[{index}].time debe tener formato HH:MM, se obtuvo '{raw['time']}'"
            ) from exc

        references = raw.get("references") or []
        if isinstance(references, str):
            references = [references]
        if not isinstance(references, list):
            raise ContentParseError(f"posts[{index}].references debe ser una lista")

        return ContentItem(
            id=f"calendar-{index:03d}",
            topic=raw["topic"].strip(),
            angle=raw["angle"].strip(),
            scheduled_at=datetime.combine(date.today(), parsed_time),
            platform=str(raw.get("platform", "x")),
            references=[str(reference) for reference in references],
            reference_notes=raw.get("reference_notes"),
            generate_image=bool(raw.get("generate_image", False)),
            image_brief=raw.get("image_brief"),
            status=ContentStatus.SCHEDULED,
        )

    def _load(self) -> list[ContentItem]:
        if self._items is None or self._refresh_on_access:
            self._items = self._parse()
        return self._items

    # -- public API --------------------------------------------------------

    def refresh(self) -> list[ContentItem]:
        """Force a re-read of the underlying file."""
        self._items = None
        return self._load()

    def get_scheduled_items(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[ContentItem]:
        return filter_by_range(self._load(), start, end)

    def get_item(self, item_id: str) -> Optional[ContentItem]:
        for item in self._load():
            if item.id == item_id:
                return item
        return None

    def update_item(self, item_id: str, **changes) -> ContentItem:
        item = self.get_item(item_id)
        if item is None:
            raise ContentItemNotFoundError(f"no existe el item '{item_id}' en {self.path}")
        # YAML is a read-only editorial input for now: updates are held in
        # memory for the lifetime of the process and never written back.
        return apply_changes(item, changes)


__all__ = [
    "ContentItemNotFoundError",
    "ContentParseError",
    "ContentSourceError",
    "YAMLContentSource",
]
