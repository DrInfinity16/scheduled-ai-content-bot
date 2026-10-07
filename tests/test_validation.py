import pytest

from app.validation import X_MAX_LENGTH, validate_content, validate_item


def test_valid_x_post_is_accepted(make_item):
    item = make_item(generated_content="hola")
    assert validate_content(item, "h" * X_MAX_LENGTH).ok


def test_item_without_topic_is_rejected(make_item):
    item = make_item(topic="   ")
    result = validate_item(item)
    assert not result.ok
    assert any("topic" in error for error in result.errors)


def test_item_without_angle_is_rejected(make_item):
    item = make_item(angle="")
    result = validate_item(item)
    assert not result.ok
    assert any("angle" in error for error in result.errors)


def test_empty_generated_content_is_rejected(make_item):
    item = make_item()
    result = validate_content(item, "")
    assert not result.ok
    assert any("vacío" in error for error in result.errors)

    blank = validate_content(item, "   ")
    assert not blank.ok


def test_unsupported_platform_is_rejected(make_item):
    item = make_item(platform="instagram")
    result = validate_item(item)
    assert not result.ok
    assert any("instagram" in error for error in result.errors)


def test_too_long_content_is_rejected(make_item):
    item = make_item()
    result = validate_content(item, "x" * (X_MAX_LENGTH + 1))
    assert not result.ok
    assert any(str(X_MAX_LENGTH) in error for error in result.errors)


def test_errors_are_aggregated(make_item):
    item = make_item(topic="", platform="tiktok")
    result = validate_content(item, "")
    assert len(result.errors) >= 3


def test_status_enum_coerces_strings_and_enums():
    from app.models import ContentItem, ContentStatus

    assert ContentStatus.coerce("published") is ContentStatus.PUBLISHED
    assert ContentStatus.coerce(ContentStatus.FAILED) is ContentStatus.FAILED
    assert ContentItem(id="a", topic="t", angle="a", status="ready").status is ContentStatus.READY
