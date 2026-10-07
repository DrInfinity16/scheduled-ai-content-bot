"""XPublisher with optional media: text-only unchanged, text+image new."""

from types import SimpleNamespace

from app.models import ContentItem
from app.publishers import create_publisher
from app.publishers.x import XPublisher
from app.retry import RetryPolicy

from tests.conftest import FakeMediaUploader, FakeTweepyClient, MINIMAL_PNG

FAST = RetryPolicy(max_attempts=3, base_delay=0)


def make_item(**overrides):
    defaults = dict(id="item-001", topic="Tip", angle="Directo")
    defaults.update(overrides)
    return ContentItem(**defaults)


def make_media(tmp_path, **overrides):
    from app.images.artifacts import ImageArtifactStore

    store = ImageArtifactStore(str(tmp_path / "images"))
    image = store.save(
        item_id="item-001",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )
    if overrides:
        from app.images.models import GeneratedImage

        fields = {
            "local_path": image.local_path,
            "mime_type": image.mime_type,
            "size_bytes": image.size_bytes,
            "sha256": image.sha256,
            "provider": image.provider,
        }
        fields.update(overrides)
        return GeneratedImage(**fields)
    return image


def publisher(tweepy=None, uploader=None, **kwargs):
    kwargs.setdefault("retry_policy", FAST)
    return XPublisher(
        dry_run=False,
        client=tweepy or FakeTweepyClient(),
        media_uploader=uploader or FakeMediaUploader(),
        **kwargs,
    )


# -- text-only path is unchanged --------------------------------------------


def test_text_only_publishes_exactly_as_before(tmp_path):
    tweepy = FakeTweepyClient()
    result = publisher(tweepy).publish(make_item(), "hola")

    assert result.status == "published"
    assert result.id == "424242"
    assert result.media_id is None
    assert tweepy.calls == ["hola"]
    assert tweepy.media_ids_calls == [None]


def test_text_only_result_carries_no_media_fields():
    result = publisher().publish(make_item(), "hola")

    assert result.media_id is None


# -- text + image ------------------------------------------------------------


def test_image_is_uploaded_and_attached_to_the_tweet(tmp_path):
    tweepy = FakeTweepyClient(post_id="555")
    uploader = FakeMediaUploader(media_id="888")
    media = make_media(tmp_path)

    result = publisher(tweepy, uploader).publish(make_item(), "hola", media=media)

    assert result.status == "published"
    assert result.id == "555"
    assert result.media_id == "888"
    assert uploader.calls == [media.local_path]
    assert tweepy.calls == ["hola"]
    assert tweepy.media_ids_calls == [["888"]]


def test_media_upload_happens_before_tweet_creation(tmp_path):
    order = []
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader()

    original_upload = uploader.media_upload
    original_tweet = tweepy.create_tweet

    def upload(filename):
        order.append("upload")
        return original_upload(filename)

    def tweet(text, media_ids=None):
        order.append("tweet")
        return original_tweet(text, media_ids=media_ids)

    uploader.media_upload = upload
    tweepy.create_tweet = tweet
    publisher(tweepy, uploader).publish(make_item(), "hola", media=make_media(tmp_path))

    assert order == ["upload", "tweet"]


def test_invalid_artifact_never_reaches_x(tmp_path):
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader()
    media = make_media(tmp_path, mime_type="image/gif")

    result = publisher(tweepy, uploader).publish(make_item(), "hola", media=media)

    assert result.status == "error"
    assert "no se pudo subir la imagen" in result.error
    assert uploader.calls == []
    assert tweepy.calls == []


def test_missing_media_file_never_reaches_x(tmp_path):
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader()
    media = make_media(tmp_path, local_path=str(tmp_path / "borrada.png"))

    result = publisher(tweepy, uploader).publish(make_item(), "hola", media=media)

    assert result.status == "error"
    assert uploader.calls == []
    assert tweepy.calls == []


def test_permanent_upload_failure_creates_no_tweet(tmp_path):
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader(error=RuntimeError("401 malformado"))

    result = publisher(tweepy, uploader).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    assert result.status == "error"
    assert "401 malformado" in result.error
    assert len(uploader.calls) == 1  # permanent: a single attempt
    assert tweepy.calls == []
    assert result.media_id is None


def test_transient_upload_failure_retries_then_succeeds(tmp_path, capsys):
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader(error=ConnectionError("se cayó"), failures=2)

    result = publisher(tweepy, uploader).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    assert result.status == "published"
    assert result.media_id == "999"
    assert len(uploader.calls) == 3
    assert tweepy.media_ids_calls == [["999"]]
    assert "media_upload" in capsys.readouterr().out


def test_missing_media_id_is_an_explicit_error(tmp_path):
    tweepy = FakeTweepyClient()

    class EmptyUploader:
        def __init__(self):
            self.calls = []

        def media_upload(self, filename):
            self.calls.append(filename)
            return SimpleNamespace(media_id_string=None)

    result = publisher(tweepy, EmptyUploader()).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    assert result.status == "error"
    assert "media_id" in result.error
    assert tweepy.calls == []


def test_tweet_failure_after_upload_keeps_the_media_id(tmp_path):
    tweepy = FakeTweepyClient(error=RuntimeError("duplicate content"))
    uploader = FakeMediaUploader(media_id="888")

    result = publisher(tweepy, uploader).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    # Orphaned upload documented: the media id is still reported for audit.
    assert result.status == "error"
    assert result.media_id == "888"
    assert len(uploader.calls) == 1
    assert len(tweepy.calls) == 1  # permanent tweet error: no retry


def test_tweet_creation_retries_reuse_the_uploaded_media(tmp_path):
    tweepy = FakeTweepyClient(error=ConnectionError("se cayó"))
    uploader = FakeMediaUploader(media_id="888")
    calls = {"n": 0}
    original = tweepy.create_tweet

    def flaky(text, media_ids=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return original(text, media_ids=media_ids)
        tweepy.error = None
        return original(text, media_ids=media_ids)

    tweepy.create_tweet = flaky
    result = publisher(tweepy, uploader).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    assert result.status == "published"
    assert len(uploader.calls) == 1  # uploaded once...
    assert tweepy.media_ids_calls == [["888"]] * 3  # ...reused on retry


def test_dry_run_never_touches_media_or_tweets(tmp_path):
    tweepy = FakeTweepyClient()
    uploader = FakeMediaUploader()
    dry = XPublisher(dry_run=True, client=tweepy, media_uploader=uploader)

    result = dry.publish(make_item(), "hola", media=make_media(tmp_path))

    assert result.status == "simulated"
    assert result.media_id is None
    assert result.id is None
    assert uploader.calls == []
    assert tweepy.calls == []


def test_media_uploader_can_be_built_from_credentials():
    from app.publishers import XCredentials

    pub = XPublisher(
        dry_run=False,
        client=FakeTweepyClient(),
        credentials=XCredentials("k", "s", "t", "ts"),
        retry_policy=FAST,
    )

    # No injected uploader and complete credentials: builds lazily.
    # (Construction only; no network is touched here.)
    assert pub._media_uploader is None
    assert pub._credentials.complete is True


def test_media_upload_without_credentials_fails_clearly(tmp_path):
    tweepy = FakeTweepyClient()
    pub = XPublisher(
        dry_run=False, client=tweepy, credentials=None, retry_policy=FAST
    )

    result = pub.publish(make_item(), "hola", media=make_media(tmp_path))

    assert result.status == "error"
    assert "credenciales" in result.error
    assert tweepy.calls == []


def test_create_publisher_forwards_the_media_uploader():
    uploader = FakeMediaUploader()

    pub = create_publisher(
        "x",
        dry_run=False,
        client=FakeTweepyClient(),
        retry_policy=FAST,
        media_uploader=uploader,
    )

    assert isinstance(pub, XPublisher)
    assert pub._media_uploader is uploader


def test_dict_shaped_upload_responses_are_accepted(tmp_path):
    tweepy = FakeTweepyClient(post_id="555")

    class DictUploader:
        def __init__(self):
            self.calls = []

        def media_upload(self, filename):
            self.calls.append(filename)
            return {"media_id_string": "777"}

    result = publisher(tweepy, DictUploader()).publish(
        make_item(), "hola", media=make_media(tmp_path)
    )

    assert result.status == "published"
    assert result.media_id == "777"
    assert tweepy.media_ids_calls == [["777"]]


def test_media_api_builds_a_real_tweepy_client_from_credentials():
    import tweepy
    from app.publishers import XCredentials

    pub = XPublisher(
        dry_run=False,
        client=FakeTweepyClient(),
        credentials=XCredentials("k", "s", "t", "ts"),
        retry_policy=FAST,
    )

    api = pub._media_api()

    assert isinstance(api, tweepy.API)


def test_missing_tweepy_is_reported_clearly(tmp_path, monkeypatch):
    import sys
    from app.publishers import XCredentials

    monkeypatch.setitem(sys.modules, "tweepy", None)
    pub = XPublisher(
        dry_run=False,
        client=FakeTweepyClient(),
        credentials=XCredentials("k", "s", "t", "ts"),
        retry_policy=FAST,
    )

    result = pub.publish(make_item(), "hola", media=make_media(tmp_path))

    assert result.status == "error"
    assert "tweepy no está instalado" in result.error
