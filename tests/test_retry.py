"""Bounded retry: classification, backoff and per-integration behaviour."""

from types import SimpleNamespace

import pytest

from app.generator import GenerationError, GeminiGenerator
from app.models import ContentItem
from app.publishers import XPublisher
from app.retry import RetryPolicy, is_transient, with_retries
from app.sources.notion_client import (
    NotionApiClient,
    NotionApiError,
    NotionAuthError,
)

from tests.conftest import FakeGeminiClient, FakeTweepyClient
from tests.test_notion_client import FakeResponse, FakeSession

FAST = RetryPolicy(max_attempts=3, base_delay=0)


def make_item(**overrides):
    defaults = dict(id="item-001", topic="Tip", angle="Directo")
    defaults.update(overrides)
    return ContentItem(**defaults)


# -- RetryPolicy ------------------------------------------------------------


def test_default_backoff_is_one_two_four():
    policy = RetryPolicy(max_attempts=4, base_delay=1)

    assert [policy.delay_for(n) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]


def test_backoff_is_capped_at_max_delay():
    policy = RetryPolicy(max_attempts=5, base_delay=10, max_delay=15)

    assert policy.delay_for(1) == 10
    assert policy.delay_for(2) == 15
    assert policy.delay_for(4) == 15


@pytest.mark.parametrize(
    "kwargs",
    [{"max_attempts": 0}, {"base_delay": -1}, {"max_delay": -0.5}],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# -- classification ---------------------------------------------------------


class HttpStatusError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.status_code = code


class ServiceUnavailable(Exception):
    pass


@pytest.mark.parametrize("code", [408, 425, 429, 500, 502, 503, 504])
def test_transient_status_codes_are_retryable(code):
    assert is_transient(HttpStatusError(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_and_auth_errors_are_not_retryable(code):
    assert is_transient(HttpStatusError(code)) is False


def test_network_style_exceptions_are_transient():
    assert is_transient(ConnectionError("se cayó"))
    assert is_transient(TimeoutError("timeout"))


def test_known_transient_exception_names_are_retryable():
    assert is_transient(ServiceUnavailable("no disponible"))


def test_unknown_exceptions_are_permanent_by_default():
    assert is_transient(RuntimeError("algo raro")) is False
    assert is_transient(ValueError("mal dato")) is False


def test_cause_chain_is_inspected():
    wrapped = GenerationError("falló la generación con Gemini: se cayó")
    wrapped.__cause__ = ConnectionError("sin red")

    assert is_transient(wrapped) is True


def test_permanent_status_code_in_the_chain_wins():
    wrapped = GenerationError("falló")
    wrapped.__cause__ = HttpStatusError(401)

    assert is_transient(wrapped) is False


def test_google_style_code_attribute_is_used():
    class APIError(Exception):
        def __init__(self, code):
            super().__init__(f"API {code}")
            self.code = code

    assert is_transient(APIError(503)) is True
    assert is_transient(APIError(400)) is False


def test_notion_auth_error_is_never_transient():
    assert is_transient(NotionAuthError("token malo", status_code=401)) is False
    assert is_transient(NotionApiError("HTTP 500", status_code=500)) is True
    assert is_transient(NotionApiError("sin código")) is False


# -- with_retries -----------------------------------------------------------


def test_retries_a_transient_failure_until_success():
    attempts = []
    sleeps = []
    retries = []

    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("se cayó")
        return "ok"

    result = with_retries(
        operation,
        policy=RetryPolicy(max_attempts=3, base_delay=1),
        sleep=sleeps.append,
        on_retry=lambda attempt, exc, delay: retries.append(attempt),
    )

    assert result == "ok"
    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]
    assert retries == [1, 2]


def test_permanent_failure_is_not_retried():
    attempts = []
    sleeps = []

    def operation():
        attempts.append(1)
        raise RuntimeError("permanente")

    with pytest.raises(RuntimeError):
        with_retries(
            operation,
            policy=RetryPolicy(max_attempts=3, base_delay=1),
            sleep=sleeps.append,
        )

    assert len(attempts) == 1
    assert sleeps == []


def test_transient_failures_are_bounded_by_max_attempts():
    attempts = []
    sleeps = []

    def operation():
        attempts.append(1)
        raise ConnectionError("siempre")

    with pytest.raises(ConnectionError):
        with_retries(
            operation,
            policy=RetryPolicy(max_attempts=3, base_delay=1),
            sleep=sleeps.append,
        )

    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]


def test_custom_predicate_decides_what_retries():
    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("reintentable según predicate")
        return "ok"

    result = with_retries(
        operation,
        policy=RetryPolicy(max_attempts=3, base_delay=0),
        retry_if=lambda exc: isinstance(exc, RuntimeError),
    )

    assert result == "ok"
    assert len(attempts) == 2


def test_no_sleep_when_the_delay_is_zero():
    sleeps = []
    attempts = []

    def operation():
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("x")
        return "ok"

    with_retries(
        operation,
        policy=RetryPolicy(max_attempts=2, base_delay=0),
        sleep=sleeps.append,
    )

    assert sleeps == []


# -- X publisher ------------------------------------------------------------


class FlakyTweepyClient:
    def __init__(self, failures, make_exc, post_id="777"):
        self.failures = failures
        self.make_exc = make_exc
        self.post_id = post_id
        self.calls = []

    def create_tweet(self, text):
        self.calls.append(text)
        if len(self.calls) <= self.failures:
            raise self.make_exc()
        return SimpleNamespace(data={"id": self.post_id})


def test_x_retries_transient_failures_and_then_publishes(capsys):
    fake = FlakyTweepyClient(2, lambda: ConnectionError("se cayó"))
    publisher = XPublisher(dry_run=False, client=fake, retry_policy=FAST)

    result = publisher.publish(make_item(), "hola")

    assert result.status == "published"
    assert result.id == "777"
    assert len(fake.calls) == 3
    assert "reintento" in capsys.readouterr().out


def test_x_does_not_retry_permanent_failures(capsys):
    fake = FlakyTweepyClient(5, lambda: RuntimeError("credenciales mal"))
    publisher = XPublisher(dry_run=False, client=fake, retry_policy=FAST)

    result = publisher.publish(make_item(), "hola")

    assert result.status == "error"
    assert len(fake.calls) == 1
    assert "reintento" not in capsys.readouterr().out


def test_x_retries_are_bounded(capsys):
    fake = FlakyTweepyClient(99, lambda: ConnectionError("seguirá cayendo"))
    publisher = XPublisher(dry_run=False, client=fake, retry_policy=FAST)

    result = publisher.publish(make_item(), "hola")

    assert result.status == "error"
    assert "seguirá cayendo" in result.error
    assert len(fake.calls) == 3
    assert capsys.readouterr().out.count("reintento") == 2


def test_x_dry_run_never_enters_the_retry_path():
    fake = FlakyTweepyClient(0, lambda: ConnectionError("nunca debería llamarse"))
    publisher = XPublisher(dry_run=True, client=fake, retry_policy=FAST)

    result = publisher.publish(make_item(), "hola")

    assert result.status == "simulated"
    assert fake.calls == []


# -- Gemini -----------------------------------------------------------------


class FlakyGeminiClient:
    def __init__(self, failures, make_exc, text="contenido generado"):
        self.failures = failures
        self.make_exc = make_exc
        self.text = text
        self.calls = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents):
        self.calls.append(contents)
        if len(self.calls) <= self.failures:
            raise self.make_exc()
        return SimpleNamespace(text=self.text)


def test_gemini_retries_transient_failures(capsys):
    fake = FlakyGeminiClient(2, lambda: ConnectionError("sin red"))
    generator = GeminiGenerator(
        api_key="k", model="m", client=fake, retry_policy=FAST
    )

    content = generator.generate(make_item())

    assert content == "contenido generado"
    assert len(fake.calls) == 3
    assert "reintento" in capsys.readouterr().out


def test_gemini_does_not_retry_permanent_failures():
    fake = FlakyGeminiClient(9, lambda: RuntimeError("prompt inválido"))
    generator = GeminiGenerator(
        api_key="k", model="m", client=fake, retry_policy=FAST
    )

    with pytest.raises(GenerationError):
        generator.generate(make_item())

    assert len(fake.calls) == 1


def test_gemini_retries_are_bounded():
    fake = FlakyGeminiClient(99, lambda: ConnectionError("siempre caído"))
    generator = GeminiGenerator(
        api_key="k", model="m", client=fake, retry_policy=FAST
    )

    with pytest.raises(GenerationError):
        generator.generate(make_item())

    assert len(fake.calls) == 3


def test_gemini_retry_uses_http_status_codes():
    class RateLimited(Exception):
        def __init__(self):
            super().__init__("429")
            self.code = 429

    fake = FlakyGeminiClient(1, RateLimited)
    generator = GeminiGenerator(
        api_key="k", model="m", client=fake, retry_policy=FAST
    )

    assert generator.generate(make_item()) == "contenido generado"
    assert len(fake.calls) == 2


def test_gemini_empty_response_is_not_retried():
    fake = FakeGeminiClient(text="   ")
    generator = GeminiGenerator(
        api_key="k", model="m", client=fake, retry_policy=FAST
    )

    with pytest.raises(GenerationError):
        generator.generate(make_item())

    assert len(fake.calls) == 1


# -- Notion client ----------------------------------------------------------


def test_notion_retries_transient_http_errors():
    session = FakeSession(
        FakeResponse(503, {"message": "se cayó"}),
        FakeResponse(200, {"results": []}),
    )
    api = NotionApiClient("token", session=session, retry_policy=FAST)

    payload = api.query_database("db-1")

    assert payload == {"results": []}
    assert len(session.calls) == 2


def test_notion_does_not_retry_auth_errors():
    session = FakeSession(FakeResponse(401, {"message": "token mal"}))
    api = NotionApiClient("token", session=session, retry_policy=FAST)

    with pytest.raises(NotionAuthError):
        api.retrieve_database("db-1")

    assert len(session.calls) == 1


def test_notion_retries_are_bounded(capsys):
    session = FakeSession(FakeResponse(500, {"message": "caído"}))
    api = NotionApiClient("token", session=session, retry_policy=FAST)

    with pytest.raises(NotionApiError):
        api.retrieve_database("db-1")

    assert len(session.calls) == 3
    assert capsys.readouterr().out.count("reintento") == 2


def test_notion_not_found_is_not_retried():
    session = FakeSession(FakeResponse(404, {"message": "no existe"}))
    api = NotionApiClient("token", session=session, retry_policy=FAST)

    from app.sources.notion_client import NotionNotFoundError

    with pytest.raises(NotionNotFoundError):
        api.retrieve_page("missing")

    assert len(session.calls) == 1


def test_notion_network_failures_are_retried():
    session = FakeSession(error=ConnectionError("se cayó la red"))
    api = NotionApiClient("token", session=session, retry_policy=FAST)

    with pytest.raises(NotionApiError):
        api.retrieve_database("db-1")

    assert len(session.calls) == 3


def test_zeroth_attempt_has_no_delay():
    assert RetryPolicy(max_attempts=3, base_delay=1).delay_for(0) == 0.0
