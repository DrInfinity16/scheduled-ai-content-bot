"""Local storage for generated image artifacts.

Layout: ``<root>/<safe-item-id>_<fingerprint12>.<ext>``. Binary blobs
never go to SQLite; only path/hash metadata does. Filenames carry the
item id and a hash fragment — never secrets, prompts or credentials.
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path
from typing import Optional

from app.images.models import GeneratedImage

SUPPORTED_MIME_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # X image limit: 5 MB
DEFAULT_ARTIFACTS_DIR = str(Path("artifacts") / "images")

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

_EXTENSION_MIME = {extension: mime for mime, extension in SUPPORTED_MIME_TYPES.items()}
_EXTENSION_MIME.update({"jpg": "image/jpeg", "jpeg": "image/jpeg"})


def hash_file(local_path: str) -> Optional[str]:
    """SHA-256 of a file's bytes, or ``None`` when unreadable."""
    try:
        return sha256(Path(local_path).read_bytes()).hexdigest()
    except OSError:
        return None


def describe_stored_artifact(
    local_path: str, image_hash: str, provider: str
) -> GeneratedImage:
    """Rebuild the value object for a file already on disk.

    Used when SQLite knows an artifact: the caller still validates it
    (existence, size, hash) before trusting it.
    """
    path = Path(local_path)
    mime = _EXTENSION_MIME.get(path.suffix.lower().lstrip("."), "")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return GeneratedImage(
        local_path=local_path,
        mime_type=mime,
        size_bytes=size,
        sha256=image_hash,
        provider=provider,
    )


def safe_filename(item_id: str, fingerprint: str, extension: str) -> str:
    """Deterministic, filesystem-safe artifact name (no secrets inside)."""
    stem = _SAFE_ID.sub("-", item_id).strip("-") or "item"
    return f"{stem}_{fingerprint[:12]}.{extension}"


def validate_artifact(image: GeneratedImage) -> Optional[str]:
    """Return ``None`` when the artifact is publishable, else the reason."""
    if image.mime_type not in SUPPORTED_MIME_TYPES:
        return f"tipo MIME no soportado para X: {image.mime_type}"
    path = Path(image.local_path)
    if not path.is_file():
        return f"el archivo de imagen no existe: {image.local_path}"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"no se pudo leer el archivo de imagen: {exc}"
    if size <= 0:
        return "el archivo de imagen está vacío"
    if size > MAX_IMAGE_BYTES:
        return (
            f"la imagen supera el máximo de X "
            f"({size} > {MAX_IMAGE_BYTES} bytes)"
        )
    if image.size_bytes != size:
        return "el tamaño registrado no coincide con el archivo"
    return None


class ImageArtifactStore:
    """Persists and validates generated image files under one root."""

    def __init__(self, root_dir: str = DEFAULT_ARTIFACTS_DIR):
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        item_id: str,
        fingerprint: str,
        image_bytes: bytes,
        mime_type: str,
        provider: str,
        provider_reference: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> GeneratedImage:
        """Write ``image_bytes`` to a deterministic path and describe it."""
        if not image_bytes:
            raise ValueError("no se puede guardar una imagen vacía")
        extension = SUPPORTED_MIME_TYPES.get(mime_type)
        if extension is None:
            raise ValueError(
                f"tipo MIME no soportado para X: {mime_type}"
            )
        path = self.root / safe_filename(item_id, fingerprint, extension)
        path.write_bytes(image_bytes)
        return GeneratedImage(
            local_path=str(path),
            mime_type=mime_type,
            size_bytes=len(image_bytes),
            sha256=sha256(image_bytes).hexdigest(),
            provider=provider,
            provider_reference=provider_reference,
            width=width,
            height=height,
        )

    def validate(self, image: GeneratedImage) -> Optional[str]:
        """Validate one artifact; ``None`` means publishable."""
        return validate_artifact(image)


__all__ = [
    "DEFAULT_ARTIFACTS_DIR",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_MIME_TYPES",
    "ImageArtifactStore",
    "describe_stored_artifact",
    "hash_file",
    "safe_filename",
    "validate_artifact",
]
