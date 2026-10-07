"""Content generation.

Gemini specifics (SDK, model name, response shape) live here and nowhere
else. Everything else in the application only sees a
:class:`ContentGenerator` that turns a :class:`ContentItem` into copy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.models import ContentItem
from app.retry import RetryPolicy, with_retries

MAX_OUTPUT_LENGTH = 280

BASE_PROMPT = (
    "Escribe un tweet (máximo 260 caracteres) sobre: {topic}.\n"
    "Ángulo/tono: {angle}.\n"
    "Sin hashtags excesivos (máximo 1 si aplica), sin comillas, "
    "directo al punto, en español."
)

REFERENCES_PROMPT = (
    "\nReferencias (usa solo como contexto, no abras ni citas enlaces): {references}"
)

REFERENCE_NOTES_PROMPT = "\nNotas adicionales del editor: {notes}"


class GenerationError(Exception):
    """Raised when copy could not be produced."""


class ContentGenerator(ABC):
    @abstractmethod
    def generate(self, item: ContentItem) -> str:
        """Return the copy for ``item`` or raise :class:`GenerationError`."""


class GeminiGenerator(ContentGenerator):
    """Generates copy with Google Gemini.

    ``client`` may be injected so tests never touch the real API.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        client: Optional[object] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self.model = model
        self.client = client
        self._retry_policy = retry_policy or RetryPolicy()

    def build_prompt(self, item: ContentItem) -> str:
        prompt = BASE_PROMPT.format(topic=item.topic, angle=item.angle)
        if item.references:
            prompt += REFERENCES_PROMPT.format(references=" | ".join(item.references))
        if item.reference_notes:
            prompt += REFERENCE_NOTES_PROMPT.format(notes=item.reference_notes)
        return prompt

    def generate(self, item: ContentItem) -> str:
        prompt = self.build_prompt(item)
        try:
            # Transient Gemini failures (429/5xx/timeouts) retry with
            # bounded backoff; permanent ones surface on the first try.
            response = with_retries(
                lambda: self.client.models.generate_content(
                    model=self.model, contents=prompt
                ),
                policy=self._retry_policy,
                on_retry=lambda attempt, exc, delay: print(
                    f"  ↻ Gemini generate_content: reintento {attempt} en "
                    f"{delay:g}s ({type(exc).__name__})"
                ),
            )
        except Exception as exc:  # SDK raises its own hierarchy of errors
            raise GenerationError(f"falló la generación con Gemini: {exc}") from exc

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise GenerationError("Gemini devolvió una respuesta vacía")
        return text[:MAX_OUTPUT_LENGTH]
