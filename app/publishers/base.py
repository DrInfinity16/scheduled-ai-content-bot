"""Publisher abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from app.models import ContentItem, PublishResult

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.images.models import GeneratedImage

SUPPORTED_PLATFORMS = frozenset({"x"})


class PublisherError(Exception):
    """Base error for publishers."""


class PublisherConfigurationError(PublisherError):
    """The publisher cannot be built with the current configuration."""


class UnsupportedPlatformError(PublisherError):
    """No publisher exists for the requested platform."""


class PlatformPublisher(ABC):
    """Turns generated copy into an external post."""

    platform: str = ""

    @abstractmethod
    def publish(
        self,
        item: ContentItem,
        generated_content: str,
        media: Optional["GeneratedImage"] = None,
    ) -> PublishResult:
        """Publish ``generated_content`` for ``item``.

        ``media`` is optional: text-only publishing keeps working exactly
        as before. Expected result statuses: ``published``, ``simulated``
        or ``error``.
        """


def unsupported_platform(platform: str) -> UnsupportedPlatformError:
    supported = ", ".join(sorted(SUPPORTED_PLATFORMS)) or "(ninguna)"
    return UnsupportedPlatformError(
        f"plataforma no soportada: '{platform}' (soportadas: {supported})"
    )


__all__ = [
    "PlatformPublisher",
    "PublisherConfigurationError",
    "PublisherError",
    "SUPPORTED_PLATFORMS",
    "UnsupportedPlatformError",
    "unsupported_platform",
]
