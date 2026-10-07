"""Item-level image pipeline: prompt → reuse-or-generate → validate.

Keeps :class:`ContentOrchestrator` readable: it calls one function and
gets back an optional, validated :class:`GeneratedImage` whose metadata
is already persisted in SQLite.
"""

from __future__ import annotations

from typing import Optional

from app.images.artifacts import (
    describe_stored_artifact,
    hash_file,
    validate_artifact,
)
from app.images.base import ImageGenerationError, ImageGenerator
from app.images.models import GeneratedImage
from app.images.prompting import (
    build_visual_prompt,
    compute_visual_fingerprint,
)
from app.models import ContentItem


def prepare_item_image(
    item: ContentItem,
    generated_content: Optional[str],
    *,
    store,
    image_generator: ImageGenerator,
    image_model: Optional[str] = None,
) -> Optional[GeneratedImage]:
    """Return the validated image for ``item``, generating it if needed.

    ``None`` when the item does not request an image. A stored artifact is
    reused only when its fingerprint matches the current editorial input,
    the file still validates and its bytes still hash to the stored value.
    Any generation or validation problem raises
    :class:`ImageGenerationError` — the caller must fail the workflow,
    never silently fall back to text-only.
    """
    if not item.generate_image:
        return None

    visual_prompt = build_visual_prompt(item, generated_content)
    fingerprint = compute_visual_fingerprint(
        item, generated_content, visual_prompt, image_model
    )

    try:
        record = store.get_execution(item.id, item.platform)
    except Exception as exc:
        raise ImageGenerationError(
            "no se pudo leer el estado técnico en SQLite "
            f"antes de generar la imagen: {exc}"
        ) from exc

    reused = _reuse_if_valid(record, fingerprint)
    if reused is not None:
        print(f"  ♻ Se reutiliza la imagen generada ({reused.local_path})")
        return reused

    image = image_generator.generate_image(
        visual_prompt, item, fingerprint=fingerprint, model=image_model
    )
    reason = validate_artifact(image)
    if reason is not None:
        raise ImageGenerationError(
            f"la imagen generada no es publicable: {reason}"
        )
    try:
        store.mark_image_generated(
            item.id,
            item.platform,
            path=image.local_path,
            image_hash=image.sha256,
            visual_fingerprint=fingerprint,
            provider=image.provider,
        )
    except Exception as exc:
        raise ImageGenerationError(
            f"no se pudo registrar la imagen generada en SQLite: {exc}"
        ) from exc
    print(f"  ✓ Imagen generada ({image.local_path})")
    return image


def _reuse_if_valid(record, fingerprint: str) -> Optional[GeneratedImage]:
    """Return the stored artifact when it is still valid, else ``None``."""
    if record is None:
        return None
    if not record.generated_image_path or not record.generated_image_hash:
        return None
    if (record.visual_fingerprint or "") != fingerprint:
        return None
    candidate = describe_stored_artifact(
        record.generated_image_path,
        record.generated_image_hash,
        record.image_provider or "",
    )
    if validate_artifact(candidate) is not None:
        return None
    if hash_file(candidate.local_path) != record.generated_image_hash:
        return None
    return candidate


__all__ = ["prepare_item_image"]
