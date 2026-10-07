"""Generated image value object.

One image per content item. The file itself lives on disk (see
:mod:`app.images.artifacts`); this object only carries the metadata the
rest of the application needs to reuse, validate and publish it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GeneratedImage:
    """A locally stored image artifact plus its technical facts."""

    local_path: str
    mime_type: str
    size_bytes: int
    sha256: str
    provider: str
    provider_reference: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


__all__ = ["GeneratedImage"]
