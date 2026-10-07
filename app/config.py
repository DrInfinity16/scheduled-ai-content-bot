"""Configuration loading.

Environment variables are read in exactly one place so no other module has to
touch ``os.environ`` or ``.env``. Secret values are never logged or printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_CALENDAR_FILE = "calendar.yaml"
DEFAULT_LOG_FILE = os.path.join("log", "posts.jsonl")
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_DATABASE_PATH = "content_bot.db"
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 1.0
DEFAULT_IMAGE_OUTPUT_DIR = os.path.join("artifacts", "images")
CONTENT_SOURCE_VALUES = ("auto", "notion", "yaml")
IMAGE_PROVIDER_VALUES = ("google", "cloudflare")
DEFAULT_IMAGE_PROVIDER = "google"


class ConfigError(Exception):
    """Raised when the environment is not usable for the requested mode."""


@dataclass(frozen=True)
class Config:
    # Secrets carry repr=False so they can never leak through logs or traces.
    gemini_api_key: Optional[str] = field(repr=False)
    model: str
    dry_run: bool
    x_api_key: Optional[str] = field(repr=False)
    x_api_secret: Optional[str] = field(repr=False)
    x_access_token: Optional[str] = field(repr=False)
    x_access_secret: Optional[str] = field(repr=False)
    notion_token: Optional[str] = field(default=None, repr=False)
    notion_database_id: Optional[str] = field(default=None, repr=False)
    content_source: str = "auto"
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    x_username: Optional[str] = None
    calendar_file: str = DEFAULT_CALENDAR_FILE
    log_file: str = DEFAULT_LOG_FILE
    database_path: str = DEFAULT_DATABASE_PATH
    retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    image_provider: str = DEFAULT_IMAGE_PROVIDER
    image_model: Optional[str] = None
    image_output_dir: str = DEFAULT_IMAGE_OUTPUT_DIR
    cloudflare_account_id: Optional[str] = field(default=None, repr=False)
    cloudflare_api_token: Optional[str] = field(default=None, repr=False)

    @property
    def retry_policy(self) -> "RetryPolicy":
        from app.retry import RetryPolicy

        return RetryPolicy(
            max_attempts=self.retry_max_attempts,
            base_delay=self.retry_base_delay_seconds,
        )

    @property
    def has_x_credentials(self) -> bool:
        return all(
            [
                self.x_api_key,
                self.x_api_secret,
                self.x_access_token,
                self.x_access_secret,
            ]
        )

    @property
    def has_notion_credentials(self) -> bool:
        return bool(self.notion_token and self.notion_database_id)

    @property
    def wants_notion(self) -> bool:
        """True when the runtime content source should be Notion."""
        if self.content_source == "yaml":
            return False
        if self.content_source == "notion":
            return True
        return self.has_notion_credentials

    def validate(self) -> "Config":
        if not self.gemini_api_key:
            raise ConfigError("no se encontró GEMINI_API_KEY en .env")
        if not self.dry_run and not self.has_x_credentials:
            raise ConfigError("DRY_RUN=false pero faltan credenciales de X en .env")
        if self.content_source not in CONTENT_SOURCE_VALUES:
            raise ConfigError(
                f"CONTENT_SOURCE inválido: '{self.content_source}' "
                f"(esperado: {' | '.join(CONTENT_SOURCE_VALUES)})"
            )
        if self.content_source == "notion" and not self.has_notion_credentials:
            raise ConfigError(
                "CONTENT_SOURCE=notion pero faltan NOTION_TOKEN / "
                "NOTION_DATABASE_ID en .env"
            )
        if self.content_source == "auto" and bool(self.notion_token) != bool(
            self.notion_database_id
        ):
            raise ConfigError(
                "configuración de Notion incompleta en .env: se necesitan "
                "NOTION_TOKEN y NOTION_DATABASE_ID (o ninguno de los dos)"
            )
        if self.poll_interval_seconds < 1:
            raise ConfigError(
                "CONTENT_POLL_INTERVAL_SECONDS debe ser un entero positivo "
                f"(recibido: {self.poll_interval_seconds})"
            )
        if not (self.database_path or "").strip():
            raise ConfigError("DATABASE_PATH no puede estar vacío")
        if not (self.image_output_dir or "").strip():
            raise ConfigError("IMAGE_OUTPUT_DIR no puede estar vacío")
        if self.retry_max_attempts < 1:
            raise ConfigError(
                "RETRY_MAX_ATTEMPTS debe ser un entero >= 1 "
                f"(recibido: {self.retry_max_attempts})"
            )
        if self.retry_base_delay_seconds < 0:
            raise ConfigError(
                "RETRY_BASE_DELAY_SECONDS no puede ser negativo "
                f"(recibido: {self.retry_base_delay_seconds})"
            )
        if self.image_provider not in IMAGE_PROVIDER_VALUES:
            raise ConfigError(
                f"IMAGE_PROVIDER inválido: '{self.image_provider}' "
                f"(esperado: {' | '.join(IMAGE_PROVIDER_VALUES)})"
            )
        if self.image_provider == "cloudflare":
            if not self.cloudflare_account_id:
                raise ConfigError(
                    "IMAGE_PROVIDER=cloudflare pero falta CLOUDFLARE_ACCOUNT_ID en .env"
                )
            if not self.cloudflare_api_token:
                raise ConfigError(
                    "IMAGE_PROVIDER=cloudflare pero falta CLOUDFLARE_API_TOKEN en .env"
                )
        if self.image_provider == "google" and self.image_model is None:
            # Google provider requires IMAGE_MODEL when an item requests an image;
            # the provider itself will fail at runtime with a clear message,
            # but we keep the config permissive to allow text-only workflows.
            pass
        return self


def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() == "true"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"{name} debe ser un entero, se obtuvo '{raw.strip()}'"
        ) from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"{name} debe ser un número, se obtuvo '{raw.strip()}'"
        ) from exc


def load_config(env_file: Optional[str] = None) -> Config:
    """Load ``.env`` (optional explicit path) and build a :class:`Config`."""
    if env_file is not None:
        load_dotenv(env_file, override=True)
    else:
        load_dotenv()

    return Config(
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        model=os.getenv("MODEL") or os.getenv("GEMINI_MODEL") or DEFAULT_MODEL,
        dry_run=_env_bool("DRY_RUN"),
        x_api_key=os.getenv("X_API_KEY"),
        x_api_secret=os.getenv("X_API_SECRET"),
        x_access_token=os.getenv("X_ACCESS_TOKEN"),
        x_access_secret=os.getenv("X_ACCESS_SECRET"),
        notion_token=os.getenv("NOTION_TOKEN") or None,
        notion_database_id=os.getenv("NOTION_DATABASE_ID") or None,
        content_source=(os.getenv("CONTENT_SOURCE") or "auto").strip().lower(),
        poll_interval_seconds=_env_int(
            "CONTENT_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
        ),
        x_username=(os.getenv("X_USERNAME") or "").strip().lstrip("@") or None,
        calendar_file=os.getenv("CALENDAR_FILE", DEFAULT_CALENDAR_FILE),
        log_file=os.getenv("LOG_FILE", DEFAULT_LOG_FILE),
        database_path=os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip()
        or DEFAULT_DATABASE_PATH,
        retry_max_attempts=_env_int(
            "RETRY_MAX_ATTEMPTS", DEFAULT_RETRY_MAX_ATTEMPTS
        ),
        retry_base_delay_seconds=_env_float(
            "RETRY_BASE_DELAY_SECONDS", DEFAULT_RETRY_BASE_DELAY_SECONDS
        ),
        image_provider=(os.getenv("IMAGE_PROVIDER") or DEFAULT_IMAGE_PROVIDER).strip().lower(),
        image_model=(os.getenv("IMAGE_MODEL") or "").strip() or None,
        image_output_dir=(
            os.getenv("IMAGE_OUTPUT_DIR", DEFAULT_IMAGE_OUTPUT_DIR).strip()
            or DEFAULT_IMAGE_OUTPUT_DIR
        ),
        cloudflare_account_id=(os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip() or None,
        cloudflare_api_token=(os.getenv("CLOUDFLARE_API_TOKEN") or "").strip() or None,
    )
