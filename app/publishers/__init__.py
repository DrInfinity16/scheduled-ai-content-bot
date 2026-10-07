"""Publisher registry.

The orchestration layer asks for a publisher by platform name and never
imports Tweepy (or any other SDK) itself.
"""

from __future__ import annotations

from typing import Optional

from app.publishers.base import (
    PlatformPublisher,
    PublisherConfigurationError,
    PublisherError,
    SUPPORTED_PLATFORMS,
    UnsupportedPlatformError,
    unsupported_platform,
)
from app.publishers.x import XCredentials, XPublisher
from app.retry import RetryPolicy

__all__ = [
    "PlatformPublisher",
    "PublisherConfigurationError",
    "PublisherError",
    "SUPPORTED_PLATFORMS",
    "UnsupportedPlatformError",
    "XCredentials",
    "XPublisher",
    "create_publisher",
]


def create_publisher(
    platform: str,
    *,
    dry_run: bool = True,
    credentials: Optional[XCredentials] = None,
    client: Optional[object] = None,
    username: Optional[str] = None,
    retry_policy: Optional[RetryPolicy] = None,
    media_uploader: Optional[object] = None,
) -> PlatformPublisher:
    """Build the publisher for ``platform`` or fail clearly."""
    key = (platform or "").strip().lower()
    if key == "x":
        return XPublisher(
            dry_run=dry_run,
            credentials=credentials,
            client=client,
            username=username,
            retry_policy=retry_policy,
            media_uploader=media_uploader,
        )
    raise unsupported_platform(platform)
