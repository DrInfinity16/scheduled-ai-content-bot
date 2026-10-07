"""NotionContentSource: parsing, schema validation and write-back.

Every test runs against :class:`FakeNotionGateway` — no network.
"""

from datetime import datetime, timezone

import pytest

from app.models import ContentStatus
from app.sources import (
    ContentItemNotFoundError,
    ContentSourceError,
    NotionContentSource,
    NotionSchemaError,
    NotionStatusError,
)
from app.sources.notion_client import NotionNotFoundError
from app.sources.notion_source import (
    map_status,
    page_to_item,
    parse_aware_datetime,
    parse_checkbox,
    parse_date,
    parse_rich_text,
    parse_select,
    parse_status,
    parse_title,
    parse_url,
    to_notion_datetime,
)
from tests.conftest import (
    FakeNotionGateway,
    make_notion_page,
    make_notion_schema,
)


# -- page → ContentItem ----------------------------------------------------


def test_valid_page_becomes_content_item(notion_source):
    page = make_notion_page(page_id="abc-123", topic="Tema", angle="Ángulo")
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item("abc-123")

    assert item.id == "abc-123"
    assert item.topic == "Tema"
    assert item.angle == "Ángulo"
    assert item.platform == "x"
    assert item.status is ContentStatus.SCHEDULED
    assert item.generated_content is None


def test_required_fields_are_parsed(notion_source):
    page = make_notion_page(
        topic="  Topic con espacios  ",
        angle="Angle literal",
        scheduled_at="2026-10-05T09:30:00.000+00:00",
        platform="x",
        status="Scheduled",
    )
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item(page["id"])

    assert item.topic == "  Topic con espacios  "  # user content preserved
    assert item.angle == "Angle literal"
    assert item.platform == "x"
    assert item.status is ContentStatus.SCHEDULED
    assert item.scheduled_at == datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


def test_optional_fields_use_safe_defaults(notion_source):
    page = make_notion_page(
        properties={
            "References": None,
            "Reference Notes": None,
            "Generate Image": None,
            "Image Brief": None,
            "Generated Copy": None,
            "Generated Image": None,
            "Published URL": None,
            "Error": None,
            "Platform": None,
        }
    )
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item(page["id"])

    assert item.references == []
    assert item.reference_notes is None
    assert item.generate_image is False
    assert item.image_brief is None
    assert item.generated_content is None
    assert item.generated_image is None
    assert item.published_url is None
    assert item.error is None
    assert item.platform == "x"  # default platform when the column is empty


def test_image_intent_is_mapped_without_generating(notion_source):
    page = make_notion_page()
    page["properties"]["Generate Image"]["checkbox"] = True
    page["properties"]["Image Brief"]["rich_text"] = [
        {"type": "text", "text": {"content": "Ilustración simple"}, "plain_text": "Ilustración simple"}
    ]
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item(page["id"])

    assert item.generate_image is True
    assert item.image_brief == "Ilustración simple"
    assert item.generated_image is None


def test_status_property_form_is_supported(notion_source):
    page = make_notion_page(status="Scheduled")
    assert page["properties"]["Status"]["type"] == "status"

    item = notion_source.get_item(page["id"])

    assert item.status is ContentStatus.SCHEDULED


def test_select_property_form_is_supported():
    schema = make_notion_schema()
    status_prop = schema["properties"]["Status"]
    schema["properties"]["Status"] = {
        "id": "E",
        "name": "Status",
        "type": "select",
        "select": {"options": [{"name": name} for name in ["Draft", "Scheduled", "Ready"]]},
    }
    assert status_prop["type"] == "status"

    page = make_notion_page(status="Scheduled")
    page["properties"]["Status"] = {"id": "E", "type": "select", "select": {"name": "Scheduled"}}
    gateway = FakeNotionGateway(schema=schema, pages=[page])
    source = NotionContentSource("token", "db-0000", gateway=gateway)

    assert source.get_item(page["id"]).status is ContentStatus.SCHEDULED

    source.update_item(page["id"], status=ContentStatus.READY)
    _, payload = gateway.update_calls[-1]
    assert payload["Status"] == {"select": {"name": "Ready"}}


def test_scheduled_at_honours_timezone_offset(notion_source):
    page = make_notion_page(scheduled_at="2026-10-05T11:00:00.000+02:00")
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item(page["id"])

    assert item.scheduled_at.tzinfo is not None
    assert item.scheduled_at == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def test_scheduled_at_without_offset_is_treated_as_utc(notion_source):
    page = make_notion_page(scheduled_at="2026-10-05T09:00:00.000")
    notion_source._gateway.pages[page["id"]] = page

    item = notion_source.get_item(page["id"])

    assert item.scheduled_at == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def test_scheduled_at_date_only_is_midnight_utc(notion_source):
    page = make_notion_page(scheduled_at="2026-10-05")
    notion_source._gateway.pages[page["id"]] = page

    assert notion_source.get_item(page["id"]).scheduled_at == datetime(
        2026, 10, 5, 0, 0, tzinfo=timezone.utc
    )


def test_empty_scheduled_at_is_none(notion_source):
    page = make_notion_page(scheduled_at=None)
    notion_source._gateway.pages[page["id"]] = page

    assert notion_source.get_item(page["id"]).scheduled_at is None


def test_references_are_split_by_line(notion_source):
    page = make_notion_page()
    page["properties"]["References"]["rich_text"] = [
        {"type": "text", "text": {"content": "https://a.example"}, "plain_text": "https://a.example"},
        {"type": "text", "text": {"content": "\nhttps://b.example"}, "plain_text": "\nhttps://b.example"},
    ]
    notion_source._gateway.pages[page["id"]] = page

    assert notion_source.get_item(page["id"]).references == [
        "https://a.example",
        "https://b.example",
    ]


def test_get_item_returns_none_for_unknown_page(notion_source):
    assert notion_source.get_item("no-existe") is None


def test_missing_required_property_in_page_is_clear(notion_source):
    page = make_notion_page(properties={"Angle": None})
    notion_source._gateway.pages[page["id"]] = page

    with pytest.raises(ContentSourceError) as exc:
        notion_source.get_item(page["id"])
    assert "Angle" in str(exc.value)


def test_unknown_status_is_never_invented(notion_source):
    page = make_notion_page(status="En revisión")
    notion_source._gateway.pages[page["id"]] = page

    with pytest.raises(NotionStatusError) as exc:
        notion_source.get_item(page["id"])
    assert "En revisión" in str(exc.value)
    assert "scheduled" in str(exc.value)


def test_empty_status_is_reported(notion_source):
    page = make_notion_page(status=None)
    notion_source._gateway.pages[page["id"]] = page

    with pytest.raises(NotionStatusError) as exc:
        notion_source.get_item(page["id"])
    assert "Status" in str(exc.value)


# -- querying --------------------------------------------------------------


def test_get_scheduled_items_returns_due_scheduled_items(notion_source):
    due = make_notion_page(page_id="due", scheduled_at="2020-01-01T09:00:00Z")
    future = make_notion_page(page_id="future", scheduled_at="2999-01-01T09:00:00Z")
    draft = make_notion_page(page_id="draft", status="Draft")
    ready = make_notion_page(page_id="ready", status="Ready")
    notion_source._gateway.pages = {p["id"]: p for p in [due, future, draft, ready]}

    from datetime import datetime as dt

    items = notion_source.get_scheduled_items(end=dt(2026, 10, 5, tzinfo=timezone.utc))

    assert [item.id for item in items] == ["due"]
    assert items[0].status is ContentStatus.SCHEDULED


def test_query_uses_notion_side_filtering(notion_source):
    from datetime import datetime as dt

    now = dt(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    notion_source.get_scheduled_items(end=now)

    call = notion_source._gateway.query_calls[-1]
    clauses = call["filter"]["and"]
    assert clauses[0] == {"property": "Status", "status": {"equals": "Scheduled"}}
    assert clauses[1]["property"] == "Scheduled At"
    assert clauses[1]["date"]["on_or_before"] == "2026-10-05T12:00:00Z"
    assert call["sorts"] == [
        {"property": "Scheduled At", "direction": "ascending"}
    ]


def test_query_without_bounds_only_filters_status(notion_source):
    notion_source.get_scheduled_items()

    clauses = notion_source._gateway.query_calls[-1]["filter"]["and"]
    assert len(clauses) == 1
    assert clauses[0]["property"] == "Status"


def test_select_status_filter_is_used_when_schema_says_select():
    schema = make_notion_schema()
    schema["properties"]["Status"] = {
        "id": "E",
        "name": "Status",
        "type": "select",
        "select": {"options": [{"name": "Scheduled"}]},
    }
    page = make_notion_page(scheduled_at="2020-01-01T09:00:00Z")
    page["properties"]["Status"] = {"id": "E", "type": "select", "select": {"name": "Scheduled"}}
    gateway = FakeNotionGateway(schema=schema, pages=[page])
    source = NotionContentSource("token", "db-0000", gateway=gateway)

    items = source.get_scheduled_items()

    assert [item.id for item in items] == [page["id"]]
    clause = gateway.query_calls[-1]["filter"]["and"][0]
    assert clause == {"property": "Status", "select": {"equals": "Scheduled"}}


def test_broken_record_does_not_stop_the_cycle(notion_source, capsys):
    good = make_notion_page(page_id="good", scheduled_at="2020-01-01T09:00:00Z")
    broken = make_notion_page(
        page_id="broken", scheduled_at="2020-01-02T09:00:00Z", properties={"Topic": None}
    )
    other = make_notion_page(page_id="other", scheduled_at="2020-01-03T09:00:00Z")
    notion_source._gateway.pages = {p["id"]: p for p in [broken, good, other]}

    items = notion_source.get_scheduled_items()

    assert [item.id for item in items] == ["good", "other"]
    assert "Topic" in capsys.readouterr().out


def test_get_scheduled_items_paginates():
    first = {
        "object": "list",
        "results": [make_notion_page(page_id="p1", scheduled_at="2020-01-01T09:00:00Z")],
        "has_more": True,
        "next_cursor": "1",
    }
    second = {
        "object": "list",
        "results": [make_notion_page(page_id="p2", scheduled_at="2020-01-02T09:00:00Z")],
        "has_more": False,
        "next_cursor": None,
    }
    gateway = FakeNotionGateway(paginated_results=[first, second])
    source = NotionContentSource("token", "db-0000", gateway=gateway)

    items = source.get_scheduled_items()

    assert [item.id for item in items] == ["p1", "p2"]
    assert len(gateway.query_calls) == 2
    assert gateway.query_calls[1]["start_cursor"] == "1"


# -- schema validation -----------------------------------------------------


def test_valid_schema_passes(notion_source):
    schema = notion_source.validate_schema()
    assert "Topic" in schema["properties"]
    assert notion_source._gateway.database_calls == 1
    notion_source.validate_schema()
    assert notion_source._gateway.database_calls == 1  # cached


def test_missing_required_property_is_named():
    schema = make_notion_schema()
    del schema["properties"]["Angle"]
    source = NotionContentSource(
        "token", "db-0000", gateway=FakeNotionGateway(schema=schema)
    )

    with pytest.raises(NotionSchemaError) as exc:
        source.validate_schema()
    assert "Angle" in str(exc.value)
    assert "requerida" in str(exc.value)


def test_wrong_required_type_reports_expected_vs_actual():
    schema = make_notion_schema()
    schema["properties"]["Scheduled At"] = {
        "id": "C",
        "name": "Scheduled At",
        "type": "text",
        "text": {},
    }
    source = NotionContentSource(
        "token", "db-0000", gateway=FakeNotionGateway(schema=schema)
    )

    with pytest.raises(NotionSchemaError) as exc:
        source.validate_schema()
    message = str(exc.value)
    assert "Scheduled At" in message
    assert "'date'" in message
    assert "'text'" in message


def test_status_accepts_status_or_select_but_not_rich_text():
    schema = make_notion_schema()
    schema["properties"]["Status"] = {
        "id": "E",
        "name": "Status",
        "type": "rich_text",
        "rich_text": {},
    }
    source = NotionContentSource(
        "token", "db-0000", gateway=FakeNotionGateway(schema=schema)
    )

    with pytest.raises(NotionSchemaError) as exc:
        source.validate_schema()
    assert "status o select" in str(exc.value)


def test_database_id_pointing_at_a_page_is_explained():
    source = NotionContentSource(
        "token",
        "db-0000",
        gateway=FakeNotionGateway(schema={"object": "page", "id": "x"}),
    )

    with pytest.raises(NotionSchemaError) as exc:
        source.validate_schema()
    assert "base de datos" in str(exc.value)


def test_empty_token_or_database_id_fails_fast():
    with pytest.raises(ContentSourceError):
        NotionContentSource("", "db")
    with pytest.raises(ContentSourceError):
        NotionContentSource("token", "")


# -- update_item -----------------------------------------------------------


def test_update_generated_copy(notion_source):
    item = notion_source.get_item("page-0001")

    notion_source.update_item(
        item.id, generated_content="Texto generado", status=ContentStatus.READY
    )

    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Generated Copy"]["rich_text"][0]["text"]["content"] == "Texto generado"
    assert payload["Status"] == {"status": {"name": "Ready"}}
    updated = notion_source.get_item(item.id)
    assert updated.generated_content == "Texto generado"
    assert updated.status is ContentStatus.READY


def test_update_status_uses_canonical_notion_spelling(notion_source):
    notion_source.update_item("page-0001", status="scheduled")

    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Status"] == {"status": {"name": "Scheduled"}}


def test_update_published_url(notion_source):
    notion_source.update_item(
        "page-0001", published_url="https://x.com/algo/status/1"
    )

    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Published URL"] == {"url": "https://x.com/algo/status/1"}


def test_clearing_published_url_is_explicit(notion_source):
    notion_source.update_item("page-0001", published_url=None)

    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Published URL"] == {"url": None}


def test_update_error_and_clear_it(notion_source):
    notion_source.update_item("page-0001", error="falló la generación")
    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Error"]["rich_text"][0]["text"]["content"] == "falló la generación"

    notion_source.update_item("page-0001", error=None)
    _, payload = notion_source._gateway.update_calls[-1]
    assert payload["Error"] == {"rich_text": []}


def test_update_missing_optional_property_degrades_gracefully(capsys):
    schema = make_notion_schema()
    del schema["properties"]["Published URL"]
    page = make_notion_page()
    gateway = FakeNotionGateway(schema=schema, pages=[page])
    source = NotionContentSource("token", "db-0000", gateway=gateway)

    source.update_item(page["id"], status=ContentStatus.PUBLISHED, published_url=None)

    _, payload = gateway.update_calls[-1]
    assert payload == {"Status": {"status": {"name": "Published"}}}
    assert "Published URL" in capsys.readouterr().out


def test_update_unknown_field_is_rejected(notion_source):
    with pytest.raises(ContentSourceError) as exc:
        notion_source.update_item("page-0001", banana=1)
    assert "banana" in str(exc.value)


def test_update_unknown_page_is_reported(notion_source):
    with pytest.raises(ContentItemNotFoundError):
        notion_source.update_item("no-existe", status=ContentStatus.READY)


def test_update_without_changes_makes_no_api_call(notion_source):
    item = notion_source.get_item("page-0001")

    notion_source.update_item(item.id)

    assert notion_source._gateway.update_calls == []


def test_status_option_missing_in_notion_is_clear():
    schema = make_notion_schema()
    schema["properties"]["Status"]["status"]["options"] = [{"name": "Scheduled"}]
    page = make_notion_page()
    gateway = FakeNotionGateway(schema=schema, pages=[page])
    source = NotionContentSource("token", "db-0000", gateway=gateway)

    with pytest.raises(ContentSourceError) as exc:
        source.update_item(page["id"], status=ContentStatus.PUBLISHED)
    assert "Published" in str(exc.value)
    assert "Scheduled" in str(exc.value)
    assert gateway.update_calls == []


def test_long_text_is_chunked(notion_source):
    notion_source.update_item("page-0001", generated_content="x" * 4500)

    _, payload = notion_source._gateway.update_calls[-1]
    chunks = payload["Generated Copy"]["rich_text"]
    assert [len(chunk["text"]["content"]) for chunk in chunks] == [2000, 2000, 500]


def test_gateway_failure_is_wrapped_clearly(notion_source):
    def boom(page_id, properties):
        raise ValueError("HTTP 500 simulado")

    notion_source._gateway.update_page = boom

    with pytest.raises(ContentSourceError) as exc:
        notion_source.update_item("page-0001", status=ContentStatus.READY)
    assert "page-0001" in str(exc.value)


def test_page_disappearing_between_read_and_write(notion_source):
    def gone(page_id, properties):
        raise NotionNotFoundError("desapareció")

    notion_source._gateway.update_page = gone

    with pytest.raises(ContentItemNotFoundError):
        notion_source.update_item("page-0001", status=ContentStatus.READY)


# -- pure helpers ----------------------------------------------------------


def test_parse_helpers_tolerate_missing_properties():
    assert parse_title({}, "Topic") == ""
    assert parse_rich_text({}, "Angle") == ""
    assert parse_select({}, "Platform") is None
    assert parse_status({}, "Status") is None
    assert parse_date({}, "Scheduled At") is None
    assert parse_checkbox({}, "Generate Image") is False
    assert parse_url({}, "Published URL") is None


def test_parse_helpers_tolerate_wrong_types():
    properties = {
        "Topic": {"type": "number", "number": 5},
        "Angle": {"type": "rich_text", "rich_text": "texto plano"},
        "Platform": {"type": "select", "select": "x"},
        "Status": {"type": "status", "status": None},
        "Scheduled At": {"type": "date", "date": None},
        "Published URL": {"type": "url", "url": ""},
    }
    assert parse_title(properties, "Topic") == ""
    assert parse_rich_text(properties, "Angle") == "texto plano"
    assert parse_select(properties, "Platform") == "x"
    assert parse_status(properties, "Status") is None
    assert parse_date(properties, "Scheduled At") is None
    assert parse_url(properties, "Published URL") is None


def test_parse_date_rejects_garbage(notion_source):
    page = make_notion_page(scheduled_at="no-es-fecha")
    notion_source._gateway.pages[page["id"]] = page

    from app.sources.base import ContentParseError

    with pytest.raises(ContentParseError) as exc:
        notion_source.get_item(page["id"])
    assert "no-es-fecha" in str(exc.value)


def test_datetime_round_trip():
    moment = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert to_notion_datetime(moment) == "2026-10-05T09:00:00Z"
    assert parse_aware_datetime("2026-10-05T11:00:00+02:00") == moment
    assert parse_aware_datetime("2026-10-05T09:00:00Z") == moment
    assert parse_aware_datetime("2026-10-05") == datetime(
        2026, 10, 5, 0, 0, tzinfo=timezone.utc
    )
    assert to_notion_datetime(datetime(2026, 10, 5, 9, 0)) == "2026-10-05T09:00:00Z"


def test_map_status_accepts_every_lifecycle_value():
    for name in [
        "Draft",
        "scheduled",
        "GENERATING",
        " Ready ",
        "Publishing",
        "published",
        "Failed",
    ]:
        assert isinstance(map_status(name), ContentStatus)


def test_page_to_item_rejects_non_pages():
    from app.sources.base import ContentParseError

    with pytest.raises(ContentParseError):
        page_to_item({"object": "database"})
    with pytest.raises(ContentParseError):
        page_to_item({"object": "page"})


# -- defensive parsing / encoding edges ------------------------------------


def test_chunk_text_tolerates_unexpected_shapes():
    from app.sources.notion_source import _chunk_text

    assert _chunk_text("no soy objeto") == ""
    assert _chunk_text({"type": "text"}) == ""
    assert _chunk_text({"text": {"content": "por contenido"}}) == "por contenido"
    assert _chunk_text({"plain_text": "plano"}) == "plano"


def test_parse_title_and_rich_text_accept_plain_strings():
    assert parse_title({"Topic": {"type": "title", "title": "hola"}}, "Topic") == "hola"
    assert (
        parse_rich_text({"Angle": {"type": "rich_text", "rich_text": "directo"}},
                        "Angle")
        == "directo"
    )
    assert parse_title({"Topic": {"type": "title", "title": 42}}, "Topic") == ""


def test_parse_url_keeps_non_empty_values():
    properties = {"Published URL": {"type": "url", "url": "https://x.com/u/status/1"}}
    assert parse_url(properties, "Published URL") == "https://x.com/u/status/1"
    assert parse_url({"Published URL": {"type": "url", "url": "   "}},
                     "Published URL") is None


def test_encode_plain_covers_every_supported_type():
    from app.sources.notion_source import _encode_plain

    assert _encode_plain("title", None) == {"title": []}
    assert _encode_plain("title", 7) == {
        "title": [{"type": "text", "text": {"content": "7"}}]
    }
    assert _encode_plain("rich_text", ["a", "b"])["rich_text"][0]["text"]["content"] == (
        "a\nb"
    )
    assert _encode_plain("checkbox", 1) == {"checkbox": True}
    assert _encode_plain("date", None) == {"date": None}
    moment = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert _encode_plain("date", moment) == {"date": {"start": "2026-10-05T09:00:00Z"}}
    assert _encode_plain("date", "2026-10-05") == {"date": {"start": "2026-10-05"}}
    assert _encode_plain("url", "") == {"url": None}
    assert _encode_plain("number", 3) is None


def test_schema_that_is_not_an_object_is_reported():
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=["no"]))

    with pytest.raises(NotionSchemaError) as exc:
        source.validate_schema()
    assert "respuesta inválida" in str(exc.value)


def test_property_types_requires_a_properties_block():
    schema = make_notion_schema()
    schema.pop("properties")
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=schema))

    with pytest.raises(NotionSchemaError) as exc:
        source._property_types  # cached schema without 'properties'
    assert "properties" in str(exc.value)


def test_require_property_reports_missing_and_wrong_type():
    schema = make_notion_schema()
    schema["properties"].pop("Scheduled At")
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=schema))

    with pytest.raises(NotionSchemaError) as exc:
        source._require_property("Scheduled At", ("date",))
    assert "Scheduled At" in str(exc.value)

    schema = make_notion_schema()
    schema["properties"]["Scheduled At"] = {
        "id": "C",
        "type": "rich_text",
        "rich_text": {},
    }
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=schema))

    with pytest.raises(NotionSchemaError) as exc:
        source._require_property("Scheduled At", ("date",))
    assert "se esperaba 'date'" in str(exc.value)


def test_status_options_tolerate_malformed_payloads():
    schema = make_notion_schema()
    schema["properties"]["Status"] = {"type": "status", "status": "no-so-lista"}
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=schema))

    assert source._status_options() == []


def test_select_status_falls_back_to_the_canonical_name():
    schema = make_notion_schema()
    schema["properties"]["Status"] = {
        "type": "select",
        "select": {"options": [{"name": "Hecho"}]},
    }
    source = NotionContentSource("t", "db", gateway=FakeNotionGateway(schema=schema))

    assert source._status_display_name("published") == "published"


def test_due_filter_with_start_only_uses_on_or_after():
    source = NotionContentSource(
        "t", "db", gateway=FakeNotionGateway(pages=[make_notion_page()])
    )
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)

    due = source._due_filter(start, None)

    date_clause = due["and"][1]
    assert "on_or_after" in date_clause["date"]
    assert "on_or_before" not in date_clause["date"]


def test_query_all_rejects_a_non_dict_payload():
    class BrokenGateway(FakeNotionGateway):
        def query_database(self, database_id, **kwargs):
            return ["no", "un", "objeto"]

    source = NotionContentSource("t", "db", gateway=BrokenGateway())

    with pytest.raises(ContentSourceError):
        source._query_all({"and": []})


def test_query_all_stops_when_the_cursor_disappears():
    gateway = FakeNotionGateway(
        paginated_results=[{"results": [], "has_more": True, "next_cursor": None}]
    )
    source = NotionContentSource("t", "db", gateway=gateway)

    assert source._query_all({"and": []}) == []


def test_query_all_warns_when_the_page_limit_is_reached(capsys):
    from app.sources.notion_client import MAX_QUERY_PAGES

    chunks = [
        {"results": [], "has_more": True, "next_cursor": str(i + 1)}
        for i in range(MAX_QUERY_PAGES + 3)
    ]
    gateway = FakeNotionGateway(paginated_results=chunks)
    source = NotionContentSource("t", "db", gateway=gateway)

    source._query_all({"and": []})

    assert f"límite de {MAX_QUERY_PAGES} páginas" in capsys.readouterr().out


def test_client_side_filter_drops_a_page_that_is_no_longer_scheduled():
    ready_page = make_notion_page(page_id="page-0009", status="Ready")
    gateway = FakeNotionGateway(
        paginated_results=[
            {"results": [ready_page], "has_more": False, "next_cursor": None}
        ]
    )
    source = NotionContentSource("t", "db", gateway=gateway)

    # El gateway entrega la página, pero ya no está Scheduled: se descarta.
    assert source.get_scheduled_items() == []


def test_gateway_content_source_error_is_propagated_unchanged():
    from app.sources.notion_client import NotionApiError

    class ExplodingGateway(FakeNotionGateway):
        def update_page(self, page_id, properties):
            raise NotionApiError("Notion devolvió HTTP 500: caído")

    source = NotionContentSource(
        "t", "db", gateway=ExplodingGateway(pages=[make_notion_page()])
    )

    with pytest.raises(NotionApiError) as exc:
        source.update_item("page-0001", status=ContentStatus.READY)
    assert "HTTP 500" in str(exc.value)


def test_encode_changes_skips_fields_without_a_notion_property(notion_source):
    encoded = notion_source._encode_changes({"campos_inventados": "x"})

    assert encoded == {}


def test_encode_changes_skips_properties_with_unsupported_types(capsys):
    schema = make_notion_schema()
    schema["properties"]["Generated Copy"] = {
        "id": "J",
        "type": "number",
        "number": {},
    }
    source = NotionContentSource(
        "t", "db", gateway=FakeNotionGateway(schema=schema, pages=[make_notion_page()])
    )

    encoded = source._encode_changes({"generated_content": "hola"})

    assert encoded == {}
    assert "tipo no soportado" in capsys.readouterr().out


def test_default_gateway_is_a_real_client_with_the_retry_policy():
    from app.retry import RetryPolicy
    from app.sources.notion_client import NotionApiClient

    source = NotionContentSource(
        "test-token",
        "db-0000",
        retry_policy=RetryPolicy(max_attempts=2, base_delay=0),
    )
    try:
        assert isinstance(source._gateway, NotionApiClient)
        assert source._gateway._retry_policy.max_attempts == 2
    finally:
        source._gateway._session.close()
