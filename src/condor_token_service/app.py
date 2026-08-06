"""FastAPI application: one minting endpoint plus health probes.

Authorization model: none beyond identity, by design. The af-mcp-broker has
already authenticated and authorized the user before minting the AF Broker
Identity Token this service verifies; a valid token with a ``unixname``
claim IS the authorization to mint an IDTOKEN for that unixname. Do not add
capability logic here based on token claims.
"""

from __future__ import annotations

import math
import shutil
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from condor_token_service.config import Settings, get_settings
from condor_token_service.identity import get_jwks, peek_sub, verify_broker_token
from condor_token_service.logging import configure_logging
from condor_token_service.minting import MintingError, mint_token
from condor_token_service.ratelimit import RateLimiter

logger = structlog.get_logger(__name__)

# ``auto_error=False`` so a missing header is audited before the 401 is raised.
_bearer_scheme = HTTPBearer(auto_error=False)

router = APIRouter()


class TokenResponse(BaseModel):
    token: str
    identity: str
    expires_at: str  # ISO8601 UTC


def _audit(
    *,
    subject: str | None,
    identity: str | None,
    jti: str | None,
    outcome: str,  # "issued" | "denied" | "error"
    request_id: str,
) -> None:
    """One structlog JSON audit line per request.

    NEVER include the minted token or the inbound bearer here — see also
    logging.TokenRedactProcessor for the backstop.
    """
    logger.info(
        "audit",
        subject=subject,
        identity=identity,
        jti=jti,
        outcome=outcome,
        request_id=request_id,
    )


@router.post("/v1/token", response_model=TokenResponse)
async def issue_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
) -> TokenResponse:
    settings: Settings = request.app.state.settings
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

    if credentials is None:
        _audit(
            subject=None,
            identity=None,
            jti=None,
            outcome="denied",
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await verify_broker_token(credentials.credentials, settings)
    except HTTPException as exc:
        # 401 (invalid token) is a denial; anything else (e.g. the JWKS
        # fetch's 502) is a platform error, not the caller's fault.
        outcome = (
            "denied" if exc.status_code == status.HTTP_401_UNAUTHORIZED else "error"
        )
        _audit(
            subject=peek_sub(credentials.credentials),
            identity=None,
            jti=None,
            outcome=outcome,
            request_id=request_id,
        )
        raise

    subject: str = claims["sub"]
    jti: str | None = claims.get("jti")

    unixname = claims.get("unixname")
    if not isinstance(unixname, str) or not unixname.strip():
        _audit(
            subject=subject,
            identity=None,
            jti=jti,
            outcome="denied",
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Token carries no unixname claim — the broker must assert a "
                "POSIX identity to mint an HTCondor IDTOKEN for."
            ),
        )
    unixname = unixname.strip()
    identity = f"{unixname}@{settings.condor_identity_domain}"

    rate_limiter: RateLimiter = request.app.state.rate_limiter
    retry_after = rate_limiter.try_acquire(subject)
    if retry_after is not None:
        _audit(
            subject=subject,
            identity=identity,
            jti=jti,
            outcome="denied",
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded; retry later.",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    try:
        minted = await mint_token(unixname, settings)
    except MintingError as exc:
        # Generic detail only — condor_token_create's stderr was logged
        # server-side by minting.py and must never reach the client.
        _audit(
            subject=subject,
            identity=identity,
            jti=jti,
            outcome="error",
            request_id=request_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Token minting failed.",
        ) from exc

    _audit(
        subject=subject,
        identity=minted.identity,
        jti=jti,
        outcome="issued",
        request_id=request_id,
    )
    return TokenResponse(
        token=minted.token,
        identity=minted.identity,
        expires_at=minted.expires_at.isoformat(),
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, str]:
    """Ready only when the minting binary is executable and the broker JWKS is fetchable."""
    settings: Settings = request.app.state.settings
    problems: list[str] = []
    if shutil.which(settings.condor_token_create_bin) is None:
        problems.append(
            f"condor_token_create binary not found or not executable: "
            f"{settings.condor_token_create_bin}"
        )
    try:
        await get_jwks(settings)
    except HTTPException:
        problems.append(f"broker JWKS endpoint unreachable: {settings.broker_jwks_url}")
    if problems:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="; ".join(problems),
        )
    return {"status": "ready"}


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; tests pass explicit Settings, production uses env."""
    if settings is None:
        settings = get_settings()
    configure_logging(settings.log_level)
    application = FastAPI(
        title="condor-token-service",
        description="HTCondor IDTOKEN issuance for the AF MCP platform",
        version="0.1.0",
    )
    application.state.settings = settings
    application.state.rate_limiter = RateLimiter(
        max_events=settings.rate_limit_max_mints,
        window_seconds=settings.rate_limit_window_seconds,
    )
    application.include_router(router)
    return application


app = create_app()
