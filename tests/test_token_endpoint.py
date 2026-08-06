"""Integration tests for POST /v1/token through the ASGI stack.

The condor_token_create the app invokes is a real executable shell script on
PATH (see conftest) — nothing is mocked at the Python level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from condor_token_service.config import Settings
from tests.conftest import FAKE_CONDOR_TOKEN

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit_events(cap_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in cap_logs if entry.get("event") == "audit"]


@pytest.mark.usefixtures("fake_condor_bin")
class TestHappyPath:
    async def test_returns_minted_token(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        resp = await client.post("/v1/token", headers=_auth(make_token()))
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"] == FAKE_CONDOR_TOKEN
        assert body["identity"] == "gstark@af.uchicago.edu"

    async def test_expires_at_is_iso8601_utc(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        resp = await client.post("/v1/token", headers=_auth(make_token()))
        expires_at = datetime.fromisoformat(resp.json()["expires_at"])
        assert expires_at.tzinfo is not None
        assert expires_at > datetime.now(UTC)

    async def test_audit_line_carries_required_fields(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        with capture_logs() as cap_logs:
            resp = await client.post(
                "/v1/token",
                headers={**_auth(make_token()), "X-Request-ID": "req-42"},
            )
        assert resp.status_code == 200
        (audit,) = _audit_events(cap_logs)
        assert audit["subject"] == "af-user-subject"
        assert audit["identity"] == "gstark@af.uchicago.edu"
        assert audit["jti"]
        assert audit["outcome"] == "issued"
        assert audit["request_id"] == "req-42"

    async def test_no_log_line_ever_contains_a_token(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        broker_token = make_token()
        with capture_logs() as cap_logs:
            resp = await client.post("/v1/token", headers=_auth(broker_token))
        assert resp.status_code == 200
        logged = repr(cap_logs)
        assert FAKE_CONDOR_TOKEN not in logged
        assert broker_token not in logged


class TestAuthenticationFailures:
    async def test_missing_authorization_header_is_401(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.post("/v1/token")
        assert resp.status_code == 401

    async def test_expired_token_is_401(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        resp = await client.post("/v1/token", headers=_auth(make_token(expires_in=-60)))
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"

    async def test_wrong_audience_is_401(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        resp = await client.post(
            "/v1/token", headers=_auth(make_token(audience="not-us"))
        )
        assert resp.status_code == 401

    async def test_wrong_issuer_is_401(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        resp = await client.post(
            "/v1/token", headers=_auth(make_token(issuer="https://evil.example"))
        )
        assert resp.status_code == 401

    async def test_denied_request_is_audited(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        with capture_logs() as cap_logs:
            await client.post("/v1/token", headers=_auth(make_token(expires_in=-60)))
        (audit,) = _audit_events(cap_logs)
        assert audit["outcome"] == "denied"


class TestUnixnameRequirement:
    @pytest.mark.parametrize("unixname", [None, "", "   "])
    async def test_missing_or_blank_unixname_is_403(
        self,
        client: httpx.AsyncClient,
        make_token: Callable[..., str],
        unixname: str | None,
    ) -> None:
        token = (
            make_token(unixname=None)
            if unixname is None
            else make_token(unixname=unixname)
        )
        with capture_logs() as cap_logs:
            resp = await client.post("/v1/token", headers=_auth(token))
        assert resp.status_code == 403
        assert "unixname" in resp.json()["detail"]
        (audit,) = _audit_events(cap_logs)
        assert audit["outcome"] == "denied"
        assert audit["subject"] == "af-user-subject"


class TestMintingFailure:
    @pytest.mark.usefixtures("failing_condor_bin")
    async def test_binary_failure_is_502_with_generic_detail(
        self, client: httpx.AsyncClient, make_token: Callable[..., str]
    ) -> None:
        with capture_logs() as cap_logs:
            resp = await client.post("/v1/token", headers=_auth(make_token()))
        assert resp.status_code == 502
        # stderr must never leak to the client.
        assert "pool password" not in resp.text
        (audit,) = _audit_events(cap_logs)
        assert audit["outcome"] == "error"


@pytest.mark.usefixtures("fake_condor_bin")
class TestRateLimit:
    async def test_over_limit_is_429_with_retry_after(
        self,
        make_client: Callable[[Settings], httpx.AsyncClient],
        make_token: Callable[..., str],
    ) -> None:
        settings = Settings(
            _env_file=None,
            broker_jwks_url="https://broker.test/jwks",
            broker_issuer="https://broker.test",
            rate_limit_max_mints=2,
        )
        async with make_client(settings) as client:
            for _ in range(2):
                resp = await client.post("/v1/token", headers=_auth(make_token()))
                assert resp.status_code == 200
            with capture_logs() as cap_logs:
                resp = await client.post("/v1/token", headers=_auth(make_token()))
        assert resp.status_code == 429
        assert int(resp.headers["Retry-After"]) >= 1
        (audit,) = _audit_events(cap_logs)
        assert audit["outcome"] == "denied"

    async def test_rate_limit_is_per_subject(
        self,
        make_client: Callable[[Settings], httpx.AsyncClient],
        make_token: Callable[..., str],
    ) -> None:
        settings = Settings(
            _env_file=None,
            broker_jwks_url="https://broker.test/jwks",
            broker_issuer="https://broker.test",
            rate_limit_max_mints=1,
        )
        async with make_client(settings) as client:
            first = await client.post(
                "/v1/token", headers=_auth(make_token(sub="subject-a"))
            )
            other = await client.post(
                "/v1/token",
                headers=_auth(make_token(sub="subject-b", unixname="other")),
            )
        assert first.status_code == 200
        assert other.status_code == 200
