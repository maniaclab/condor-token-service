"""Shared fixtures: RSA keypair, stubbed JWKS fetch, and a broker-token factory.

The JWKS is never fetched over the network in tests — ``stub_jwks_fetch``
replaces ``identity._fetch_jwks`` (the single network boundary) with an
in-process stub serving keys generated here.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from condor_token_service import identity
from condor_token_service.app import create_app
from condor_token_service.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

TEST_KID = "test-signing-key"

# What the fake condor_token_create prints — deterministic so tests can assert
# both that the response carries it and that no log line ever does.
FAKE_CONDOR_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake-condor-idtoken.deadbeef"


@pytest.fixture(scope="session")
def rsa_private_key() -> rsa.RSAPrivateKey:
    # 2048 bits keeps per-session generation fast while staying a realistic
    # RS256 key size.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_private_key() -> rsa.RSAPrivateKey:
    """A second keypair NOT in the served JWKS — for wrong-signature tests."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(rsa_private_key: rsa.RSAPrivateKey) -> list[dict[str, Any]]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_private_key.public_key()))
    jwk.update({"kid": TEST_KID, "alg": "RS256", "use": "sig"})
    return [jwk]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        broker_jwks_url="https://broker.test/jwks",
        broker_issuer="https://broker.test",
    )


class JwksFetchStub:
    """Callable standing in for ``identity._fetch_jwks``.

    Counts calls, can be told to fail (mimicking the real fetch's 502), and
    can delay to expose single-flight behavior.
    """

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.calls = 0
        self.fail = False
        self.delay = 0.0

    async def __call__(self, jwks_url: str) -> list[dict[str, Any]]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to reach JWKS endpoint: {jwks_url}",
            )
        return self.keys


@pytest.fixture
def stub_jwks_fetch(
    jwks: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> JwksFetchStub:
    identity._jwks_cache.clear()
    stub = JwksFetchStub(jwks)
    monkeypatch.setattr(identity, "_fetch_jwks", stub)
    return stub


@pytest.fixture
def make_token(
    rsa_private_key: rsa.RSAPrivateKey, settings: Settings
) -> Callable[..., str]:
    """Factory for AF Broker Identity Tokens with controllable claims."""

    def _make(
        *,
        sub: str = "af-user-subject",
        unixname: str | None = "gstark",
        issuer: str | None = None,
        audience: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = TEST_KID,
        expires_in: int = 300,
        omit: tuple[str, ...] = (),
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer or settings.broker_issuer,
            "sub": sub,
            "aud": audience or settings.expected_audience,
            "exp": now + expires_in,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "uid": 12345,
            "gid": 12345,
        }
        if unixname is not None:
            claims["unixname"] = unixname
        if extra:
            claims.update(extra)
        for claim in omit:
            claims.pop(claim, None)
        headers = {"kid": kid} if kid is not None else None
        return jwt.encode(claims, key or rsa_private_key, algorithm="RS256", headers=headers)

    return _make


class FakeCondorBin(NamedTuple):
    path: Path
    args_file: Path


def _install_fake_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> Path:
    """Write an executable ``condor_token_create`` shell script into a tmpdir and prepend that tmpdir to PATH."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "condor_token_create"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


@pytest.fixture
def fake_condor_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCondorBin:
    """A fake condor_token_create on PATH: records its argv, prints a deterministic token."""
    args_file = tmp_path / "condor_args.txt"
    script = _install_fake_bin(
        tmp_path,
        monkeypatch,
        f'echo "$@" > "{args_file}"\nprintf \'%s\\n\' "{FAKE_CONDOR_TOKEN}"',
    )
    return FakeCondorBin(path=script, args_file=args_file)


@pytest.fixture
def failing_condor_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake condor_token_create on PATH that fails like a real one whose pool password is unreadable."""
    return _install_fake_bin(
        tmp_path,
        monkeypatch,
        'echo "ERROR: could not read pool password file" >&2\nexit 1',
    )


@pytest.fixture
def make_client(
    stub_jwks_fetch: JwksFetchStub,
) -> Callable[[Settings], httpx.AsyncClient]:
    """Factory building an ASGI test client around a fresh app for *settings*."""

    def _make(settings: Settings) -> httpx.AsyncClient:
        app = create_app(settings)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    return _make


@pytest.fixture
async def client(
    make_client: Callable[[Settings], httpx.AsyncClient], settings: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(settings) as test_client:
        yield test_client
