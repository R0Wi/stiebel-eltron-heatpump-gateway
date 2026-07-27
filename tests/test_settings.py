"""Locks in the settings precedence: env vars > YAML file > defaults."""

from pathlib import Path

import pytest

from stiebel_heatpump.settings import AppSettings

ROOT = Path(__file__).resolve().parent.parent
APP_CONFIG = ROOT / "config" / "app.example.yaml"
DEVICE_CONFIG = "device_configs/LWZ_THZ504_7_59.xml"


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("STIEBEL_"):
            monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_yaml_file_alone_is_applied(clean_env):
    settings = AppSettings.load(str(APP_CONFIG))
    assert settings.transport == "simulator"
    assert settings.waiting_time_ms == 1200


def test_env_var_overrides_yaml_file(clean_env):
    clean_env.setenv("STIEBEL_TRANSPORT", "serial")
    clean_env.setenv("STIEBEL_WAITING_TIME_MS", "999")
    settings = AppSettings.load(str(APP_CONFIG))
    assert settings.transport == "serial"
    assert settings.waiting_time_ms == 999
    # Fields not overridden by env still come from the file.
    assert settings.device_config == DEVICE_CONFIG


def test_env_only_falls_back_to_defaults_for_unset_fields(clean_env):
    clean_env.setenv("STIEBEL_DEVICE_CONFIG", DEVICE_CONFIG)
    settings = AppSettings.load(None)
    assert settings.device_config == DEVICE_CONFIG
    assert settings.transport == "simulator"  # default, no yaml/env for it


def test_stiebel_app_config_env_var_locates_file(clean_env):
    clean_env.setenv("STIEBEL_APP_CONFIG", str(APP_CONFIG))
    settings = AppSettings.load(None)
    assert settings.device_config == DEVICE_CONFIG
