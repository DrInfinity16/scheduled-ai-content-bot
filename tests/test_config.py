import os

import pytest

from app.config import Config, ConfigError, DEFAULT_MODEL, load_config

ENV_KEYS = (
    "GEMINI_API_KEY",
    "MODEL",
    "GEMINI_MODEL",
    "DRY_RUN",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_SECRET",
    "X_USERNAME",
    "CALENDAR_FILE",
    "LOG_FILE",
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "CONTENT_SOURCE",
    "CONTENT_POLL_INTERVAL_SECONDS",
    "DATABASE_PATH",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_BASE_DELAY_SECONDS",
    "IMAGE_MODEL",
    "IMAGE_OUTPUT_DIR",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Tests never inherit credentials from the developer's real .env."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    # load_dotenv() writes into os.environ: clear it again so nothing leaks
    # into other test modules.
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_config(**overrides):
    defaults = dict(
        gemini_api_key="gemini-key",
        model=DEFAULT_MODEL,
        dry_run=True,
        x_api_key=None,
        x_api_secret=None,
        x_access_token=None,
        x_access_secret=None,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_valid_dry_run_config_is_accepted():
    assert make_config().validate().dry_run is True


def test_missing_gemini_key_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(gemini_api_key=None).validate()
    assert "GEMINI_API_KEY" in str(exc.value)


def test_real_mode_without_x_credentials_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(dry_run=False).validate()
    assert "DRY_RUN=false" in str(exc.value)


def test_real_mode_with_x_credentials_is_accepted():
    config = make_config(
        dry_run=False,
        x_api_key="k",
        x_api_secret="s",
        x_access_token="t",
        x_access_secret="ts",
    )
    assert config.validate().has_x_credentials is True


def test_load_config_reads_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "MODEL=gemini-custom\n"
        "DRY_RUN=false\n"
        "X_API_KEY=xk\n"
        "X_API_SECRET=xs\n"
        "X_ACCESS_TOKEN=xt\n"
        "X_ACCESS_SECRET=xts\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.gemini_api_key == "abc"
    assert config.model == "gemini-custom"
    assert config.dry_run is False
    assert config.has_x_credentials is True
    assert config.calendar_file.endswith("calendar.yaml")


def test_dry_run_defaults_to_true(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=abc\n", encoding="utf-8")

    assert load_config(str(env_file)).dry_run is True


def test_load_config_without_env_file_uses_dotenv():
    # Falls back to the repository .env (values may be empty placeholders).
    config = load_config()
    assert config.model
    assert isinstance(config.dry_run, bool)


def test_config_never_exposes_secrets_in_repr():
    config = make_config(gemini_api_key="super-secreto")
    assert "super-secreto" not in repr(config)


def test_notion_secrets_are_never_exposed_in_repr():
    config = make_config(notion_token="secret_abc", notion_database_id="db_123")
    assert "secret_abc" not in repr(config)
    assert "db_123" not in repr(config)


# -- source selection ------------------------------------------------------


def test_auto_source_prefers_notion_when_credentials_exist():
    config = make_config(notion_token="t", notion_database_id="d")
    assert config.wants_notion is True
    assert config.has_notion_credentials is True
    assert config.validate().wants_notion is True


def test_auto_source_falls_back_to_yaml_without_credentials():
    config = make_config()
    assert config.wants_notion is False
    assert config.validate().wants_notion is False


def test_yaml_source_ignores_notion_credentials():
    config = make_config(
        content_source="yaml", notion_token="t", notion_database_id="d"
    )
    assert config.wants_notion is False
    assert config.validate().wants_notion is False


def test_explicit_notion_source_without_credentials_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(content_source="notion").validate()
    assert "NOTION_TOKEN" in str(exc.value)
    assert "NOTION_DATABASE_ID" in str(exc.value)


def test_partial_notion_configuration_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(notion_token="solo-token").validate()
    assert "incompleta" in str(exc.value)


def test_invalid_content_source_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(content_source="calendar").validate()
    assert "CONTENT_SOURCE" in str(exc.value)
    assert "calendar" in str(exc.value)


# -- polling interval ------------------------------------------------------


def test_poll_interval_defaults_to_60():
    assert make_config().poll_interval_seconds == 60


def test_non_positive_poll_interval_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(poll_interval_seconds=0).validate()
    assert "CONTENT_POLL_INTERVAL_SECONDS" in str(exc.value)


def test_load_config_reads_notion_polling_and_username(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "NOTION_TOKEN=secret_notion\n"
        "NOTION_DATABASE_ID=db_42\n"
        "CONTENT_SOURCE=notion\n"
        "CONTENT_POLL_INTERVAL_SECONDS=120\n"
        "X_USERNAME=@ana_dev\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.notion_token == "secret_notion"
    assert config.notion_database_id == "db_42"
    assert config.content_source == "notion"
    assert config.poll_interval_seconds == 120
    assert config.x_username == "ana_dev"
    assert config.wants_notion is True
    assert config.validate().has_notion_credentials is True


def test_poll_interval_env_must_be_an_integer(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\nCONTENT_POLL_INTERVAL_SECONDS=pronto\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(str(env_file))
    assert "CONTENT_POLL_INTERVAL_SECONDS" in str(exc.value)


def test_empty_poll_interval_env_uses_the_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\nCONTENT_POLL_INTERVAL_SECONDS=\n",
        encoding="utf-8",
    )

    assert load_config(str(env_file)).poll_interval_seconds == 60


# -- SQLite state + bounded retries ---------------------------------------


def test_state_and_retry_defaults():
    config = make_config().validate()

    assert config.database_path == "content_bot.db"
    assert config.retry_max_attempts == 3
    assert config.retry_base_delay_seconds == 1.0


def test_retry_policy_property_uses_config_values():
    config = make_config(retry_max_attempts=5, retry_base_delay_seconds=2.5)

    policy = config.retry_policy

    assert policy.max_attempts == 5
    assert policy.base_delay == 2.5
    assert policy.delay_for(1) == 2.5
    assert policy.delay_for(2) == 5.0


def test_load_config_reads_state_and_retry_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "DATABASE_PATH=data/bot.db\n"
        "RETRY_MAX_ATTEMPTS=5\n"
        "RETRY_BASE_DELAY_SECONDS=0.5\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.database_path == "data/bot.db"
    assert config.retry_max_attempts == 5
    assert config.retry_base_delay_seconds == 0.5


def test_empty_database_path_env_uses_the_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=abc\nDATABASE_PATH=\n", encoding="utf-8")

    assert load_config(str(env_file)).database_path == "content_bot.db"


def test_empty_retry_env_values_use_the_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "RETRY_MAX_ATTEMPTS=\n"
        "RETRY_BASE_DELAY_SECONDS=\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))
    assert config.retry_max_attempts == 3
    assert config.retry_base_delay_seconds == 1.0


def test_empty_database_path_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(database_path="").validate()

    assert "DATABASE_PATH" in str(exc.value)


@pytest.mark.parametrize(
    "kwargs,marker",
    [
        ({"retry_max_attempts": 0}, "RETRY_MAX_ATTEMPTS"),
        ({"retry_max_attempts": -1}, "RETRY_MAX_ATTEMPTS"),
        ({"retry_base_delay_seconds": -0.5}, "RETRY_BASE_DELAY_SECONDS"),
    ],
)
def test_invalid_retry_values_are_rejected(kwargs, marker):
    with pytest.raises(ConfigError) as exc:
        make_config(**kwargs).validate()

    assert marker in str(exc.value)


def test_non_numeric_retry_max_attempts_env_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\nRETRY_MAX_ATTEMPTS=mucho\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc:
        load_config(str(env_file))

    assert "RETRY_MAX_ATTEMPTS" in str(exc.value)


def test_non_numeric_retry_delay_env_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\nRETRY_BASE_DELAY_SECONDS=lento\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc:
        load_config(str(env_file))

    assert "RETRY_BASE_DELAY_SECONDS" in str(exc.value)


# -- Pass 4: image configuration ------------------------------------------


def test_image_defaults():
    config = make_config().validate()

    assert config.image_model is None
    assert config.image_output_dir == os.path.join("artifacts", "images")


def test_load_config_reads_image_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "IMAGE_MODEL=modelo-visual\n"
        "IMAGE_OUTPUT_DIR=data/imagenes\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.image_model == "modelo-visual"
    assert config.image_output_dir == "data/imagenes"


def test_blank_image_model_env_stays_unset(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=abc\nIMAGE_MODEL=\n", encoding="utf-8")

    assert load_config(str(env_file)).image_model is None


def test_blank_image_output_dir_env_uses_the_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\nIMAGE_OUTPUT_DIR=\n", encoding="utf-8"
    )

    config = load_config(str(env_file))
    assert config.image_output_dir == os.path.join("artifacts", "images")


def test_empty_image_output_dir_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(image_output_dir="").validate()

    assert "IMAGE_OUTPUT_DIR" in str(exc.value)


# -- Cloudflare image provider configuration --------------------------------


def test_image_provider_defaults_to_google():
    config = make_config().validate()

    assert config.image_provider == "google"


def test_load_config_reads_image_provider_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "IMAGE_PROVIDER=cloudflare\n"
        "CLOUDFLARE_ACCOUNT_ID=test-account\n"
        "CLOUDFLARE_API_TOKEN=test-token\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.image_provider == "cloudflare"
    assert config.cloudflare_account_id == "test-account"
    assert config.cloudflare_api_token == "test-token"


def test_invalid_image_provider_is_rejected():
    with pytest.raises(ConfigError) as exc:
        make_config(image_provider="invalid").validate()

    assert "IMAGE_PROVIDER" in str(exc.value)
    assert "invalid" in str(exc.value)


def test_cloudflare_provider_requires_account_id():
    with pytest.raises(ConfigError) as exc:
        make_config(image_provider="cloudflare", cloudflare_account_id=None).validate()

    assert "CLOUDFLARE_ACCOUNT_ID" in str(exc.value)


def test_cloudflare_provider_requires_api_token():
    with pytest.raises(ConfigError) as exc:
        make_config(
            image_provider="cloudflare",
            cloudflare_account_id="test-account",
            cloudflare_api_token=None,
        ).validate()

    assert "CLOUDFLARE_API_TOKEN" in str(exc.value)


def test_google_provider_does_not_require_cloudflare_vars():
    config = make_config(
        image_provider="google",
        cloudflare_account_id=None,
        cloudflare_api_token=None,
    ).validate()

    assert config.image_provider == "google"
    assert config.cloudflare_account_id is None
    assert config.cloudflare_api_token is None


def test_cloudflare_provider_with_custom_model(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_API_KEY=abc\n"
        "IMAGE_PROVIDER=cloudflare\n"
        "CLOUDFLARE_ACCOUNT_ID=test-account\n"
        "CLOUDFLARE_API_TOKEN=test-token\n"
        "IMAGE_MODEL=@cf/custom/model\n",
        encoding="utf-8",
    )

    config = load_config(str(env_file))

    assert config.image_provider == "cloudflare"
    assert config.image_model == "@cf/custom/model"
