"""HTCondor IDTOKEN minting via the ``condor_token_create`` CLI.

The IDTOKEN signing key is the pool password — a symmetric secret hostPath-
mounted read-only into this pod and readable only here. Minting therefore
shells out to Condor's own tooling on this node rather than ever loading the
key into Python.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from condor_token_service.config import Settings

logger = structlog.get_logger(__name__)


class MintingError(Exception):
    """Raised when condor_token_create fails.

    The message is deliberately generic: stderr from the binary is logged
    server-side (it can reference the pool password path or Condor config)
    and must never reach the client.
    """


@dataclass(frozen=True)
class MintedToken:
    token: str
    identity: str
    expires_at: datetime


async def mint_token(unixname: str, settings: Settings) -> MintedToken:
    """Mint an IDTOKEN for ``{unixname}@{condor_identity_domain}``.

    Runs ``condor_token_create -identity <identity> -lifetime <seconds>`` and
    captures stdout as the token. ``expires_at`` is computed from the
    configured lifetime — condor_token_create embeds the same lifetime in the
    token it signs.
    """
    identity = f"{unixname}@{settings.condor_identity_domain}"
    argv = [
        settings.condor_token_create_bin,
        "-identity",
        identity,
        "-lifetime",
        str(settings.token_lifetime_seconds),
    ]
    # _CONDOR_<PARAM> environment overrides beat any config file, so this
    # pins the minted token's `iss` (= TRUST_DOMAIN) regardless of whether
    # the surrounding environment has an HTCondor config at all -- see the
    # condor_trust_domain comment in config.py. env=None inherits untouched.
    env: dict[str, str] | None = None
    if settings.condor_trust_domain:
        env = {**os.environ, "_CONDOR_TRUST_DOMAIN": settings.condor_trust_domain}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except OSError as exc:
        # Binary missing or not executable — surfaced by readyz, but a race
        # (node reconfiguration) can still land here.
        logger.exception(
            "condor_token_create_spawn_failed",
            binary=settings.condor_token_create_bin,
            error=str(exc),
        )
        raise MintingError("failed to invoke condor_token_create") from exc

    if proc.returncode != 0:
        logger.error(
            "condor_token_create_failed",
            returncode=proc.returncode,
            stderr=stderr.decode(errors="replace").strip(),
            identity=identity,
        )
        raise MintingError(f"condor_token_create exited {proc.returncode}")

    token = stdout.decode().strip()
    if not token:
        logger.error("condor_token_create_empty_stdout", identity=identity)
        raise MintingError("condor_token_create produced no token")

    expires_at = datetime.now(UTC) + timedelta(seconds=settings.token_lifetime_seconds)
    return MintedToken(token=token, identity=identity, expires_at=expires_at)
