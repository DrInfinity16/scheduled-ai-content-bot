"""Visual-prompt strategy and image-input fingerprint.

The visual prompt describes an *image*, never a social-media caption.
Image models render poor typography, so generated visuals avoid embedded
text unless the editorial Image Brief explicitly asks for it.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Optional

from app.models import ContentItem

# Words suggesting the editor explicitly wants typography in the image.
_TYPOGRAPHY_MARKERS = (
    "texto",
    "tipograf",
    "letra",
    "letras",
    "título",
    "titulo",
    "palabra",
    "frase",
    "cita",
    "typography",
    "text",
)

_NO_TEXT_GUIDANCE = (
    " Sin texto incrustado, sin letras, sin logotipos y sin marcas de agua."
)


def _wants_typography(brief: str) -> bool:
    lowered = brief.lower()
    return any(marker in lowered for marker in _TYPOGRAPHY_MARKERS)


def build_visual_prompt(
    item: ContentItem, generated_content: Optional[str]
) -> str:
    """Resolve the visual intent for ``item``.

    Case A (Image Brief present): the brief is the primary direction,
    lightly normalized, never overwritten. Case B (no brief): a prompt is
    derived from topic, angle, copy and reference notes. References (URLs)
    are never fetched; they stay out of the visual prompt entirely.
    """
    brief = (item.image_brief or "").strip()
    if brief:
        if _wants_typography(brief):
            return brief
        return brief + _NO_TEXT_GUIDANCE

    parts = [
        "Ilustración limpia y simple para acompañar una publicación "
        "en redes sociales.",
        f"Tema: {item.topic}.",
        f"Tono: {item.angle}.",
    ]
    copy = (generated_content or "").strip()
    if copy:
        parts.append(f"Contexto del texto: {copy[:200]}")
    notes = (item.reference_notes or "").strip()
    if notes:
        parts.append(f"Notas del editor: {notes[:200]}")
    parts.append(
        "Estilo plano, alto contraste, composición centrada."
        + _NO_TEXT_GUIDANCE
    )
    return " ".join(parts)


def compute_visual_fingerprint(
    item: ContentItem,
    generated_content: Optional[str],
    visual_prompt: str,
    model: Optional[str],
) -> str:
    """Stable fingerprint of the inputs that determine the image.

    Used to decide whether a stored artifact is still valid for the
    current editorial input: same fingerprint + valid file ⇒ reuse.
    """
    payload = "\x1e".join(
        [
            item.id,
            item.platform,
            (item.image_brief or "").strip(),
            item.topic,
            item.angle,
            (generated_content or "").strip(),
            (item.reference_notes or "").strip(),
            visual_prompt.strip(),
            model or "",
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["build_visual_prompt", "compute_visual_fingerprint"]
