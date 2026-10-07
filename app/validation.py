"""Deterministic validation.

No moderation, no policy engine, no AI review: just the mechanical checks
that keep obviously broken content from reaching a publisher.
"""

from __future__ import annotations

from app.models import ContentItem, ValidationResult
from app.publishers.base import SUPPORTED_PLATFORMS

X_MAX_LENGTH = 280


def validate_item(item: ContentItem) -> ValidationResult:
    """Check the editorial fields of an item before generating anything."""
    errors: list[str] = []
    if not (item.topic or "").strip():
        errors.append("el item no tiene topic")
    if not (item.angle or "").strip():
        errors.append("el item no tiene angle")
    if item.platform not in SUPPORTED_PLATFORMS:
        errors.append(
            f"plataforma no soportada: '{item.platform}' "
            f"(soportadas: {', '.join(sorted(SUPPORTED_PLATFORMS))})"
        )
    if errors:
        return ValidationResult.failure(*errors)
    return ValidationResult.success()


def validate_content(item: ContentItem, content: str) -> ValidationResult:
    """Check the item plus the generated copy destined for its platform."""
    base = validate_item(item)
    errors = list(base.errors)

    if not (content or "").strip():
        errors.append("el contenido generado está vacío")
    elif item.platform in SUPPORTED_PLATFORMS and len(content) > X_MAX_LENGTH:
        errors.append(
            f"el contenido supera el límite de {X_MAX_LENGTH} caracteres "
            f"({len(content)} para '{item.platform}')"
        )

    if errors:
        return ValidationResult.failure(*errors)
    return ValidationResult.success()
