"""Unit tests for Settings (env-driven configuration)."""

from __future__ import annotations

import pytest

from condor_token_service.config import Settings, get_settings


class TestDefaults:
    def test_expected_audience_defaults_to_service_name(self) -> None:
        assert Settings(_env_file=None).expected_audience == "condor-token-service"

    def test_condor_identity_domain_default(self) -> None:
        assert Settings(_env_file=None).condor_identity_domain == "af.uchicago.edu"

    def test_token_lifetime_default(self) -> None:
        assert Settings(_env_file=None).token_lifetime_seconds == 3600

    def test_rate_limit_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.rate_limit_max_mints == 30
        assert settings.rate_limit_window_seconds == 300

    def test_condor_token_create_bin_default(self) -> None:
        assert Settings(_env_file=None).condor_token_create_bin == "condor_token_create"

    def test_jwks_cache_ttl_default(self) -> None:
        assert Settings(_env_file=None).jwks_cache_ttl_seconds == 300


class TestEnvOverrides:
    def test_env_vars_override_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BROKER_JWKS_URL", "https://broker.example/jwks")
        monkeypatch.setenv("BROKER_ISSUER", "https://broker.example")
        monkeypatch.setenv("EXPECTED_AUDIENCE", "other-audience")
        monkeypatch.setenv("CONDOR_IDENTITY_DOMAIN", "example.org")
        monkeypatch.setenv("TOKEN_LIFETIME_SECONDS", "60")
        monkeypatch.setenv("RATE_LIMIT_MAX_MINTS", "5")
        monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "10")
        monkeypatch.setenv("CONDOR_TOKEN_CREATE_BIN", "/opt/condor/bin/condor_token_create")

        settings = Settings(_env_file=None)
        assert settings.broker_jwks_url == "https://broker.example/jwks"
        assert settings.broker_issuer == "https://broker.example"
        assert settings.expected_audience == "other-audience"
        assert settings.condor_identity_domain == "example.org"
        assert settings.token_lifetime_seconds == 60
        assert settings.rate_limit_max_mints == 5
        assert settings.rate_limit_window_seconds == 10
        assert settings.condor_token_create_bin == "/opt/condor/bin/condor_token_create"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
