"""Image generation with Cloudflare Workers AI.

Uses the Cloudflare Workers AI REST API for the model
``@cf/black-forest-labs/flux-1-schnell``. The response returns a base64-encoded
JPEG image in ``result.image``. Network failures use the shared bounded-retry
infrastructure; anything else surfaces once as :class:`ImageGenerationError`.
"""

from __future__ import annotations

import base64
from typing import Optional

import requests

from app.images.artifacts import ImageArtifactStore
from app.images.base import ImageGenerationError, ImageGenerator
from app.images.models import GeneratedImage
from app.models import ContentItem
from app.retry import RetryPolicy, with_retries


class CloudflareImageProvider(ImageGenerator):
    """Generates images with Cloudflare Workers AI and stores the artifact."""

    provider_name = "cloudflare"

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: Optional[str],
        artifacts: ImageArtifactStore,
        session: Optional[object] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self._account_id = account_id
        self._api_token = api_token
        self._model = (model or "@cf/black-forest-labs/flux-1-schnell").strip()
        self._artifacts = artifacts
        self._session = session or requests.Session()
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
        if not (visual_prompt or "").strip():
            raise ImageGenerationError(
                "no se puede generar una imagen sin prompt visual"
            )
        try:
            image_bytes, mime_type = with_retries(
                lambda: self._render_once(visual_prompt.strip(), chosen),
                policy=self._retry_policy,
                on_retry=lambda attempt, exc, delay: print(
                    "  ↻ Cloudflare generate (imagen): reintento "
                    f"{attempt} en {delay:g}s ({type(exc).__name__})"
                ),
            )
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(
                f"falló la generación de imagen con Cloudflare: {exc}"
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
        """One image render via Cloudflare Workers AI; returns raw bytes plus MIME type."""
        url = f"https://api.cloudflare.com/client/v4/accounts/{self._account_id}/ai/run/{model}"
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
        }
        payload = {"prompt": visual_prompt, "steps": 4}
        response = self._session.post(url, headers=headers, json=payload, timeout=60)
        response_text = getattr(response, "text", str(response))
        if response.status_code >= 400:
            # Let retry logic handle transient codes; raise for permanent ones.
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                raise ConnectionError(f"HTTP {response.status_code}: {response_text}")
            raise ImageGenerationError(
                f"Cloudflare API error {response.status_code}: {response_text}"
            )
        data = response.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            raise ImageGenerationError(
                f"Cloudflare API error: {errors}"
            )
        result = data.get("result", {})
        image_b64 = result.get("image")
        if not image_b64:
            raise ImageGenerationError(
                "Cloudflare no devolvió ninguna imagen para el prompt visual"
            )
        try:
            raw = base64.b64decode(image_b64, validate=True)
        except Exception as exc:
            raise ImageGenerationError(
                f"no se pudo decodificar la imagen base64 de Cloudflare: {exc}"
            ) from exc
        if not raw:
            raise ImageGenerationError(
                "Cloudflare devolvió una imagen vacía"
            )
        return raw, "image/jpeg"


__all__ = ["CloudflareImageProvider"]