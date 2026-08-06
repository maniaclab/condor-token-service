"""Integration tests for IDTOKEN minting via a real (fake) condor_token_create subprocess.

No Python-level mocking here: the binary under test is an executable shell
script on PATH, exactly how the real condor_token_create is invoked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from condor_token_service.config import Settings
from condor_token_service.minting import MintingError, mint_token
from tests.conftest import FAKE_CONDOR_TOKEN, _install_fake_bin

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import FakeCondorBin


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, token_lifetime_seconds=1800)


class TestMintToken:
    async def test_returns_token_from_binary_stdout(
        self, fake_condor_bin: FakeCondorBin, settings: Settings
    ) -> None:
        minted = await mint_token("gstark", settings)
        assert minted.token == FAKE_CONDOR_TOKEN

    async def test_identity_is_unixname_at_domain(
        self, fake_condor_bin: FakeCondorBin, settings: Settings
    ) -> None:
        minted = await mint_token("gstark", settings)
        assert minted.identity == "gstark@af.uchicago.edu"

    async def test_binary_invoked_with_identity_and_lifetime(
        self, fake_condor_bin: FakeCondorBin, settings: Settings
    ) -> None:
        await mint_token("gstark", settings)
        recorded = fake_condor_bin.args_file.read_text().split()
        assert recorded == [
            "-identity",
            "gstark@af.uchicago.edu",
            "-lifetime",
            "1800",
        ]

    async def test_lifetime_flag_is_always_passed(
        self, fake_condor_bin: FakeCondorBin, settings: Settings
    ) -> None:
        """Security invariant, not a stylistic choice.

        Pool-spike finding (docs/pool-spike.md): condor_token_create invoked
        WITHOUT -lifetime mints a token with no exp claim — it never expires.
        This service must be structurally incapable of doing that: the flag
        is always present and always carries the configured positive value
        (Settings rejects <= 0, see test_config.py).
        """
        await mint_token("gstark", settings)
        recorded = fake_condor_bin.args_file.read_text().split()
        assert "-lifetime" in recorded
        lifetime_value = recorded[recorded.index("-lifetime") + 1]
        assert int(lifetime_value) == settings.token_lifetime_seconds
        assert int(lifetime_value) > 0

    async def test_expires_at_reflects_configured_lifetime(
        self, fake_condor_bin: FakeCondorBin, settings: Settings
    ) -> None:
        before = datetime.now(UTC)
        minted = await mint_token("gstark", settings)
        after = datetime.now(UTC)
        lifetime = timedelta(seconds=settings.token_lifetime_seconds)
        assert before + lifetime <= minted.expires_at <= after + lifetime
        assert minted.expires_at.tzinfo is not None

    async def test_nonzero_exit_raises_without_leaking_stderr(
        self, failing_condor_bin: Path, settings: Settings
    ) -> None:
        with pytest.raises(MintingError) as excinfo:
            await mint_token("gstark", settings)
        # stderr is logged server-side but must never reach the exception
        # message a route handler might echo to the client.
        assert "pool password" not in str(excinfo.value)

    async def test_missing_binary_raises(self, settings: Settings) -> None:
        missing = Settings(
            _env_file=None, condor_token_create_bin="/nonexistent/condor_token_create"
        )
        with pytest.raises(MintingError):
            await mint_token("gstark", missing)

    async def test_empty_stdout_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings,
    ) -> None:
        _install_fake_bin(tmp_path, monkeypatch, "exit 0")
        with pytest.raises(MintingError):
            await mint_token("gstark", settings)


class TestTrustDomain:
    """The container has no HTCondor config, so a bare condor_token_create
    derives TRUST_DOMAIN from the local hostname and mints tokens whose
    ``iss`` is the pod name -- rejected by the schedd and by anything
    pinning the pool trust domain. When ``condor_trust_domain`` is set, it
    must reach the subprocess as the ``_CONDOR_TRUST_DOMAIN`` config
    override; when unset, the environment must not carry the variable (a
    host-run service should keep deriving from the real local config).
    """

    async def test_trust_domain_env_passed_to_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "trust_domain.txt"
        _install_fake_bin(
            tmp_path,
            monkeypatch,
            f'echo "${{_CONDOR_TRUST_DOMAIN:-UNSET}}" > "{env_file}"\n'
            f"printf '%s\\n' \"{FAKE_CONDOR_TOKEN}\"",
        )
        settings = Settings(
            _env_file=None, condor_trust_domain="head01.af.uchicago.edu"
        )
        await mint_token("alice", settings)
        assert env_file.read_text().strip() == "head01.af.uchicago.edu"

    async def test_trust_domain_env_absent_when_unconfigured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_file = tmp_path / "trust_domain.txt"
        _install_fake_bin(
            tmp_path,
            monkeypatch,
            f'echo "${{_CONDOR_TRUST_DOMAIN:-UNSET}}" > "{env_file}"\n'
            f"printf '%s\\n' \"{FAKE_CONDOR_TOKEN}\"",
        )
        settings = Settings(_env_file=None)
        await mint_token("alice", settings)
        assert env_file.read_text().strip() == "UNSET"
