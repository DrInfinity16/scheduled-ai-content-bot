"""Image layer: prompts, fingerprints, artifacts, service, provider."""

from types import SimpleNamespace

import pytest

from app.images.artifacts import (
    ImageArtifactStore,
    describe_stored_artifact,
    hash_file,
    safe_filename,
    validate_artifact,
)
from app.images.base import ImageGenerationError
from app.images.cloudflare import CloudflareImageProvider
from app.images.google import GoogleImageProvider
from app.images.models import GeneratedImage
from app.images.prompting import (
    build_visual_prompt,
    compute_visual_fingerprint,
)
from app.images.service import prepare_item_image
from app.models import ContentItem
from app.retry import RetryPolicy
from app.storage import ExecutionStore

from tests.conftest import FakeImageGenerator, MINIMAL_PNG

FAST = RetryPolicy(max_attempts=3, base_delay=0)


def make_item(**overrides):
    defaults = dict(id="item-001", topic="Tip de Python", angle="Directo")
    defaults.update(overrides)
    return ContentItem(**defaults)


def make_image(path="img.png", **overrides):
    defaults = dict(
        local_path=path,
        mime_type="image/png",
        size_bytes=10,
        sha256="abc",
        provider="fake",
    )
    defaults.update(overrides)
    return GeneratedImage(**defaults)


# -- visual prompt ----------------------------------------------------------


def test_brief_is_the_primary_direction():
    item = make_item(
        generate_image=True,
        image_brief="Un zorro geométrico al atardecer, estilo plano",
    )

    prompt = build_visual_prompt(item, "texto del tweet")

    assert prompt.startswith("Un zorro geométrico al atardecer")
    assert "Sin texto incrustado" in prompt


def test_brief_requesting_typography_is_not_overwritten():
    item = make_item(
        generate_image=True,
        image_brief="Cartel con el texto HOLA en tipografía grande",
    )

    prompt = build_visual_prompt(item, "texto")

    assert prompt == "Cartel con el texto HOLA en tipografía grande"


def test_derived_prompt_uses_topic_angle_copy_and_notes():
    item = make_item(
        generate_image=True,
        image_brief=None,
        reference_notes="colores cálidos",
        references=["https://example.com/nota"],
    )

    prompt = build_visual_prompt(item, "copia generada del tweet")

    assert "Tip de Python" in prompt
    assert "Directo" in prompt
    assert "copia generada" in prompt
    assert "colores cálidos" in prompt
    assert "example.com" not in prompt  # references never fetched/inlined
    assert "Sin texto incrustado" in prompt


def test_derived_prompt_survives_empty_inputs():
    prompt = build_visual_prompt(make_item(topic="T", angle="A"), None)

    assert isinstance(prompt, str) and prompt.strip()


# -- fingerprint ------------------------------------------------------------


def test_fingerprint_is_stable_and_sensitive():
    item = make_item(generate_image=True, image_brief="zorro")
    base = compute_visual_fingerprint(item, "copy", "prompt", "model")

    assert base == compute_visual_fingerprint(item, "copy", "prompt", "model")
    assert len(base) == 64
    assert base != compute_visual_fingerprint(item, "copy!", "prompt", "model")
    assert base != compute_visual_fingerprint(
        make_item(generate_image=True, image_brief="otro"), "copy", "prompt", "model"
    )
    assert base != compute_visual_fingerprint(item, "copy", "prompt", "otro-modelo")


# -- artifacts --------------------------------------------------------------


def test_save_creates_a_deterministic_safe_file(tmp_path):
    store = ImageArtifactStore(str(tmp_path / "images"))

    image = store.save(
        item_id="page-1",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )

    assert image.local_path.endswith("page-1_ffffffffffff.png")
    assert image.size_bytes == len(MINIMAL_PNG)
    assert len(image.sha256) == 64
    assert image.mime_type == "image/png"
    assert validate_artifact(image) is None


def test_filenames_never_leak_unsafe_characters(tmp_path):
    store = ImageArtifactStore(str(tmp_path))

    name = safe_filename("../../etc/passwd", "f" * 64, "png")

    assert ".." not in name and "/" not in name
    assert name.endswith(".png")
    image = store.save(
        item_id="page'; DROP TABLE x;--",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )
    assert "../" not in image.local_path and ";" not in image.local_path.split("/")[-1]


def test_jpeg_maps_to_jpg_extension(tmp_path):
    store = ImageArtifactStore(str(tmp_path))

    image = store.save(
        item_id="p",
        fingerprint="f" * 64,
        image_bytes=b"\xff\xd8fakejpeg",
        mime_type="image/jpeg",
        provider="fake",
    )

    assert image.local_path.endswith(".jpg")


def test_save_rejects_empty_bytes_and_bad_mime(tmp_path):
    store = ImageArtifactStore(str(tmp_path))

    with pytest.raises(ValueError):
        store.save(
            item_id="p",
            fingerprint="f" * 64,
            image_bytes=b"",
            mime_type="image/png",
            provider="fake",
        )
    with pytest.raises(ValueError):
        store.save(
            item_id="p",
            fingerprint="f" * 64,
            image_bytes=b"GIF89a...",
            mime_type="image/gif",
            provider="fake",
        )


def test_validate_rejects_missing_empty_and_oversize(tmp_path, monkeypatch):
    import app.images.artifacts as artifacts_module

    store = ImageArtifactStore(str(tmp_path))

    missing = make_image(path=str(tmp_path / "no-existe.png"), size_bytes=0)
    assert "no existe" in (store.validate(missing) or "")

    empty_path = tmp_path / "vacia.png"
    empty_path.write_bytes(b"")
    empty = make_image(path=str(empty_path), size_bytes=0)
    assert "vacío" in (store.validate(empty) or "")

    bad_mime = store.save(
        item_id="p",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )
    tampered = GeneratedImage(
        local_path=bad_mime.local_path,
        mime_type="image/gif",
        size_bytes=bad_mime.size_bytes,
        sha256=bad_mime.sha256,
        provider="fake",
    )
    assert "no soportado" in (store.validate(tampered) or "")

    wrong_size = GeneratedImage(
        local_path=bad_mime.local_path,
        mime_type="image/png",
        size_bytes=bad_mime.size_bytes + 1,
        sha256=bad_mime.sha256,
        provider="fake",
    )
    assert "no coincide" in (store.validate(wrong_size) or "")

    monkeypatch.setattr(artifacts_module, "MAX_IMAGE_BYTES", 10)
    assert "supera el máximo" in (store.validate(bad_mime) or "")


def test_hash_file_and_describe_roundtrip(tmp_path):
    store = ImageArtifactStore(str(tmp_path))
    image = store.save(
        item_id="p",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )

    assert hash_file(image.local_path) == image.sha256
    assert hash_file(str(tmp_path / "ausente.png")) is None

    described = describe_stored_artifact(
        image.local_path, image.sha256, "fake"
    )
    assert described.mime_type == "image/png"
    assert described.size_bytes == image.size_bytes
    assert validate_artifact(described) is None


# -- service ----------------------------------------------------------------


def build_service_world(tmp_path, **item_overrides):
    flag = item_overrides.pop("generate_image", True)
    item = make_item(generate_image=flag, **item_overrides)
    store = ExecutionStore(str(tmp_path / "content_bot.db"))
    artifacts = ImageArtifactStore(str(tmp_path / "images"))
    generator = FakeImageGenerator(artifacts=artifacts)
    return item, store, artifacts, generator


def test_no_image_flag_means_no_generation(tmp_path):
    item, store, _, generator = build_service_world(
        tmp_path, generate_image=False
    )

    result = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )

    assert result is None
    assert generator.calls == []


def test_first_run_generates_and_persists_metadata(tmp_path):
    item, store, _, generator = build_service_world(tmp_path)

    image = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )

    assert len(generator.calls) == 1
    assert "Sin texto incrustado" in generator.calls[0]["prompt"]
    record = store.get_execution("item-001", "x")
    assert record.generated_image_path == image.local_path
    assert record.generated_image_hash == image.sha256
    assert record.image_provider == "fake"
    assert len(record.visual_fingerprint or "") == 64


def test_valid_stored_artifact_is_reused(tmp_path):
    item, store, _, generator = build_service_world(tmp_path)
    first = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )

    second = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )

    assert len(generator.calls) == 1
    assert second.local_path == first.local_path
    assert second.sha256 == first.sha256


def test_changed_brief_regenerates(tmp_path):
    item, store, _, generator = build_service_world(
        tmp_path, image_brief="zorro"
    )
    prepare_item_image(item, "copy", store=store, image_generator=generator)

    item.image_brief = "robot"
    prepare_item_image(item, "copy", store=store, image_generator=generator)

    assert len(generator.calls) == 2


def test_missing_file_regenerates(tmp_path):
    import os

    item, store, _, generator = build_service_world(tmp_path)
    first = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )
    os.remove(first.local_path)

    prepare_item_image(item, "copy", store=store, image_generator=generator)

    assert len(generator.calls) == 2


def test_tampered_file_regenerates(tmp_path):
    from pathlib import Path

    item, store, _, generator = build_service_world(tmp_path)
    first = prepare_item_image(
        item, "copy", store=store, image_generator=generator
    )
    same_size = bytearray(MINIMAL_PNG)
    same_size[10] ^= 1  # same length, different bytes: hash check must fail
    Path(first.local_path).write_bytes(bytes(same_size))

    prepare_item_image(item, "copy", store=store, image_generator=generator)

    assert len(generator.calls) == 2


def test_generation_error_is_explicit_and_stored(tmp_path):
    from app.images.base import ImageGenerationError as IGE

    item, store, artifacts, _ = build_service_world(tmp_path)
    broken = FakeImageGenerator(
        artifacts=artifacts, error=IGE("proveedor caído")
    )

    with pytest.raises(IGE) as exc:
        prepare_item_image(
            item, "copy", store=store, image_generator=broken
        )

    assert "proveedor caído" in str(exc.value)
    # Nothing half-written: no image metadata was persisted.
    assert store.get_execution("item-001", "x") is None


def test_store_read_failure_is_an_explicit_error(tmp_path):
    from app.images.base import ImageGenerationError as IGE

    item, _, artifacts, generator = build_service_world(tmp_path)

    class BrokenStore:
        def get_execution(self, *args):
            raise RuntimeError("sqlite bloqueado")

    with pytest.raises(IGE) as exc:
        prepare_item_image(
            item, "copy", store=BrokenStore(), image_generator=generator
        )

    assert "no se pudo leer" in str(exc.value)
    assert generator.calls == []


def test_store_write_failure_is_an_explicit_error(tmp_path):
    from app.images.base import ImageGenerationError as IGE

    item, store, artifacts, generator = build_service_world(tmp_path)

    class WriteBrokenStore(ExecutionStore):
        def mark_image_generated(self, *args, **kwargs):
            raise RuntimeError("disco lleno")

    broken = WriteBrokenStore(str(tmp_path / "content_bot.db"))
    with pytest.raises(IGE) as exc:
        prepare_item_image(
            item, "copy", store=broken, image_generator=generator
        )

    assert "no se pudo registrar" in str(exc.value)


# -- Google provider (fake transport, real parsing) -------------------------


class FakeGenaiModels:
    def __init__(self, parts=None, error=None):
        self.parts = parts
        self.error = error
        self.calls: list = []

    def generate_content(self, model, contents):
        self.calls.append({"model": model, "contents": contents})
        if self.error is not None:
            raise self.error
        content = SimpleNamespace(parts=self.parts or [])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def image_part(data=MINIMAL_PNG, mime_type="image/png"):
    return SimpleNamespace(
        inline_data=SimpleNamespace(data=data, mime_type=mime_type)
    )


def google_provider(tmp_path, models):
    client = SimpleNamespace(models=models)
    return GoogleImageProvider(
        api_key="fake",
        model="modelo-falso",
        artifacts=ImageArtifactStore(str(tmp_path / "images")),
        client=client,
        retry_policy=FAST,
    )


def test_google_provider_returns_a_stored_artifact(tmp_path):
    models = FakeGenaiModels(parts=[image_part()])
    provider = google_provider(tmp_path, models)
    item = make_item(generate_image=True)

    image = provider.generate_image("un zorro", item, fingerprint="f" * 64)

    assert provider.provider_name == "google"
    assert image.local_path.endswith("item-001_ffffffffffff.png")
    assert image.mime_type == "image/png"
    assert image.provider == "google"
    assert image.provider_reference == "modelo-falso"
    assert models.calls[0]["model"] == "modelo-falso"
    assert models.calls[0]["contents"] == "un zorro"


def test_google_provider_accepts_base64_text_parts(tmp_path):
    import base64

    models = FakeGenaiModels(
        parts=[image_part(base64.b64encode(MINIMAL_PNG).decode("ascii"))]
    )
    provider = google_provider(tmp_path, models)

    image = provider.generate_image(
        "un zorro", make_item(), fingerprint="f" * 64
    )

    assert image.size_bytes == len(MINIMAL_PNG)


def test_google_provider_requires_a_model(tmp_path):
    models = FakeGenaiModels(parts=[image_part()])
    client = SimpleNamespace(models=models)
    provider = GoogleImageProvider(
        api_key="fake",
        model=None,
        artifacts=ImageArtifactStore(str(tmp_path)),
        client=client,
        retry_policy=FAST,
    )

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("x", make_item(), fingerprint="f" * 64)

    assert "IMAGE_MODEL" in str(exc.value)
    assert models.calls == []


def test_google_provider_rejects_empty_prompt(tmp_path):
    models = FakeGenaiModels(parts=[image_part()])
    provider = google_provider(tmp_path, models)

    with pytest.raises(ImageGenerationError):
        provider.generate_image("   ", make_item(), fingerprint="f" * 64)

    assert models.calls == []


def test_google_provider_retries_transient_failures(tmp_path):
    models = FakeGenaiModels(
        parts=[image_part()], error=ConnectionError("se cayó")
    )
    # Fail twice, then succeed: swap the error off after two calls.
    original = models.generate_content

    def flaky(model, contents):
        if len(models.calls) < 2:
            return original(model, contents)
        models.error = None
        return original(model, contents)

    models.generate_content = flaky
    provider = google_provider(tmp_path, models)

    image = provider.generate_image(
        "un zorro", make_item(), fingerprint="f" * 64
    )

    assert image.size_bytes == len(MINIMAL_PNG)
    assert len(models.calls) == 3


def test_google_provider_wraps_permanent_failures(tmp_path):
    models = FakeGenaiModels(error=RuntimeError("401 malformado"))
    provider = google_provider(tmp_path, models)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "401 malformado" in str(exc.value)
    assert len(models.calls) == 1


def test_google_provider_rejects_empty_responses(tmp_path):
    models = FakeGenaiModels(parts=[SimpleNamespace(text="solo texto")])
    provider = google_provider(tmp_path, models)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "no devolvió ninguna imagen" in str(exc.value)


def test_image_generation_error_is_never_transient():
    from app.retry import is_transient

    assert is_transient(ImageGenerationError("proveedor caído")) is False


def test_hash_file_returns_none_for_unreadable_paths(tmp_path):
    assert hash_file(str(tmp_path)) is None  # a directory, not a file


def test_google_provider_skips_parts_without_image_data(tmp_path):
    empty = SimpleNamespace(inline_data=SimpleNamespace(data=None, mime_type=None))
    models = FakeGenaiModels(parts=[empty, image_part()])
    provider = google_provider(tmp_path, models)

    image = provider.generate_image(
        "un zorro", make_item(), fingerprint="f" * 64
    )

    assert image.size_bytes == len(MINIMAL_PNG)


def test_service_rejects_an_invalid_provider_artifact(tmp_path):
    from app.images.base import ImageGenerationError as IGE
    from app.images.models import GeneratedImage as GI

    item, store, artifacts, _ = build_service_world(tmp_path)

    class SloppyGenerator:
        provider_name = "sloppy"

        def __init__(self):
            self.calls = []

        def generate_image(self, prompt, item, *, fingerprint, model=None):
            self.calls.append(prompt)
            return GI(
                local_path=str(tmp_path / "inexistente.png"),
                mime_type="image/png",
                size_bytes=10,
                sha256="x" * 64,
                provider="sloppy",
            )

    with pytest.raises(IGE) as exc:
        prepare_item_image(
            item, "copy", store=store, image_generator=SloppyGenerator()
        )

    assert "no es publicable" in str(exc.value)
    assert store.get_execution("item-001", "x") is None


def test_validate_reports_unreadable_files(tmp_path, monkeypatch):
    from pathlib import Path

    from app.images.models import GeneratedImage as GI

    real = ImageArtifactStore(str(tmp_path)).save(
        item_id="p",
        fingerprint="f" * 64,
        image_bytes=MINIMAL_PNG,
        mime_type="image/png",
        provider="fake",
    )

    def boom(self):
        raise OSError("disco ilegible")

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "stat", boom)

    reason = validate_artifact(
        GI(
            local_path=real.local_path,
            mime_type="image/png",
            size_bytes=real.size_bytes,
            sha256=real.sha256,
            provider="fake",
        )
    )
    assert reason is not None and "no se pudo leer" in reason


# -- Cloudflare provider (fake transport, real parsing) ----------------------


class FakeCloudflareResponse:
    def __init__(self, image_b64=None, status_code=200, success=True, error=None, text=None):
        self._image_b64 = image_b64
        self.status_code = status_code
        self._success = success
        self._error = error
        self._text = text

    @property
    def text(self):
        if self._text is not None:
            return self._text
        if self._error:
            return f'{{"success": false, "errors": [{{"message": "{self._error}"}}]}}'
        return f'{{"success": {str(self._success).lower()}, "result": {{"image": "{self._image_b64}"}}}}'

    def json(self):
        if self._error:
            return {"success": False, "errors": [{"message": self._error}]}
        return {
            "success": self._success,
            "result": {"image": self._image_b64} if self._image_b64 else {},
        }


class FakeCloudflareSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeCloudflareResponse(image_b64="")
        self.error = error
        self.calls: list = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


import base64


def cloudflare_provider(tmp_path, session):
    return CloudflareImageProvider(
        account_id="test-account",
        api_token="test-token",
        model="@cf/black-forest-labs/flux-1-schnell",
        artifacts=ImageArtifactStore(str(tmp_path / "images")),
        session=session,
        retry_policy=FAST,
    )


def test_cloudflare_provider_returns_a_stored_artifact(tmp_path):
    image_b64 = base64.b64encode(MINIMAL_PNG).decode("ascii")
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64=image_b64))
    provider = cloudflare_provider(tmp_path, session)
    item = make_item(generate_image=True)

    image = provider.generate_image("un zorro", item, fingerprint="f" * 64)

    assert provider.provider_name == "cloudflare"
    assert image.local_path.endswith("item-001_ffffffffffff.jpg")
    assert image.mime_type == "image/jpeg"
    assert image.provider == "cloudflare"
    assert image.provider_reference == "@cf/black-forest-labs/flux-1-schnell"
    assert len(session.calls) == 1
    assert session.calls[0]["json"]["prompt"] == "un zorro"
    assert session.calls[0]["json"]["steps"] == 4
    assert "Authorization" in session.calls[0]["headers"]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-token"


def test_cloudflare_provider_uses_custom_model(tmp_path):
    image_b64 = base64.b64encode(MINIMAL_PNG).decode("ascii")
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64=image_b64))
    provider = CloudflareImageProvider(
        account_id="test-account",
        api_token="test-token",
        model="@cf/custom/model",
        artifacts=ImageArtifactStore(str(tmp_path / "images")),
        session=session,
        retry_policy=FAST,
    )
    item = make_item(generate_image=True)

    image = provider.generate_image("un zorro", item, fingerprint="f" * 64)

    assert image.provider_reference == "@cf/custom/model"
    assert "@cf/custom/model" in session.calls[0]["url"]


def test_cloudflare_provider_rejects_empty_prompt(tmp_path):
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64=""))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError):
        provider.generate_image("   ", make_item(), fingerprint="f" * 64)

    assert session.calls == []


def test_cloudflare_provider_rejects_empty_response(tmp_path):
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64=None))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "no devolvió ninguna imagen" in str(exc.value)


def test_cloudflare_provider_rejects_malformed_base64(tmp_path):
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64="no-base64!"))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "no se pudo decodificar" in str(exc.value)


def test_cloudflare_provider_rejects_empty_image_bytes(tmp_path):
    session = FakeCloudflareSession(response=FakeCloudflareResponse(image_b64=""))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "no devolvió ninguna imagen" in str(exc.value)


def test_cloudflare_provider_retries_transient_failures(tmp_path):
    call_count = {"count": 0}

    def make_session():
        call_count["count"] += 1
        if call_count["count"] < 3:
            return FakeCloudflareResponse(status_code=503, success=False)
        image_b64 = base64.b64encode(MINIMAL_PNG).decode("ascii")
        return FakeCloudflareResponse(image_b64=image_b64)

    class FlakySession:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            resp = make_session()
            if resp.status_code >= 400:
                raise ConnectionError(f"HTTP {resp.status_code}")
            return resp

    session = FlakySession()
    provider = cloudflare_provider(tmp_path, session)
    item = make_item(generate_image=True)

    image = provider.generate_image("un zorro", item, fingerprint="f" * 64)

    assert image.size_bytes == len(MINIMAL_PNG)
    assert len(session.calls) == 3


def test_cloudflare_provider_wraps_permanent_failures(tmp_path):
    session = FakeCloudflareSession(
        response=FakeCloudflareResponse(status_code=401, success=False, error="unauthorized", text="401 Unauthorized")
    )
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "401" in str(exc.value) or "unauthorized" in str(exc.value).lower()


def test_cloudflare_provider_network_error_is_transient(tmp_path):
    session = FakeCloudflareSession(error=ConnectionError("dns falló"))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "dns falló" in str(exc.value) or "ConnectionError" in str(exc.value)
    # Should have retried 3 times (max_attempts=3)
    assert len(session.calls) == 3


def test_cloudflare_provider_wraps_permanent_failures(tmp_path):
    session = FakeCloudflareSession(
        response=FakeCloudflareResponse(status_code=401, success=False, error="unauthorized")
    )
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "401" in str(exc.value) or "unauthorized" in str(exc.value).lower()
    assert len(session.calls) == 1


def test_cloudflare_provider_network_error_is_transient(tmp_path):
    session = FakeCloudflareSession(error=ConnectionError("dns falló"))
    provider = cloudflare_provider(tmp_path, session)

    with pytest.raises(ImageGenerationError) as exc:
        provider.generate_image("un zorro", make_item(), fingerprint="f" * 64)

    assert "dns falló" in str(exc.value) or "ConnectionError" in str(exc.value)
    # Should have retried 3 times (max_attempts=3)
    assert len(session.calls) == 3
