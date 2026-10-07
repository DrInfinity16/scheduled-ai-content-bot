from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import ContentItem, ContentStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
CALENDAR_PATH = REPO_ROOT / "calendar.yaml"


@pytest.fixture
def make_item():
    def _make(**overrides) -> ContentItem:
        defaults = dict(
            id="item-001",
            topic="Tip de Python",
            angle="Práctico y directo",
        )
        defaults.update(overrides)
        return ContentItem(**defaults)

    return _make


@pytest.fixture
def calendar_path() -> Path:
    return CALENDAR_PATH


class FakeGeminiClient:
    """Stands in for ``google.genai.Client`` without any network access."""

    def __init__(self, text: str = "Contenido generado de prueba", error=None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, model: str, contents: str):
        self.calls.append({"model": model, "contents": contents})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class FakeTweepyClient:
    """Stands in for ``tweepy.Client`` without any network access."""

    def __init__(self, post_id: str = "424242", error=None):
        self.post_id = post_id
        self.error = error
        self.calls: list[str] = []
        self.media_ids_calls: list = []

    def create_tweet(self, text: str, media_ids=None):
        self.calls.append(text)
        self.media_ids_calls.append(media_ids)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data={"id": self.post_id})


@pytest.fixture
def fake_gemini():
    return FakeGeminiClient()


@pytest.fixture
def fake_tweepy():
    return FakeTweepyClient()


class RecordingGenerator:
    def __init__(self, content: str = "tweet válido", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[ContentItem] = []

    def generate(self, item: ContentItem) -> str:
        self.calls.append(item)
        if self.error is not None:
            raise self.error
        return self.content


class RecordingPublisher:
    def __init__(self, result=None):
        from app.models import PublishResult

        self.result = result or PublishResult(status="published", id="1", platform="x")
        self.calls: list[tuple] = []

    def publish(self, item: ContentItem, generated_content: str, media=None):
        self.calls.append((item, generated_content, media))
        return self.result


@pytest.fixture
def recording_generator():
    return RecordingGenerator()


@pytest.fixture
def recording_publisher():
    return RecordingPublisher()


# ---------------------------------------------------------------------------
# Image fakes: no test ever generates a real image or uploads media.
# ---------------------------------------------------------------------------

# Smallest structurally valid PNG (signature + IHDR + IEND, 1x1 pixel).
MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeImageGenerator:
    """ImageGenerator double: returns a stored artifact, never any network."""

    def __init__(self, artifacts=None, image_bytes: bytes = MINIMAL_PNG,
                 mime_type: str = "image/png", error=None):
        from app.images.artifacts import ImageArtifactStore

        self.artifacts = artifacts
        self.image_bytes = image_bytes
        self.mime_type = mime_type
        self.error = error
        self.calls: list = []
        self.provider_name = "fake"

    def generate_image(self, visual_prompt, item, *, fingerprint, model=None):
        self.calls.append(
            {
                "prompt": visual_prompt,
                "item": item,
                "fingerprint": fingerprint,
                "model": model,
            }
        )
        if self.error is not None:
            raise self.error
        store = self.artifacts
        if store is None:
            from app.images.artifacts import ImageArtifactStore
            import tempfile

            store = ImageArtifactStore(tempfile.mkdtemp())
            self.artifacts = store
        return store.save(
            item_id=item.id,
            fingerprint=fingerprint,
            image_bytes=self.image_bytes,
            mime_type=self.mime_type,
            provider=self.provider_name,
        )


class FakeMediaUploader:
    """Stands in for the v1.1 media-upload client."""

    def __init__(self, media_id: str = "999", error=None, failures: int = 0):
        self.media_id = media_id
        self.error = error
        self.failures = failures
        self.calls: list[str] = []

    def media_upload(self, filename: str):
        self.calls.append(filename)
        if self.error is not None and (
            self.failures <= 0 or len(self.calls) <= self.failures
        ):
            raise self.error
        return SimpleNamespace(media_id_string=self.media_id)


# ---------------------------------------------------------------------------
# Notion fakes: no test ever touches the real Notion API.
# ---------------------------------------------------------------------------

NOTION_STATUS_OPTIONS = [
    "Draft",
    "Scheduled",
    "Generating",
    "Ready",
    "Publishing",
    "Published",
    "Failed",
]


def make_notion_schema(properties=None) -> dict:
    """Database retrieve payload with the schema documented in the README."""
    if properties is None:
        properties = {
            "Topic": {"id": "A", "name": "Topic", "type": "title", "title": {}},
            "Angle": {"id": "B", "name": "Angle", "type": "rich_text", "rich_text": {}},
            "Scheduled At": {"id": "C", "name": "Scheduled At", "type": "date", "date": {}},
            "Platform": {
                "id": "D",
                "name": "Platform",
                "type": "select",
                "select": {"options": [{"name": "x"}]},
            },
            "Status": {
                "id": "E",
                "name": "Status",
                "type": "status",
                "status": {"options": [{"name": name} for name in NOTION_STATUS_OPTIONS]},
            },
            "References": {"id": "F", "name": "References", "type": "rich_text", "rich_text": {}},
            "Reference Notes": {"id": "G", "name": "Reference Notes", "type": "rich_text", "rich_text": {}},
            "Generate Image": {"id": "H", "name": "Generate Image", "type": "checkbox", "checkbox": {}},
            "Image Brief": {"id": "I", "name": "Image Brief", "type": "rich_text", "rich_text": {}},
            "Generated Copy": {"id": "J", "name": "Generated Copy", "type": "rich_text", "rich_text": {}},
            "Generated Image": {"id": "K", "name": "Generated Image", "type": "url", "url": {}},
            "Published URL": {"id": "L", "name": "Published URL", "type": "url", "url": {}},
            "Error": {"id": "M", "name": "Error", "type": "rich_text", "rich_text": {}},
        }
    return {"object": "database", "id": "db-0000", "title": [], "properties": properties}


def _rich(text: str) -> list:
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def make_notion_page(
    page_id: str = "page-0001",
    topic: str = "Tip de Python",
    angle: str = "Práctico y directo",
    scheduled_at="2026-10-05T09:00:00.000+00:00",
    platform: str = "x",
    status: str = "Scheduled",
    properties: dict | None = None,
) -> dict:
    """One Notion page payload with every documented property."""
    props = {
        "Topic": {"id": "A", "type": "title", "title": _rich(topic)},
        "Angle": {"id": "B", "type": "rich_text", "rich_text": _rich(angle)},
        "Scheduled At": {
            "id": "C",
            "type": "date",
            "date": {"start": scheduled_at} if scheduled_at else None,
        },
        "Platform": {
            "id": "D",
            "type": "select",
            "select": {"name": platform} if platform else None,
        },
        "Status": {
            "id": "E",
            "type": "status",
            "status": {"name": status} if status else None,
        },
        "References": {"id": "F", "type": "rich_text", "rich_text": []},
        "Reference Notes": {"id": "G", "type": "rich_text", "rich_text": []},
        "Generate Image": {"id": "H", "type": "checkbox", "checkbox": False},
        "Image Brief": {"id": "I", "type": "rich_text", "rich_text": []},
        "Generated Copy": {"id": "J", "type": "rich_text", "rich_text": []},
        "Generated Image": {"id": "K", "type": "url", "url": None},
        "Published URL": {"id": "L", "type": "url", "url": None},
        "Error": {"id": "M", "type": "rich_text", "rich_text": []},
    }
    if properties:
        for name, value in properties.items():
            if value is None:
                props.pop(name, None)
            else:
                props[name] = value
    return {"object": "page", "id": page_id, "properties": props}


def _matches_filter(page: dict, notion_filter) -> bool:
    """Tiny evaluator for the filters this application builds."""
    if not notion_filter:
        return True
    if "and" in notion_filter:
        return all(_matches_filter(page, clause) for clause in notion_filter["and"])
    if "or" in notion_filter:
        return any(_matches_filter(page, clause) for clause in notion_filter["or"])

    property_name = notion_filter.get("property")
    kind = next((key for key in notion_filter if key != "property"), None)
    condition = notion_filter.get(kind) or {}
    prop = (page.get("properties") or {}).get(property_name) or {}

    if kind in ("status", "select"):
        current = (prop.get(kind) or {}).get("name")
        if "equals" in condition:
            return current == condition["equals"]
        return current is not None
    if kind == "date":
        from app.sources.notion_source import parse_aware_datetime

        date_value = prop.get("date")
        raw = date_value.get("start") if isinstance(date_value, dict) else None
        if not raw:
            return False
        moment = parse_aware_datetime(raw)
        if "on_or_before" in condition:
            if moment > parse_aware_datetime(condition["on_or_before"]):
                return False
        if "on_or_after" in condition:
            if moment < parse_aware_datetime(condition["on_or_after"]):
                return False
        return True
    return False


class FakeNotionGateway:
    """In-memory Notion: records every call, never opens a socket."""

    def __init__(self, schema=None, pages=(), paginated_results=None):
        self.schema = make_notion_schema() if schema is None else schema
        self.pages = {page["id"]: page for page in pages}
        self.paginated_results = paginated_results
        self.database_calls = 0
        self.query_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []

    def retrieve_database(self, database_id: str) -> dict:
        self.database_calls += 1
        return self.schema

    def query_database(
        self,
        database_id: str,
        *,
        filter=None,
        sorts=None,
        start_cursor=None,
        page_size: int = 100,
    ) -> dict:
        self.query_calls.append(
            {
                "database_id": database_id,
                "filter": filter,
                "sorts": sorts,
                "start_cursor": start_cursor,
            }
        )
        if self.paginated_results is not None:
            index = 0 if start_cursor is None else int(start_cursor)
            chunk = self.paginated_results[index]
            return chunk
        results = [
            page
            for page in self.pages.values()
            if _matches_filter(page, filter)
        ]
        return {
            "object": "list",
            "results": results,
            "has_more": False,
            "next_cursor": None,
        }

    def retrieve_page(self, page_id: str) -> dict:
        from app.sources.notion_client import NotionNotFoundError

        if page_id not in self.pages:
            raise NotionNotFoundError(f"Notion no encuentra la página {page_id}")
        return self.pages[page_id]

    def update_page(self, page_id: str, properties: dict) -> dict:
        from app.sources.notion_client import NotionNotFoundError

        if page_id not in self.pages:
            raise NotionNotFoundError(f"Notion no encuentra la página {page_id}")
        self.update_calls.append((page_id, properties))
        page = self.pages[page_id]
        merged = dict(page.get("properties") or {})
        merged.update(properties)
        page = {**page, "properties": merged}
        self.pages[page_id] = page
        return page


@pytest.fixture
def notion_gateway():
    return FakeNotionGateway(pages=[make_notion_page()])


@pytest.fixture
def notion_source(notion_gateway):
    from app.sources import NotionContentSource

    return NotionContentSource("test-token", "db-0000", gateway=notion_gateway)


class RecordingSource:
    """ContentSource double that records write-backs (and can fail on cue)."""

    def __init__(self, items=(), fail_on=None):
        self.items = {item.id: item for item in items}
        self.updates: list[tuple[str, dict]] = []
        self.fail_on = fail_on

    def get_scheduled_items(self, start=None, end=None):
        return list(self.items.values())

    def get_item(self, item_id):
        return self.items.get(item_id)

    def update_item(self, item_id, **changes):
        if self.fail_on is not None and item_id in self.fail_on:
            raise RuntimeError("Notion devolvió un error simulado")
        self.updates.append((item_id, changes))
        item = self.items.get(item_id)
        if item is not None:
            from app.sources.base import apply_changes

            apply_changes(item, changes)
        return item

    @property
    def statuses(self) -> list:
        return [changes.get("status") for _, changes in self.updates]

    @property
    def last(self) -> dict:
        return self.updates[-1][1]


@pytest.fixture
def recording_source(make_item):
    return RecordingSource([make_item()])


class FailingOrchestrator:
    """Orchestrator double that raises for selected ids."""

    def __init__(self, failing_ids=()):
        self.failing_ids = set(failing_ids)
        self.runs = []

    def run(self, item):
        self.runs.append(item)
        if item.id in self.failing_ids:
            raise RuntimeError(f"falla deliberada de {item.id}")
        return item
