"""Integration tests against a real HTCondor pool (htcondor/mini in docker).

The unit/integration tests elsewhere validate service logic against a fake
binary; this module validates the claim that actually matters: tokens minted
with our exact flags authenticate against a real schedd as the right
identity.

Gated on ``CONDOR_MINI_INTEGRATION=1`` so plain ``pixi run test`` stays
docker-free; CI runs it in the dedicated ``integration-condor`` job.

Encodes the deployment gotcha this layer exists to catch:
``condor_token_create`` derives the token's ``iss`` claim from the local
condor config's TRUST_DOMAIN, and the schedd rejects mismatched issuers —
so the production pod needs minimal condor config aligned with the pool
(TRUST_DOMAIN), not just the pool password file. See docs/pool-spike.md.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import jwt
import pytest

from condor_token_service.config import Settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx

pytestmark = pytest.mark.skipif(
    os.environ.get("CONDOR_MINI_INTEGRATION") != "1",
    reason=(
        "requires docker and the htcondor/mini image; "
        "set CONDOR_MINI_INTEGRATION=1 to run"
    ),
)

_IMAGE = "htcondor/mini:latest"
_POOL_KEY = "/etc/condor/passwords.d/POOL"
_READINESS_TIMEOUT_SECONDS = 180.0
# The mini image's unprivileged submit user, used for the schedd-side
# authentication check (root would be a meaningless test subject).
_SUBMIT_USER = "submituser"


def _docker(
    *args: str, check: bool = True, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        input=input_text,
        timeout=120,
    )


def _exec(
    container: str,
    *args: str,
    user: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["exec"]
    if input_text is not None:
        cmd.append("-i")
    if user is not None:
        cmd.extend(["-u", user])
    for key, value in (env or {}).items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([container, *args])
    return _docker(*cmd, check=check, input_text=input_text)


@pytest.fixture(scope="module")
def condor_container() -> Iterator[str]:
    """A running htcondor/mini container, removed again no matter what.

    Readiness means ``condor_status`` succeeds inside the container. The
    image auto-generates the pool signing key (SEC_TOKEN_POOL_SIGNING_KEY_FILE
    = /etc/condor/passwords.d/POOL, 0600 root:root) during startup — verified
    by inspection; the explicit check below both documents that and creates
    one should a future image stop doing so.
    """
    name = f"cts-minicondor-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--rm", "--name", name, _IMAGE)
    try:
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            if _exec(name, "condor_status", check=False).returncode == 0:
                break
            if time.monotonic() > deadline:
                logs = _docker("logs", name, check=False).stdout[-2000:]
                pytest.fail(
                    f"htcondor/mini pool not ready within "
                    f"{_READINESS_TIMEOUT_SECONDS}s; last logs:\n{logs}"
                )
            time.sleep(5)

        if _exec(name, "test", "-s", _POOL_KEY, check=False).returncode != 0:
            _exec(
                name,
                "sh",
                "-c",
                f"umask 077 && head -c 64 /dev/urandom > {_POOL_KEY}",
            )
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture(scope="module")
def trust_domain(condor_container: str) -> str:
    return _exec(condor_container, "condor_config_val", "TRUST_DOMAIN").stdout.strip()


class TestCommandContract:
    """Part (a): the exact command our service runs mints what we assume it does."""

    def test_token_claims_match_flags(
        self, condor_container: str, trust_domain: str
    ) -> None:
        identity = f"testuser@{trust_domain}"
        result = _exec(
            condor_container,
            "condor_token_create",
            "-identity",
            identity,
            "-lifetime",
            "3600",
        )
        token = result.stdout.strip()
        assert token, f"condor_token_create printed nothing; stderr: {result.stderr}"

        # Deliberately unverified decode: the signature check belongs to the
        # schedd (exercised in TestFullServicePath below); this asserts the
        # claim structure our deployment assumptions rest on.
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == trust_domain
        assert claims["sub"] == identity
        assert claims["exp"] - claims["iat"] == 3600


class TestFullServicePath:
    """Part (b): broker token in → IDTOKEN out → schedd accepts it as that identity."""

    async def test_minted_token_authenticates_to_schedd(
        self,
        condor_container: str,
        trust_domain: str,
        tmp_path: Path,
        make_client: Callable[[Settings], httpx.AsyncClient],
        make_token: Callable[..., str],
    ) -> None:
        # The service's CONDOR_TOKEN_CREATE_BIN points at a wrapper that runs
        # the real binary inside the container — the service still invokes an
        # executable with its exact production argv.
        wrapper = tmp_path / "condor_token_create"
        wrapper.write_text(
            f'#!/bin/sh\nexec docker exec {condor_container} condor_token_create "$@"\n'
        )
        wrapper.chmod(0o755)

        # condor_identity_domain is set to the pool's trust domain here
        # because in htcondor/mini the two COINCIDE: a single-host pool maps
        # user@<host> identities and issues iss=<host>. Real pools separate
        # them — the identity domain is the pool's user/UID domain
        # (condor_config_val UID_DOMAIN), and the AF pool spike
        # (docs/pool-spike.md, finding 1) showed the schedd rejecting
        # trust-domain identities outright. That coincidence is exactly why
        # this docker layer could not retire the domain risk and the
        # real-pool spike was needed.
        settings = Settings(
            _env_file=None,
            broker_jwks_url="https://broker.test/jwks",
            broker_issuer="https://broker.test",
            condor_identity_domain=trust_domain,
            condor_token_create_bin=str(wrapper),
        )
        async with make_client(settings) as client:
            resp = await client.post(
                "/v1/token",
                headers={"Authorization": f"Bearer {make_token(unixname='testuser')}"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["identity"] == f"testuser@{trust_domain}"
        idtoken = body["token"]

        # Install the token for the unprivileged submit user.
        _exec(
            condor_container,
            "sh",
            "-c",
            (
                f"mkdir -p ~{_SUBMIT_USER}/.condor/tokens.d && "
                f"cat > ~{_SUBMIT_USER}/.condor/tokens.d/cts-test && "
                f"chown -R {_SUBMIT_USER}:{_SUBMIT_USER} ~{_SUBMIT_USER}/.condor && "
                f"chmod 600 ~{_SUBMIT_USER}/.condor/tokens.d/cts-test"
            ),
            input_text=idtoken + "\n",
        )

        # Force IDTOKENS: as a local user, submituser would otherwise win
        # with FS authentication and prove nothing about our token.
        idtokens_only = {"_condor_SEC_CLIENT_AUTHENTICATION_METHODS": "IDTOKENS"}

        ping = _exec(
            condor_container,
            "condor_ping",
            "-type",
            "SCHEDD",
            "WRITE",
            user=_SUBMIT_USER,
            env=idtokens_only,
        )
        # e.g. "WRITE command using (AES, AES, and IDTOKENS) succeeded as
        #       testuser@<trust domain> to local schedd."
        assert "IDTOKENS" in ping.stdout, ping.stdout
        assert f"succeeded as testuser@{trust_domain}" in ping.stdout, ping.stdout

        condor_q = _exec(
            condor_container,
            "condor_q",
            user=_SUBMIT_USER,
            env=idtokens_only,
            check=False,
        )
        assert condor_q.returncode == 0, condor_q.stderr
