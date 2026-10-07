"""Notion HTTP boundary.

Everything that talks to ``https://api.notion.com`` lives here: URLs,
headers, status codes and error translation. No other module sees raw HTTP,
and the authorization header is never logged or echoed back in errors.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from app.retry import RetryPolicy, with_retries
from app.sources.base import ContentSourceError

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_QUERY_PAGES = 20


class NotionApiError(ContentSourceError):
    """Notion answered with an error we cannot work with.

    ``status_code`` (when known) is what lets the retry policy separate a
    transient 429/5xx from a permanent 4xx.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class NotionNotFoundError(NotionApiError):
    """The database or page does not exist (or is not shared with the token)."""


class NotionAuthError(NotionApiError):
    """Notion rejected the integration token."""


class NotionGateway(Protocol):
    """The four Notion operations the content source depends on."""

    def retrieve_database(self, database_id: str) -> dict: ...

    def query_database(
        self,
        database_id: str,
        *,
        filter: Optional[dict] = None,
        sorts: Optional[list] = None,
        start_cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> dict: ...

    def retrieve_page(self, page_id: str) -> dict: ...

    def update_page(self, page_id: str, properties: dict) -> dict: ...


class NotionApiClient:
    """Minimal Notion REST client (API version ``2022-06-28``).

    ``session`` is any object exposing ``request(method, url, ...)``, which
    keeps tests off the network.
    """

    def __init__(
        self,
        token: str,
        *,
        session: Optional[Any] = None,
        base_url: str = NOTION_API_BASE,
        notion_version: str = NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        if not token:
            raise NotionAuthError("NOTION_TOKEN vacío")
        if session is None:
            import requests

            session = requests.Session()
        self._token = token
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._retry_policy = retry_policy or RetryPolicy()

    # -- internals ---------------------------------------------------------

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        """One HTTP exchange, retried on transient failures only."""
        return with_retries(
            lambda: self._request_once(method, path, json_body),
            policy=self._retry_policy,
            on_retry=lambda attempt, exc, delay: print(
                f"  ↻ Notion {method} {path}: reintento {attempt} en {delay:g}s "
                f"({type(exc).__name__})"
            ),
        )

    def _request_once(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._notion_version,
            "Content-Type": "application/json",
        }
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json_body,
                timeout=self._timeout,
            )
        except Exception as exc:  # network failure, DNS, TLS...
            raise NotionApiError(f"no se pudo contactar con Notion: {exc}") from exc

        status = getattr(response, "status_code", 0)
        if status in (401, 403):
            raise NotionAuthError(
                f"Notion rechazó las credenciales (HTTP {status}); revisa NOTION_TOKEN",
                status_code=status,
            )
        if status == 404:
            raise NotionNotFoundError(
                f"Notion no encuentra el objeto (HTTP 404): {_error_message(response)}",
                status_code=status,
            )
        if status >= 400:
            raise NotionApiError(
                f"Notion devolvió HTTP {status}: {_error_message(response)}",
                status_code=status,
            )

        try:
            return response.json() or {}
        except ValueError as exc:
            raise NotionApiError("Notion devolvió una respuesta que no es JSON") from exc

    # -- public API --------------------------------------------------------

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def query_database(
        self,
        database_id: str,
        *,
        filter: Optional[dict] = None,
        sorts: Optional[list] = None,
        start_cursor: Optional[str] = None,
        page_size: int = 100,
    ) -> dict:
        body: dict = {"page_size": page_size}
        if filter is not None:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request("POST", f"/databases/{database_id}/query", body)

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})


def _error_message(response: Any) -> str:
    """Human readable Notion error, never the request headers or token."""
    try:
        payload = response.json()
    except Exception:
        return "sin detalle"
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return "sin detalle"


__all__ = [
    "MAX_QUERY_PAGES",
    "NOTION_API_BASE",
    "NOTION_VERSION",
    "NotionApiClient",
    "NotionApiError",
    "NotionAuthError",
    "NotionGateway",
    "NotionNotFoundError",
]
