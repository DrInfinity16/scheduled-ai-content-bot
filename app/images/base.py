"""Image generation boundary.

Text generation and image generation are separate collaborators so future
platforms (Instagram, Facebook, ...) can reuse the same asset. Providers
never touch the network except inside :meth:`ImageGenerator.generate_image`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.images.models import GeneratedImage
from app.models import ContentItem


class ImageGenerationError(Exception):
    """Raised when no image could be produced for the visual prompt."""


class ImageGenerator(ABC):
    """Turns a visual prompt into a stored :class:`GeneratedImage`."""

    provider_name: str = ""

    @abstractmethod
    def generate_image(
        self,
        visual_prompt: str,
        item: ContentItem,
        *,
        fingerprint: str,
        model: Optional[str] = None,
    ) -> GeneratedImage:
        """Generate one image for ``visual_prompt`` and store it.

        ``item`` scopes the deterministic artifact filename and
        ``fingerprint`` names the exact editorial input the image was
        rendered from. Returns the stored artifact or raises
        :class:`ImageGenerationError`.
        """


__all__ = ["ImageGenerationError", "ImageGenerator"]
