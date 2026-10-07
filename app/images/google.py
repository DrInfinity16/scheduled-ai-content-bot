"""Image generation with the Google Gemini API.

Uses ``client.models.generate_content`` with an image-capable model (the
SDK marks ``generate_images`` as deprecated), honouring the configured
``IMAGE_MODEL``. No model name is invented here: when no model is
configured the provider fails fast with a clear message instead of
guessing one.

Network failures use the shared bounded-retry infrastructure; anything
else surfaces once as :class:`ImageGenerationError`.
"""

from __future__ import annotations

import base64
from typing import Optional

from app.images.artifacts import ImageArtifactStore
from app.images.base import ImageGenerationError, ImageGenerator
from app.images.models import GeneratedImage
from app.models import ContentItem
from app.retry import RetryPolicy, with_retries


class GoogleImageProvider(ImageGenerator):
    """Generates images with Gemini image models and stores the artifact."""

    provider_name = "google"

    def __init__(
        self,
        api_key: str,
        model: Optional[str],
        artifacts: ImageArtifactStore,
        client: Optional[object] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client
        self._model = (model or "").strip() or None
        self._artifacts = artifacts
        self._retry_policy = retry_policy or RetryPolicy()

    def generate_image(
        self,
        visual_prompt: str,
        item: ContentItem,
        *,
        fingerprint: str,
        model: Optional[str] = None,
    ) -> GeneratedImage:
        chosen = (model or "").strip() or self._model
        if not chosen:
            raise ImageGenerationError(
                "no hay modelo de imagen configurado "
                "(define IMAGE_MODEL en .env)"
            )
        if not (visual_prompt or "").strip():
            raise ImageGenerationError(
                "no se puede generar una imagen sin prompt visual"
            )
        try:
            image_bytes, mime_type = with_retries(
                lambda: self._render_once(visual_prompt.strip(), chosen),
                policy=self._retry_policy,
                on_retry=lambda attempt, exc, delay: print(
                    "  ↻ Gemini generate_content (imagen): reintento "
                    f"{attempt} en {delay:g}s ({type(exc).__name__})"
                ),
            )
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(
                f"falló la generación de imagen con Gemini: {exc}"
            ) from exc
        return self._artifacts.save(
            item_id=item.id,
            fingerprint=fingerprint,
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider=self.provider_name,
            provider_reference=chosen,
        )

    # -- internals ---------------------------------------------------------

    def _render_once(self, visual_prompt: str, model: str) -> tuple[bytes, str]:
        """One image render; returns raw bytes plus MIME type."""
        response = self._client.models.generate_content(
            model=model, contents=visual_prompt
        )
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline is None:
                    continue
                data = getattr(inline, "data", None)
                if not data:
                    continue
                raw = (
                    base64.b64decode(data)
                    if isinstance(data, str)
                    else bytes(data)
                )
                if raw:
                    mime = getattr(inline, "mime_type", None) or "image/png"
                    return raw, mime
        raise ImageGenerationError(
            "Gemini no devolvió ninguna imagen para el prompt visual"
        )


__all__ = ["GoogleImageProvider"]
