"""Application settings.

Every field below can be set two ways, and both can be mixed:

1. **A YAML file** — see ``config/app.example.yaml``. Point ``load()`` at it via
   the ``--config`` CLI flag, or set the ``STIEBEL_APP_CONFIG`` environment
   variable to its path.
2. **Environment variables** — prefix ``STIEBEL_`` + the field name in
   UPPER_SNAKE_CASE. E.g. ``device_config`` -> ``STIEBEL_DEVICE_CONFIG``,
   ``waiting_time_ms`` -> ``STIEBEL_WAITING_TIME_MS``. This is the convention
   ``pydantic-settings`` applies automatically from ``env_prefix`` below; no
   per-field aliases are declared, so it always matches the attribute name.

**Precedence (highest wins) when both are set for the same field:**

    environment variable  >  YAML file  >  field default

This matches how containers are normally operated: an image can ship a
baked-in YAML file as its base configuration, and ``docker run -e`` /
``docker-compose.yml``'s ``environment:`` can override individual values
without rebuilding the image or editing the file.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class AppSettings(BaseSettings):
    """Runtime configuration for the REST service."""

    model_config = SettingsConfigDict(env_prefix="STIEBEL_", extra="ignore")

    # Path of a YAML file to layer in underneath env vars; set via `load()`.
    # Not a real settings source itself (no STIEBEL_ env var for it beyond
    # the special-cased STIEBEL_APP_CONFIG handled in `load`).
    _yaml_file: ClassVar[Optional[str]] = None

    # Which heat pump protocol definition to expose (XML or YAML).
    device_config: str = Field(
        ...,
        description="Path to a channel-definition file (binding XML or native YAML).",
    )

    # Transport selection.
    transport: Literal["serial", "simulator"] = Field(
        "simulator",
        description="'serial' for a real USB device, 'simulator' for offline use.",
    )
    port: str = Field("/dev/ttyUSB0", description="Serial device path (serial transport).")
    baud_rate: int = Field(115200, description="Serial baud rate.")
    waiting_time_ms: int = Field(500, description="Delay between serial requests in ms.")

    # API metadata.
    title: str = Field("Stiebel Eltron Heat Pump API", description="OpenAPI title.")
    connect_on_startup: bool = Field(True, description="Open the transport when the app starts.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = precedence, highest first. `init_settings` covers explicit
        # constructor kwargs (mainly used by tests); `env_settings` is the
        # STIEBEL_* environment variables; the YAML source (if any file was
        # given to `load()`) sits below both, so it acts as a base/default
        # layer that env vars can always override.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if cls._yaml_file:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=cls._yaml_file))
        sources.append(file_secret_settings)
        return tuple(sources)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "AppSettings":
        """Build settings from ``config_path`` (or ``STIEBEL_APP_CONFIG``), env vars and defaults.

        ``config_path`` takes priority over the ``STIEBEL_APP_CONFIG`` env var
        purely as a way to *locate* the file; the values inside that file are
        still overridable by any other ``STIEBEL_*`` env var (see precedence
        note in the module docstring).
        """
        import os

        cls._yaml_file = config_path or os.environ.get("STIEBEL_APP_CONFIG")
        try:
            return cls()
        finally:
            cls._yaml_file = None
