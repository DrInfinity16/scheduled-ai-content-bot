"""Notion HTTP boundary: URLs, headers, status codes, error translation."""

import pytest

from app.sources.notion_client import (
    NOTION_API_BASE,
    NOTION_VERSION,
    NotionApiClient,
    NotionApiError,
    NotionAuthError,
    NotionNotFoundError,
    _error_message,
)

TOKEN = "secret_abc123"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Queues of responses: one entry per call (last one repeats)."""

    def __init__(self, *responses, error=None):
        self.responses = list(responses) or [FakeResponse()]
        self.error = error
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def client(session, **kwargs):
    return NotionApiClient(TOKEN, session=session, **kwargs)


def test_empty_token_fails_before_any_request():
    with pytest.raises(NotionAuthError) as exc:
        NotionApiClient("", session=FakeSession())
    assert "NOTION_TOKEN" in str(exc.value)


def test_request_carries_auth_version_and_timeout():
    session = FakeSession()
    client(session).retrieve_database("db-1")

    call = session.calls[0]
    assert call["url"] == f"{NOTION_API_BASE}/databases/db-1"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["Notion-Version"] == NOTION_VERSION
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["timeout"] == 30


def test_trailing_slash_in_base_url_is_normalised():
    session = FakeSession()
    NotionApiClient(TOKEN, session=session, base_url="https://example.test/v1/").retrieve_page(
        "p1"
    )

    assert session.calls[0]["url"] == "https://example.test/v1/pages/p1"


def test_custom_timeout_is_used():
    session = FakeSession()
    client(session, timeout=5).retrieve_page("p1")

    assert session.calls[0]["timeout"] == 5


def test_query_database_sends_filter_sorts_and_cursor():
    session = FakeSession(FakeResponse(payload={"results": []}))
    payload = client(session).query_database(
        "db-1",
        filter={"property": "Status", "status": {"equals": "Scheduled"}},
        sorts=[{"property": "Scheduled At", "direction": "ascending"}],
        start_cursor="cursor-2",
        page_size=42,
    )

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/databases/db-1/query")
    assert call["json"]["filter"]["status"]["equals"] == "Scheduled"
    assert call["json"]["sorts"][0]["direction"] == "ascending"
    assert call["json"]["start_cursor"] == "cursor-2"
    assert call["json"]["page_size"] == 42
    assert payload == {"results": []}


def test_query_database_omits_empty_optional_keys():
    session = FakeSession()
    client(session).query_database("db-1")

    body = session.calls[0]["json"]
    assert body == {"page_size": 100}


def test_update_page_sends_properties():
    session = FakeSession()
    client(session).update_page("p1", {"Status": {"status": {"name": "Ready"}}})

    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/pages/p1")
    assert call["json"] == {"properties": {"Status": {"status": {"name": "Ready"}}}}


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_translated(status):
    session = FakeSession(FakeResponse(status, {"message": "token invalido"}))

    with pytest.raises(NotionAuthError) as exc:
        client(session).retrieve_database("db-1")

    assert "NOTION_TOKEN" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_not_found_is_translated():
    session = FakeSession(FakeResponse(404, {"message": "page not found"}))

    with pytest.raises(NotionNotFoundError) as exc:
        client(session).retrieve_page("missing")

    assert "HTTP 404" in str(exc.value)
    assert "page not found" in str(exc.value)


def test_other_http_errors_include_status_and_detail():
    session = FakeSession(FakeResponse(400, {"message": "bad filter"}))

    with pytest.raises(NotionApiError) as exc:
        client(session).query_database("db-1")

    assert "HTTP 400" in str(exc.value)
    assert "bad filter" in str(exc.value)


def test_network_failure_is_translated():
    from app.retry import RetryPolicy

    # Zero-delay policy: same behaviour, no wall-clock waiting in tests.
    session = FakeSession(error=ConnectionError("dns falló"))

    with pytest.raises(NotionApiError) as exc:
        NotionApiClient(
            TOKEN, session=session, retry_policy=RetryPolicy(base_delay=0)
        ).retrieve_database("db-1")

    assert "no se pudo contactar" in str(exc.value)
    assert "dns falló" in str(exc.value)


def test_non_json_body_is_reported():
    session = FakeSession(FakeResponse(200, json_error=True))

    with pytest.raises(NotionApiError) as exc:
        client(session).retrieve_database("db-1")

    assert "no es JSON" in str(exc.value)


def test_empty_body_is_treated_as_an_empty_object():
    session = FakeSession(FakeResponse(payload=None))

    assert client(session).retrieve_database("db-1") == {}


def test_error_message_falls_back_to_sin_detalle():
    class NoJsonResponse:
        status_code = 500

        def json(self):
            raise ValueError("boom")

    assert _error_message(NoJsonResponse()) == "sin detalle"


def test_error_message_ignores_empty_and_non_dict_payloads():
    class ListResponse:
        def json(self):
            return ["no", "message"]

    class EmptyMessage:
        def json(self):
            return {"message": ""}

    assert _error_message(ListResponse()) == "sin detalle"
    assert _error_message(EmptyMessage()) == "sin detalle"


def test_default_session_is_a_real_requests_session():
    client = NotionApiClient(TOKEN)

    import requests

    assert isinstance(client._session, requests.Session)
    assert client._base_url == NOTION_API_BASE
    assert client._notion_version == NOTION_VERSION


def test_public_api_surface():
    from app.sources.notion_client import MAX_QUERY_PAGES

    session = FakeSession()
    api = client(session)

    assert MAX_QUERY_PAGES >= 1
    assert api.retrieve_database("db") == {}
    assert api.retrieve_page("p") == {}
    assert api.update_page("p", {}) == {}
    assert api.query_database("db") == {}
