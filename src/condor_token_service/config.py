from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

# pydantic-settings matches env vars to field names case-insensitively, so the
# uppercase env var names (BROKER_JWKS_URL, ...) map to these fields without
# explicit aliases.


class Settings(BaseSettings):
    # Route handlers receive Settings via ``Depends``. FastAPI builds a request
    # model from the callable's signature, and the pydantic-settings
    # ``BaseSettings.__init__`` exposes private (``_cli_parse_args`` ...)
    # parameters that FastAPI cannot turn into fields. Overriding ``__init__``
    # with a plain ``**data`` signature keeps env loading intact while giving
    # FastAPI a clean signature to introspect.
    def __init__(self, **data: Any) -> None:
        super().__init__(**data)

    # Where the broker publishes the JWKS for its AF Broker Identity Token
    # signing keys (maniaclab/af-mcp-platform#162). The default points at a
    # broker running locally (`pixi run broker` in af-mcp-platform); production
    # deployments must set BROKER_JWKS_URL explicitly (see the Helm chart).
    broker_jwks_url: str = "http://localhost:8080/.well-known/jwks.json"

    # Required `iss` claim on inbound AF Broker Identity Tokens.
    broker_issuer: str = "https://mcp.af.uchicago.edu"

    # Required `aud` claim — this service's own identity in the protocol.
    expected_audience: str = "condor-token-service"

    # Domain half of the HTCondor identity: tokens are minted for
    # `{unixname}@{condor_identity_domain}`. This is the pool's USER/UID
    # domain (`condor_config_val UID_DOMAIN`; at AF, `af.uchicago.edu` —
    # matching the provisioner's `$USER@af.uchicago.edu`), NOT the pool's
    # TRUST_DOMAIN that lands in the token's `iss` claim. The pool spike
    # (docs/pool-spike.md) showed the schedd rejects tokens whose identity
    # uses the trust domain instead.
    condor_identity_domain: str = "af.uchicago.edu"

    # The pool's TRUST_DOMAIN, exported to condor_token_create as the
    # `_CONDOR_TRUST_DOMAIN` config override and landing in the minted
    # token's `iss` claim. REQUIRED in a container: with no HTCondor config
    # present, condor_token_create derives TRUST_DOMAIN from the local
    # hostname and mints `iss=<pod name>` tokens that the schedd (and any
    # consumer pinning the pool trust domain, e.g. condor-mcp) rejects.
    # Leave empty only when the service runs on a host whose real Condor
    # config already defines TRUST_DOMAIN (the pool-spike scenario).
    condor_trust_domain: str = ""

    # Lifetime passed to `condor_token_create -lifetime` and used to compute
    # the response's `expires_at`. Must be positive — SECURITY INVARIANT:
    # condor_token_create invoked without -lifetime mints a token with NO
    # exp claim at all (it never expires; pool-spike finding), so this
    # service always passes the flag and refuses to start with a value that
    # would make it meaningless.
    token_lifetime_seconds: int = Field(default=3600, gt=0)

    # Per-subject sliding-window rate limit: at most `rate_limit_max_mints`
    # successful mint attempts per `rate_limit_window_seconds`.
    rate_limit_max_mints: int = 30
    rate_limit_window_seconds: int = 300

    # Path to (or bare name of, resolved via PATH) the condor_token_create
    # binary. Configurable so tests can substitute a fake executable and
    # non-standard Condor installs can point elsewhere.
    condor_token_create_bin: str = "condor_token_create"

    # How long a fetched JWKS is served from the in-process cache before a
    # refresh is attempted. A failed refresh serves the stale entry instead of
    # taking token verification down with it (see identity.py).
    jwks_cache_ttl_seconds: int = 300

    log_level: str = "INFO"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance.

    Use as a FastAPI dependency (``Depends(get_settings)``) so ``.env`` is read
    once at first access rather than re-instantiated on every request.
    """
    return Settings()
