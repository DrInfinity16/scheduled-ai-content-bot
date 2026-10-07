"""Domain models shared by every layer of the application.

Nothing in this module knows about YAML, Notion, Gemini, Tweepy or APScheduler.
It only defines the vocabulary of the domain: a piece of scheduled content, its
lifecycle status and the result of trying to publish it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

DEFAULT_PLATFORM = "x"


class ContentStatus(str, Enum):
    """Lifecycle vocabulary for a :class:`ContentItem`.

    Only the vocabulary exists at this stage; the full lifecycle engine is a
    future pass.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    GENERATING = "generating"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"

    @classmethod
    def coerce(cls, value: "ContentStatus | str") -> "ContentStatus":
        if isinstance(value, cls):
            return value
        return cls(str(value))


@dataclass
class ContentItem:
    """A single unit of scheduled editorial content."""

    id: str
    topic: str
    angle: str
    scheduled_at: Optional[datetime] = None
    platform: str = DEFAULT_PLATFORM
    references: list[str] = field(default_factory=list)
    reference_notes: Optional[str] = None
    generate_image: bool = False
    image_brief: Optional[str] = None
    generated_content: Optional[str] = None
    generated_image: Optional[str] = None
    published_url: Optional[str] = None
    error: Optional[str] = None
    status: ContentStatus = ContentStatus.DRAFT

    def __post_init__(self) -> None:
        self.status = ContentStatus.coerce(self.status)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a publishing attempt.

    ``status`` keeps the historical vocabulary used by the JSONL audit log:
    ``published`` | ``simulated`` | ``error``.
    """

    status: str
    platform: str = DEFAULT_PLATFORM
    id: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    url: Optional[str] = None
    media_id: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("published", "simulated")


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic outcome of the validation layer."""

    ok: bool
    errors: tuple[str, ...] = ()

    @classmethod
    def success(cls) -> "ValidationResult":
        return cls(True, ())

    @classmethod
    def failure(cls, *errors: str) -> "ValidationResult":
        return cls(False, tuple(errors))
