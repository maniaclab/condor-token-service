"""End-to-end test against a real deployment — never faked.

Requires a real HTCondor pool (a node holding the pool password with
condor_token_create available), a real af-mcp-broker minting AF Broker
Identity Tokens, and a deployed condor-token-service wired between them.
None of that can be faked without defeating the point of an e2e test, so
this module is skipped unless explicitly opted into:

    CONDOR_E2E=1 \\
    CONDOR_TOKEN_SERVICE_URL=https://condor-token.af.uchicago.edu \\
    AF_BROKER_IDENTITY_TOKEN=<freshly-minted broker token> \\
    pixi run test

The token must be freshly minted by the broker (they are short-lived) and
carry a unixname claim for a real pool user.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CONDOR_E2E") != "1",
    reason="requires a real HTCondor pool and broker; set CONDOR_E2E=1 to run",
)


async def test_mint_idtoken_against_real_service() -> None:
    base_url = os.environ["CONDOR_TOKEN_SERVICE_URL"]
    broker_token = os.environ["AF_BROKER_IDENTITY_TOKEN"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/v1/token",
            headers={"Authorization": f"Bearer {broker_token}"},
            timeout=30.0,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # A real IDTOKEN is a JWT; sanity-check shape without decoding it.
    assert body["token"].count(".") == 2
    assert "@" in body["identity"]
    assert body["expires_at"]
