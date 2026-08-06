"""Integration tests for GET /healthz and GET /readyz."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from condor_token_service.config import Settings

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from tests.conftest import JwksFetchStub


class TestHealthz:
    async def test_healthz_is_200_unconditionally(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestReadyz:
    @pytest.mark.usefixtures("fake_condor_bin")
    async def test_ready_when_binary_present_and_jwks_fetchable(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}

    async def test_503_when_binary_missing(
        self, make_client: Callable[[Settings], httpx.AsyncClient]
    ) -> None:
        settings = Settings(
            _env_file=None,
            broker_jwks_url="https://broker.test/jwks",
            broker_issuer="https://broker.test",
            condor_token_create_bin="/nonexistent/condor_token_create",
        )
        async with make_client(settings) as client:
            resp = await client.get("/readyz")
        assert resp.status_code == 503
        assert "condor_token_create" in resp.json()["detail"]

    @pytest.mark.usefixtures("fake_condor_bin")
    async def test_503_when_jwks_unreachable(
        self, client: httpx.AsyncClient, stub_jwks_fetch: JwksFetchStub
    ) -> None:
        stub_jwks_fetch.fail = True
        resp = await client.get("/readyz")
        assert resp.status_code == 503
        assert "JWKS" in resp.json()["detail"]
