import pytest

from app.models import PublishResult
from app.publishers import (
    UnsupportedPlatformError,
    XCredentials,
    XPublisher,
    create_publisher,
)
from app.publishers.base import PublisherConfigurationError
from tests.conftest import FakeTweepyClient

COMPLETE_CREDENTIALS = XCredentials("k", "s", "t", "ts")


def test_x_publisher_calls_tweepy(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy)
    item = make_item()

    result = publisher.publish(item, "hola mundo")

    assert result.status == "published"
    assert result.id == "424242"
    assert fake_tweepy.calls == ["hola mundo"]


def test_dry_run_performs_no_external_call(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=True, client=fake_tweepy)
    item = make_item()

    result = publisher.publish(item, "hola mundo")

    assert result.status == "simulated"
    assert result.id is None
    assert result.dry_run is True
    assert fake_tweepy.calls == []


def test_dry_run_without_client(make_item):
    publisher = XPublisher(dry_run=True)
    result = publisher.publish(make_item(), "hola")
    assert result.status == "simulated"


def test_publishing_failure_is_reported(make_item, fake_tweepy):
    fake_tweepy.error = RuntimeError("rate limit")
    publisher = XPublisher(dry_run=False, client=fake_tweepy)

    result = publisher.publish(make_item(), "hola")

    assert result.status == "error"
    assert "rate limit" in result.error
    assert result.ok is False


def test_empty_content_is_not_published(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy)

    result = publisher.publish(make_item(), "   ")

    assert result.status == "error"
    assert fake_tweepy.calls == []


def test_real_mode_without_credentials_fails_clearly():
    with pytest.raises(PublisherConfigurationError) as exc:
        XPublisher(dry_run=False, credentials=XCredentials(None, None, None, None))
    assert "DRY_RUN=false" in str(exc.value)

    with pytest.raises(PublisherConfigurationError):
        XPublisher(dry_run=False)


def test_complete_credentials_build_real_client():
    publisher = XPublisher(dry_run=False, credentials=COMPLETE_CREDENTIALS)
    assert publisher.platform == "x"
    assert publisher._client is not None


def test_create_publisher_supports_x():
    publisher = create_publisher("x", dry_run=True)
    assert isinstance(publisher, XPublisher)


def test_create_publisher_rejects_unknown_platform():
    with pytest.raises(UnsupportedPlatformError) as exc:
        create_publisher("instagram", dry_run=True)
    assert "instagram" in str(exc.value)
    assert "x" in str(exc.value)


def test_missing_tweepy_is_reported_clearly(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tweepy":
            raise ImportError("No module named 'tweepy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(PublisherConfigurationError) as exc:
        XPublisher(dry_run=False, credentials=COMPLETE_CREDENTIALS)
    assert "tweepy" in str(exc.value)


def test_publish_result_ok_semantics():
    assert PublishResult(status="published").ok
    assert PublishResult(status="simulated").ok
    assert not PublishResult(status="error").ok


def test_published_without_id_is_still_success(make_item):
    class NoDataClient(FakeTweepyClient):
        def create_tweet(self, text):
            self.calls.append(text)
            from types import SimpleNamespace

            return SimpleNamespace(data=None)

    publisher = XPublisher(dry_run=False, client=NoDataClient())
    result = publisher.publish(make_item(), "hola")

    assert result.status == "published"
    assert result.id is None
    assert result.ok is True


# -- Published URL (X_USERNAME) --------------------------------------------


def test_url_is_built_when_the_handle_is_configured(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy, username="ana_dev")

    result = publisher.publish(make_item(), "hola mundo")

    assert result.url == "https://x.com/ana_dev/status/424242"


def test_username_has_the_at_sign_stripped(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy, username="  @ana_dev ")

    result = publisher.publish(make_item(), "hola")

    assert result.url == "https://x.com/ana_dev/status/424242"


def test_no_username_means_no_invented_url(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy)

    result = publisher.publish(make_item(), "hola")

    assert result.url is None
    assert result.id == "424242"


def test_url_is_none_when_the_post_id_is_missing(make_item, fake_tweepy):
    publisher = XPublisher(dry_run=False, client=fake_tweepy, username="ana")

    assert publisher.build_post_url(None) is None
    assert publisher.build_post_url("") is None


def test_dry_run_never_produces_a_url(make_item):
    publisher = XPublisher(dry_run=True, username="ana")

    result = publisher.publish(make_item(), "hola")

    assert result.url is None
    assert result.status == "simulated"


def test_create_publisher_forwards_the_username():
    publisher = create_publisher("x", dry_run=True, username=" @ana ")

    assert isinstance(publisher, XPublisher)
    assert publisher.username == "ana"
