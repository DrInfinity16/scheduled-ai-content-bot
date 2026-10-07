"""Notion editorial calendar as a content source.

This module owns every piece of Notion knowledge: property names, property
parsing, status mapping, due-date filtering and write-back encoding. The
rest of the application only ever sees :class:`ContentItem` objects, so the
scheduler, orchestrator, generator, validator and publisher stay Notion-free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models import DEFAULT_PLATFORM, ContentItem, ContentStatus
from app.sources.base import (
    ContentItemNotFoundError,
    ContentParseError,
    ContentSource,
    ContentSourceError,
    UPDATEABLE_FIELDS,
    apply_changes,
    filter_by_range,
)
from app.sources.notion_client import (
    MAX_QUERY_PAGES,
    NotionApiClient,
    NotionNotFoundError,
)

# -- schema ----------------------------------------------------------------

TOPIC_PROPERTY = "Topic"
ANGLE_PROPERTY = "Angle"
SCHEDULED_AT_PROPERTY = "Scheduled At"
PLATFORM_PROPERTY = "Platform"
STATUS_PROPERTY = "Status"

SCHEDULED_STATUS_NAME = "Scheduled"

REQUIRED_PROPERTY_TYPES = {
    TOPIC_PROPERTY: ("title",),
    ANGLE_PROPERTY: ("rich_text",),
    SCHEDULED_AT_PROPERTY: ("date",),
    PLATFORM_PROPERTY: ("select",),
    STATUS_PROPERTY: ("status", "select"),
}

# ContentItem field -> Notion property name.
FIELD_PROPERTIES = {
    "topic": TOPIC_PROPERTY,
    "angle": ANGLE_PROPERTY,
    "scheduled_at": SCHEDULED_AT_PROPERTY,
    "platform": PLATFORM_PROPERTY,
    "status": STATUS_PROPERTY,
    "references": "References",
    "reference_notes": "Reference Notes",
    "generate_image": "Generate Image",
    "image_brief": "Image Brief",
    "generated_content": "Generated Copy",
    "generated_image": "Generated Image",
    "published_url": "Published URL",
    "error": "Error",
}

TEXT_LIMIT = 2000  # Notion rich text objects cap at 2000 characters


class NotionSchemaError(ContentParseError):
    """The Notion database does not match the expected schema."""


class NotionStatusError(ContentParseError):
    """A Status value that the lifecycle does not understand."""


# -- datetime helpers ------------------------------------------------------


def parse_aware_datetime(raw: str) -> datetime:
    """Parse a Notion date string into an aware UTC datetime.

    Values without an offset are treated as UTC so a naive and an aware
    datetime are never compared directly.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_notion_datetime(moment: datetime) -> str:
    """Format an aware datetime the way Notion expects it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- property parsing ------------------------------------------------------


def _property_value(properties: dict, name: str, *types: str):
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    if kind in types:
        return prop.get(kind)
    for candidate in types:
        if candidate in prop:
            return prop[candidate]
    return None


def _chunk_text(chunk: object) -> str:
    if not isinstance(chunk, dict):
        return ""
    plain = chunk.get("plain_text")
    if isinstance(plain, str):
        return plain
    inner = chunk.get("text")
    if isinstance(inner, dict) and isinstance(inner.get("content"), str):
        return inner["content"]
    return ""


def _name_of(value: object) -> Optional[str]:
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    if isinstance(value, str):
        return value
    return None


def parse_title(properties: dict, name: str) -> str:
    chunks = _property_value(properties, name, "title")
    if isinstance(chunks, str):
        return chunks
    if not isinstance(chunks, list):
        return ""
    return "".join(_chunk_text(chunk) for chunk in chunks)


def parse_rich_text(properties: dict, name: str) -> str:
    chunks = _property_value(properties, name, "rich_text")
    if isinstance(chunks, str):
        return chunks
    if not isinstance(chunks, list):
        return ""
    return "".join(_chunk_text(chunk) for chunk in chunks)


def parse_select(properties: dict, name: str) -> Optional[str]:
    return _name_of(_property_value(properties, name, "select"))


def parse_status(properties: dict, name: str) -> Optional[str]:
    return _name_of(_property_value(properties, name, "status", "select"))


def parse_date(properties: dict, name: str) -> Optional[datetime]:
    value = _property_value(properties, name, "date")
    raw = value if isinstance(value, str) else None
    if isinstance(value, dict):
        raw = value.get("start") if isinstance(value.get("start"), str) else None
    if not raw:
        return None
    try:
        return parse_aware_datetime(raw)
    except ValueError as exc:
        raise ContentParseError(
            f"fecha de Notion inválida en '{name}': '{raw}'"
        ) from exc


def parse_checkbox(properties: dict, name: str) -> bool:
    return bool(_property_value(properties, name, "checkbox"))


def parse_url(properties: dict, name: str) -> Optional[str]:
    value = _property_value(properties, name, "url")
    if isinstance(value, str) and value.strip():
        return value
    return None


def map_status(raw: Optional[str], *, context: str = "") -> ContentStatus:
    """Map a Notion Status/Select value onto the domain lifecycle.

    Unknown values are never silently invented: they raise with context.
    """
    if raw is None or not raw.strip():
        raise NotionStatusError(f"{context} la propiedad Status está vacía")
    status = _STATUS_BY_NAME.get(raw.strip().lower())
    if status is None:
        expected = ", ".join(sorted(value for value in _STATUS_BY_NAME))
        raise NotionStatusError(
            f"{context} estado de Notion no soportado: '{raw}' (esperados: {expected})"
        )
    return status


_STATUS_BY_NAME = {status.value: status for status in ContentStatus}


def page_to_item(page: dict) -> ContentItem:
    """Convert one raw Notion page payload into a :class:`ContentItem`."""
    if not isinstance(page, dict) or page.get("object") != "page":
        raise ContentParseError("Notion devolvió algo que no es una página")
    page_id = page.get("id") or "?"
    properties = page.get("properties")
    if not isinstance(properties, dict):
        raise ContentParseError(f"[{page_id}] la página no contiene 'properties'")

    for required in (TOPIC_PROPERTY, ANGLE_PROPERTY, STATUS_PROPERTY):
        if required not in properties:
            raise ContentParseError(
                f"[{page_id}] falta la propiedad requerida '{required}' en la página"
            )

    references_text = parse_rich_text(properties, "References")
    references = [line for line in references_text.split("\n") if line.strip()]

    return ContentItem(
        id=str(page_id),
        topic=parse_title(properties, TOPIC_PROPERTY),
        angle=parse_rich_text(properties, ANGLE_PROPERTY),
        scheduled_at=parse_date(properties, SCHEDULED_AT_PROPERTY),
        platform=parse_select(properties, PLATFORM_PROPERTY) or DEFAULT_PLATFORM,
        references=references,
        reference_notes=parse_rich_text(properties, "Reference Notes") or None,
        generate_image=parse_checkbox(properties, "Generate Image"),
        image_brief=parse_rich_text(properties, "Image Brief") or None,
        generated_content=parse_rich_text(properties, "Generated Copy") or None,
        generated_image=parse_url(properties, "Generated Image")
        or (parse_rich_text(properties, "Generated Image") or None),
        published_url=parse_url(properties, "Published URL"),
        error=parse_rich_text(properties, "Error") or None,
        status=map_status(
            parse_status(properties, STATUS_PROPERTY), context=f"[{page_id}] "
        ),
    )


# -- encoding --------------------------------------------------------------


def _text_chunks(text: str) -> list[dict]:
    if not text:
        return []
    return [
        {"type": "text", "text": {"content": text[index : index + TEXT_LIMIT]}}
        for index in range(0, len(text), TEXT_LIMIT)
    ]


def _encode_plain(prop_type: str, value: object) -> Optional[dict]:
    if prop_type == "title":
        return {"title": _text_chunks("" if value is None else str(value))}
    if prop_type == "rich_text":
        if isinstance(value, (list, tuple)):
            text = "\n".join(str(entry) for entry in value)
        elif value is None:
            text = ""
        else:
            text = str(value)
        return {"rich_text": _text_chunks(text)}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "date":
        if value is None:
            return {"date": None}
        if isinstance(value, datetime):
            return {"date": {"start": to_notion_datetime(value)}}
        return {"date": {"start": str(value)}}
    if prop_type == "url":
        return {"url": value if isinstance(value, str) and value else None}
    return None


# -- source ----------------------------------------------------------------


class NotionContentSource(ContentSource):
    """Reads and writes the editorial calendar stored in Notion."""

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        gateway: Optional[object] = None,
        retry_policy: Optional["RetryPolicy"] = None,
    ):
        if not token:
            raise ContentSourceError("NOTION_TOKEN vacío en .env")
        if not database_id:
            raise ContentSourceError("NOTION_DATABASE_ID vacío en .env")
        self.database_id = database_id
        if gateway is None:
            from app.retry import RetryPolicy

            self._gateway = NotionApiClient(
                token, retry_policy=retry_policy or RetryPolicy()
            )
        else:
            self._gateway = gateway
        self._schema: Optional[dict] = None

    # -- schema ------------------------------------------------------------

    def validate_schema(self) -> dict:
        """Fail clearly when the database does not match the expected schema."""
        schema = self._raw_schema()
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise NotionSchemaError(
                "Notion devolvió un objeto sin 'properties': "
                "NOTION_DATABASE_ID debe apuntar a una base de datos, no a una página"
            )

        for name, expected in REQUIRED_PROPERTY_TYPES.items():
            prop = properties.get(name)
            if not isinstance(prop, dict):
                raise NotionSchemaError(
                    f"falta la propiedad requerida '{name}' en la base de datos de Notion"
                )
            actual = prop.get("type")
            if actual not in expected:
                expected_text = " o ".join(expected)
                raise NotionSchemaError(
                    f"la propiedad '{name}' tiene tipo '{actual or 'desconocido'}', "
                    f"se esperaba '{expected_text}'"
                )
        return schema

    def _raw_schema(self) -> dict:
        if self._schema is None:
            schema = self._gateway.retrieve_database(self.database_id)
            if not isinstance(schema, dict):
                raise NotionSchemaError("Notion devolvió una respuesta inválida")
            self._schema = schema
        return self._schema

    @property
    def _property_types(self) -> dict:
        schema = self._raw_schema()
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise NotionSchemaError("la base de datos de Notion no tiene 'properties'")
        return {
            name: (prop or {}).get("type")
            for name, prop in properties.items()
            if isinstance(prop, dict)
        }

    def _require_property(self, name: str, expected: tuple) -> str:
        actual = self._property_types.get(name)
        if actual is None:
            raise NotionSchemaError(
                f"falta la propiedad requerida '{name}' en la base de datos de Notion"
            )
        if expected and actual not in expected:
            raise NotionSchemaError(
                f"la propiedad '{name}' tiene tipo '{actual}', "
                f"se esperaba '{' o '.join(expected)}'"
            )
        return actual

    def _status_options(self) -> list[str]:
        prop = self._raw_schema().get("properties", {}).get(STATUS_PROPERTY) or {}
        bucket = prop.get("status") or prop.get("select") or {}
        options = bucket.get("options") if isinstance(bucket, dict) else None
        if not isinstance(options, list):
            return []
        return [
            option.get("name")
            for option in options
            if isinstance(option, dict) and isinstance(option.get("name"), str)
        ]

    def _status_display_name(self, value: object) -> str:
        status = ContentStatus.coerce(value)  # type: ignore[arg-type]
        wanted = status.value
        for option in self._status_options():
            if option.strip().lower() == wanted:
                return option
        if self._property_types.get(STATUS_PROPERTY) == "status" and self._status_options():
            raise ContentSourceError(
                f"la propiedad Status de Notion no tiene la opción "
                f"'{wanted.capitalize()}' "
                f"(opciones: {', '.join(sorted(self._status_options()))})"
            )
        return wanted

    # -- reading -----------------------------------------------------------

    def _status_filter(self) -> dict:
        prop_type = self._require_property(STATUS_PROPERTY, ("status", "select"))
        return {"property": STATUS_PROPERTY, prop_type: {"equals": SCHEDULED_STATUS_NAME}}

    def _due_filter(self, start: Optional[datetime], end: Optional[datetime]) -> dict:
        clauses = [self._status_filter()]
        if start is not None or end is not None:
            self._require_property(SCHEDULED_AT_PROPERTY, ("date",))
            bounds: dict = {}
            if start is not None:
                bounds["on_or_after"] = to_notion_datetime(start)
            if end is not None:
                bounds["on_or_before"] = to_notion_datetime(end)
            clauses.append({"property": SCHEDULED_AT_PROPERTY, "date": bounds})
        return {"and": clauses}

    def _query_all(self, notion_filter: dict) -> list[dict]:
        self._require_property(SCHEDULED_AT_PROPERTY, ("date",))
        results: list[dict] = []
        cursor: Optional[str] = None
        pages_read = 0
        while True:
            payload = self._gateway.query_database(
                self.database_id,
                filter=notion_filter,
                sorts=[{"property": SCHEDULED_AT_PROPERTY, "direction": "ascending"}],
                start_cursor=cursor,
            )
            if not isinstance(payload, dict):
                raise ContentSourceError("Notion devolvió una consulta inválida")
            results.extend(payload.get("results") or [])
            if not payload.get("has_more"):
                return results
            cursor = payload.get("next_cursor")
            pages_read += 1
            if not cursor:
                return results
            if pages_read >= MAX_QUERY_PAGES:
                print(
                    f"  ⚠ Se alcanzó el límite de {MAX_QUERY_PAGES} páginas de Notion; "
                    "la lista de items está truncada"
                )
                return results

    def get_scheduled_items(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> list[ContentItem]:
        """Scheduled, due items straight from Notion (server-side filter)."""
        pages = self._query_all(self._due_filter(start, end))
        items: list[ContentItem] = []
        for page in pages:
            try:
                item = page_to_item(page)
            except ContentSourceError as exc:
                # One broken record must not stop the whole polling cycle.
                print(f"  ✗ Registro de Notion omitido: {exc}")
                continue
            if item.status is not ContentStatus.SCHEDULED:
                continue
            items.append(item)
        return filter_by_range(items, start, end)

    def get_item(self, item_id: str) -> Optional[ContentItem]:
        try:
            page = self._gateway.retrieve_page(item_id)
        except NotionNotFoundError:
            return None
        return page_to_item(page)

    # -- writing -----------------------------------------------------------

    def update_item(self, item_id: str, **changes) -> ContentItem:
        unknown = sorted(set(changes) - UPDATEABLE_FIELDS)
        if unknown:
            raise ContentSourceError(
                f"campo(s) desconocido(s) en update_item: {', '.join(unknown)}"
            )

        item = self.get_item(item_id)
        if item is None:
            raise ContentItemNotFoundError(f"no existe la página '{item_id}' en Notion")

        properties = self._encode_changes(changes)
        if properties:
            try:
                self._gateway.update_page(item_id, properties)
            except NotionNotFoundError as exc:
                raise ContentItemNotFoundError(
                    f"no existe la página '{item_id}' en Notion"
                ) from exc
            except ContentSourceError:
                raise
            except Exception as exc:
                raise ContentSourceError(
                    f"no se pudo actualizar '{item_id}' en Notion: {exc}"
                ) from exc

        return apply_changes(item, changes)

    def _encode_changes(self, changes: dict) -> dict:
        types = self._property_types
        encoded: dict = {}
        for field, value in changes.items():
            prop_name = FIELD_PROPERTIES.get(field)
            if prop_name is None:
                continue
            if prop_name not in types:
                # Optional column missing: degrade gracefully instead of failing.
                print(
                    f"  ⚠ La propiedad '{prop_name}' no existe en Notion — "
                    f"se omite '{field}'"
                )
                continue
            prop_type = types[prop_name]
            if prop_type in ("status", "select"):
                payload = {
                    prop_type: {"name": self._status_display_name(value)}
                }
            else:
                payload = _encode_plain(prop_type, value)
            if payload is None:
                print(
                    f"  ⚠ La propiedad '{prop_name}' tiene un tipo no soportado "
                    f"('{prop_type}') — se omite '{field}'"
                )
                continue
            encoded[prop_name] = payload
        return encoded


__all__ = [
    "FIELD_PROPERTIES",
    "NotionApiClient",
    "NotionSchemaError",
    "NotionStatusError",
    "NotionContentSource",
    "map_status",
    "page_to_item",
    "parse_checkbox",
    "parse_date",
    "parse_rich_text",
    "parse_select",
    "parse_status",
    "parse_title",
    "parse_url",
    "parse_aware_datetime",
    "to_notion_datetime",
]
