"""X/Twitter publisher built on Tweepy.

All Tweepy specifics live here: client construction, credential handling,
``create_tweet`` and the ``DRY_RUN`` simulation path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from app.images.artifacts import validate_artifact
from app.models import ContentItem, PublishResult
from app.publishers.base import (
    PlatformPublisher,
    PublisherConfigurationError,
)
from app.retry import RetryPolicy, with_retries

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.images.models import GeneratedImage

DRY_RUN_STATUS = "simulated"
PUBLISHED_STATUS = "published"
ERROR_STATUS = "error"


@dataclass(frozen=True)
class XCredentials:
    api_key: Optional[str]
    api_secret: Optional[str]
    access_token: Optional[str]
    access_secret: Optional[str]

    @property
    def complete(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_secret])


class XPublisher(PlatformPublisher):
    """Publishes to X, or simulates it when ``dry_run`` is enabled.

    Text-only publishing works exactly as before; pass a validated
    :class:`GeneratedImage` as ``media`` to attach one image. Media is
    uploaded first (v1.1 upload endpoint) and the tweet references the
    returned ``media_id``. If the tweet then fails, the uploaded media is
    left orphaned on X on purpose: no tweet exists, so a retry simply
    uploads again or reuses the id — deleting remote media would add
    complexity for no safety gain.
    """

    platform = "x"

    def __init__(
        self,
        *,
        dry_run: bool = True,
        client: Optional[object] = None,
        credentials: Optional[XCredentials] = None,
        username: Optional[str] = None,
        retry_policy: Optional[RetryPolicy] = None,
        media_uploader: Optional[object] = None,
    ):
        self.dry_run = dry_run
        self.username = (username or "").strip().lstrip("@") or None
        self._client = client
        self._credentials = credentials
        self._media_uploader = media_uploader
        self._retry_policy = retry_policy or RetryPolicy()
        if dry_run or client is not None:
            return

        if credentials is None or not credentials.complete:
            raise PublisherConfigurationError(
                "DRY_RUN=false pero faltan credenciales de X en .env"
            )
        try:
            import tweepy
        except ImportError as exc:
            raise PublisherConfigurationError(
                "tweepy no está instalado. pip install tweepy"
            ) from exc

        self._client = tweepy.Client(
            consumer_key=credentials.api_key,
            consumer_secret=credentials.api_secret,
            access_token=credentials.access_token,
            access_token_secret=credentials.access_secret,
        )

    def publish(
        self,
        item: ContentItem,
        generated_content: str,
        media: Optional["GeneratedImage"] = None,
    ) -> PublishResult:
        if not (generated_content or "").strip():
            return PublishResult(
                status=ERROR_STATUS,
                platform=self.platform,
                error="contenido vacío, no se publicó nada",
                dry_run=self.dry_run,
            )

        if self.dry_run:
            # Authoritative: nothing touches X — no upload, no tweet,
            # no fabricated ids, even when an image was generated.
            return PublishResult(
                status=DRY_RUN_STATUS,
                platform=self.platform,
                dry_run=True,
            )

        media_id: Optional[str] = None
        if media is not None:
            media_id = self._upload_media(media)
            if isinstance(media_id, PublishResult):
                return media_id

        try:
            # Transient X failures (429/5xx/timeouts) retry with bounded
            # backoff here; permanent failures surface immediately.
            # create_tweet retries reuse an already-uploaded media_id.
            if media_id is None:
                result = with_retries(
                    lambda: self._client.create_tweet(text=generated_content),
                    policy=self._retry_policy,
                    on_retry=lambda attempt, exc, delay: print(
                        f"  ↻ X create_tweet: reintento {attempt} en {delay:g}s "
                        f"({type(exc).__name__})"
                    ),
                )
            else:
                result = with_retries(
                    lambda: self._client.create_tweet(
                        text=generated_content, media_ids=[media_id]
                    ),
                    policy=self._retry_policy,
                    on_retry=lambda attempt, exc, delay: print(
                        f"  ↻ X create_tweet: reintento {attempt} en {delay:g}s "
                        f"({type(exc).__name__})"
                    ),
                )
        except Exception as exc:
            return PublishResult(
                status=ERROR_STATUS,
                platform=self.platform,
                error=str(exc),
                dry_run=False,
                media_id=media_id,
            )

        data = getattr(result, "data", None) or {}
        post_id = data.get("id") if isinstance(data, dict) else None
        post_id = str(post_id) if post_id is not None else None
        return PublishResult(
            status=PUBLISHED_STATUS,
            platform=self.platform,
            id=post_id,
            dry_run=False,
            url=self.build_post_url(post_id),
            media_id=media_id,
        )

    def _upload_media(self, media: "GeneratedImage") -> Union[str, PublishResult]:
        """Upload one image; returns the media id or an error result."""
        reason = validate_artifact(media)
        if reason is not None:
            return PublishResult(
                status=ERROR_STATUS,
                platform=self.platform,
                error=f"no se pudo subir la imagen a X: {reason}",
                dry_run=False,
            )
        try:
            uploaded = with_retries(
                lambda: self._media_api().media_upload(media.local_path),
                policy=self._retry_policy,
                on_retry=lambda attempt, exc, delay: print(
                    f"  ↻ X media_upload: reintento {attempt} en {delay:g}s "
                    f"({type(exc).__name__})"
                ),
            )
        except Exception as exc:
            return PublishResult(
                status=ERROR_STATUS,
                platform=self.platform,
                error=f"no se pudo subir la imagen a X: {exc}",
                dry_run=False,
            )
        media_id = getattr(uploaded, "media_id_string", None)
        if media_id is None and isinstance(uploaded, dict):
            media_id = uploaded.get("media_id_string")
        media_id = str(media_id) if media_id is not None else None
        if not media_id:
            return PublishResult(
                status=ERROR_STATUS,
                platform=self.platform,
                error="X no devolvió media_id tras subir la imagen",
                dry_run=False,
            )
        return media_id

    def _media_api(self):
        """v1.1 upload client (the v2 ``Client`` has no media endpoints)."""
        if self._media_uploader is not None:
            return self._media_uploader
        credentials = self._credentials
        if credentials is None or not credentials.complete:
            raise PublisherConfigurationError(
                "DRY_RUN=false con imagen pero faltan credenciales de X en .env"
            )
        try:
            import tweepy
        except ImportError as exc:
            raise PublisherConfigurationError(
                "tweepy no está instalado. pip install tweepy"
            ) from exc
        auth = tweepy.OAuth1UserHandler(
            credentials.api_key,
            credentials.api_secret,
            credentials.access_token,
            credentials.access_secret,
        )
        return tweepy.API(auth)

    def build_post_url(self, post_id: Optional[str]) -> Optional[str]:
        """Public URL for a post, only when the handle is configured.

        No username is ever invented: without ``X_USERNAME`` the URL is left
        empty and the id is kept in the audit log instead.
        """
        if not post_id or not self.username:
            return None
        return f"https://x.com/{self.username}/status/{post_id}"
