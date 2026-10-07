from datetime import datetime

import pytest

from app.models import ContentStatus
from app.sources import (
    ContentItemNotFoundError,
    ContentParseError,
    ContentSourceError,
    YAMLContentSource,
)


def write_calendar(path, body: str):
    path.write_text(body, encoding="utf-8")
    return YAMLContentSource(str(path))


def test_parses_repo_calendar_into_content_items(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    items = source.get_scheduled_items()

    assert len(items) == 3
    assert [item.topic for item in items] == [
        "Tip de Python para principiantes",
        "Reflexión sobre construir en público",
        "Curiosidad sobre IA aplicada",
    ]
    for item in items:
        assert item.id.startswith("calendar-")
        assert item.angle
        assert isinstance(item.scheduled_at, datetime)
        assert item.platform == "x"
        assert item.status is ContentStatus.SCHEDULED
    assert [item.scheduled_at.hour for item in items] == [9, 14, 19]
    assert [item.scheduled_at.minute for item in items] == [0, 0, 0]
    assert len({item.id for item in items}) == 3


def test_missing_required_field_is_reported_clearly(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        "posts:\n  - time: '09:00'\n    topic: 'Solo tema'\n",
    )
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "posts[0]" in str(exc.value)
    assert "angle" in str(exc.value)


def test_missing_time_is_reported_clearly(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml", "posts:\n  - topic: 'T'\n    angle: 'A'\n"
    )
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "time" in str(exc.value)


def test_invalid_time_is_reported_clearly(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        "posts:\n  - time: 'nueve'\n    topic: 'T'\n    angle: 'A'\n",
    )
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "HH:MM" in str(exc.value)


def test_non_list_posts_is_reported_clearly(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "posts: nope\n")
    with pytest.raises(ContentParseError):
        source.get_scheduled_items()


def test_missing_file_is_reported_clearly(tmp_path):
    source = YAMLContentSource(str(tmp_path / "no-existe.yaml"))
    with pytest.raises(ContentSourceError) as exc:
        source.get_scheduled_items()
    assert "no-existe.yaml" in str(exc.value)


def test_optional_future_fields_use_defaults(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        "posts:\n  - time: '09:00'\n    topic: 'T'\n    angle: 'A'\n",
    )
    item = source.get_scheduled_items()[0]

    assert item.platform == "x"
    assert item.references == []
    assert item.reference_notes is None
    assert item.generate_image is False
    assert item.image_brief is None
    assert item.generated_content is None
    assert item.generated_image is None
    assert item.status is ContentStatus.SCHEDULED


def test_optional_future_fields_are_parsed(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        """
posts:
  - time: "09:00"
    topic: "T"
    angle: "A"
    platform: "x"
    references:
      - "https://example.com/uno"
      - "https://example.com/dos"
    reference_notes: "Cita al autor"
    generate_image: true
    image_brief: "Ilustración minimalista"
""",
    )
    item = source.get_scheduled_items()[0]

    assert item.references == ["https://example.com/uno", "https://example.com/dos"]
    assert item.reference_notes == "Cita al autor"
    assert item.generate_image is True
    assert item.image_brief == "Ilustración minimalista"
    assert item.generated_image is None


def test_get_item_returns_item_or_none(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    items = source.get_scheduled_items()

    assert source.get_item(items[0].id) is items[0]
    assert source.get_item("no-existe") is None


def test_update_item_applies_changes_in_memory(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    item = source.get_scheduled_items()[0]

    updated = source.update_item(
        item.id,
        generated_content="texto",
        status=ContentStatus.PUBLISHED,
    )

    assert updated.generated_content == "texto"
    assert updated.status is ContentStatus.PUBLISHED
    assert source.get_item(item.id).generated_content == "texto"


def test_update_item_rejects_unknown_fields(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    item = source.get_scheduled_items()[0]

    with pytest.raises(ContentSourceError) as exc:
        source.update_item(item.id, banana=1)
    assert "banana" in str(exc.value)


def test_update_item_rejects_unknown_id(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    with pytest.raises(ContentItemNotFoundError):
        source.update_item("no-existe", topic="x")


def test_update_item_coerces_status_strings(calendar_path):
    source = YAMLContentSource(str(calendar_path))
    item = source.get_scheduled_items()[0]

    updated = source.update_item(item.id, status="ready")

    assert updated.status is ContentStatus.READY


def test_get_scheduled_items_filters_by_range(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        """
posts:
  - time: "09:00"
    topic: "Mañana"
    angle: "A"
  - time: "19:00"
    topic: "Noche"
    angle: "B"
  - time: "23:30"
    topic: "Madrugada"
    angle: "C"
""",
    )
    items = source.get_scheduled_items()
    noon = datetime.combine(items[0].scheduled_at.date(), datetime.min.time()).replace(
        hour=12
    )

    only_evening = source.get_scheduled_items(
        start=noon, end=noon.replace(hour=23)
    )
    assert [item.topic for item in only_evening] == ["Noche"]


def test_filter_by_range_keeps_unscheduled_items_only_without_bounds(make_item):
    from app.sources.base import filter_by_range

    unscheduled = make_item(scheduled_at=None)

    assert filter_by_range([unscheduled], None, None) == [unscheduled]
    assert filter_by_range([unscheduled], datetime(2026, 1, 1), datetime(2026, 1, 2)) == []


def test_null_posts_key_yields_no_items(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "posts:\n")
    assert source.get_scheduled_items() == []


def test_invalid_yaml_is_reported_clearly(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "posts: [unclosed\n  : bad")
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "YAML válido" in str(exc.value)


def test_empty_document_yields_no_items(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "")
    assert source.get_scheduled_items() == []


def test_root_must_be_a_mapping(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "- solo\n- lista\n")
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "posts" in str(exc.value)


def test_posts_must_be_a_list(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "posts:\n  topic: 'T'\n")
    with pytest.raises(ContentParseError):
        source.get_scheduled_items()


def test_entry_must_be_a_mapping(tmp_path):
    source = write_calendar(tmp_path / "calendar.yaml", "posts:\n  - 'texto crudo'\n")
    with pytest.raises(ContentParseError) as exc:
        source.get_scheduled_items()
    assert "posts[0]" in str(exc.value)


def test_single_reference_string_becomes_a_list(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        "posts:\n  - time: '09:00'\n    topic: 'T'\n    angle: 'A'\n"
        "    references: 'https://example.com'\n",
    )
    assert source.get_scheduled_items()[0].references == ["https://example.com"]


def test_references_must_be_a_list(tmp_path):
    source = write_calendar(
        tmp_path / "calendar.yaml",
        "posts:\n  - time: '09:00'\n    topic: 'T'\n    angle: 'A'\n    references: 5\n",
    )
    with pytest.raises(ContentParseError):
        source.get_scheduled_items()


def test_refresh_rereads_the_file(tmp_path):
    path = tmp_path / "calendar.yaml"
    path.write_text("posts:\n  - time: '09:00'\n    topic: 'Uno'\n    angle: 'A'\n", encoding="utf-8")
    source = YAMLContentSource(str(path))

    assert [i.topic for i in source.get_scheduled_items()] == ["Uno"]

    path.write_text("posts:\n  - time: '10:00'\n    topic: 'Dos'\n    angle: 'B'\n", encoding="utf-8")
    assert [i.topic for i in source.refresh()] == ["Dos"]
    assert [i.topic for i in source.get_scheduled_items()] == ["Dos"]
