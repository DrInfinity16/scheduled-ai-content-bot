import pytest

from app.generator import GenerationError, GeminiGenerator, MAX_OUTPUT_LENGTH
from tests.conftest import FakeGeminiClient


def build(fake, **item_overrides):
    generator = GeminiGenerator(api_key="test-key", model="gemini-test", client=fake)
    item = None
    from app.models import ContentItem

    defaults = dict(id="item-001", topic="Tip de Python", angle="Práctico")
    defaults.update(item_overrides)
    item = ContentItem(**defaults)
    return generator, item


def test_prompt_includes_topic_and_angle(fake_gemini):
    generator, item = build(fake_gemini)

    prompt = generator.build_prompt(item)

    assert "Tip de Python" in prompt
    assert "Práctico" in prompt


def test_generate_calls_model_and_returns_text(fake_gemini):
    fake_gemini.text = "  Hola mundo  "
    generator, item = build(fake_gemini)

    content = generator.generate(item)

    assert content == "Hola mundo"
    assert len(fake_gemini.calls) == 1
    assert fake_gemini.calls[0]["model"] == "gemini-test"
    assert "Tip de Python" in fake_gemini.calls[0]["contents"]


def test_references_are_optional_context(fake_gemini):
    generator, item = build(fake_gemini)

    assert "Referencias" not in generator.build_prompt(item)

    item.references = ["https://example.com/uno", "https://example.com/dos"]
    prompt = generator.build_prompt(item)
    assert "https://example.com/uno" in prompt
    assert "https://example.com/dos" in prompt
    assert "como contexto" in prompt


def test_reference_notes_are_optional_context(fake_gemini):
    generator, item = build(fake_gemini)
    assert "Notas adicionales" not in generator.build_prompt(item)

    item.reference_notes = "Menciona el dato de 2026"
    assert "Menciona el dato de 2026" in generator.build_prompt(item)


def test_long_responses_are_truncated(fake_gemini):
    fake_gemini.text = "x" * 500
    generator, item = build(fake_gemini)

    assert len(generator.generate(item)) == MAX_OUTPUT_LENGTH


def test_empty_response_raises_generation_error(fake_gemini):
    fake_gemini.text = "   "
    generator, item = build(fake_gemini)

    with pytest.raises(GenerationError):
        generator.generate(item)


def test_client_failure_raises_generation_error(fake_gemini):
    fake_gemini.error = RuntimeError("sin red")
    generator, item = build(fake_gemini)

    with pytest.raises(GenerationError) as exc:
        generator.generate(item)
    assert "sin red" in str(exc.value)


def test_fake_client_never_touches_real_api():
    fake = FakeGeminiClient(text="ok")
    generator = GeminiGenerator(api_key="", model="m", client=fake)
    assert generator.client is fake


def test_generator_builds_its_own_client_when_none_is_injected(monkeypatch):
    from google import genai

    sentinel = object()
    monkeypatch.setattr(genai, "Client", lambda api_key: sentinel)

    generator = GeminiGenerator(api_key="k", model="m")

    assert generator.client is sentinel
    assert generator.model == "m"
