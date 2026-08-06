# condor-token-service

HTCondor IDTOKEN issuance for the UChicago ATLAS Analysis Facility MCP
platform. A deliberately tiny, auditable service: one minting endpoint,
verified against one credential type, shelling out to one binary.

## Why this service exists

HTCondor IDTOKENS are signed with the **pool password**
(`/etc/condor/passwords.d/POOL`) — a **symmetric** key. Anyone holding it can
mint a token for *any* identity in the pool, so it must never leave Condor
infrastructure. In particular it must never reach the
[af-mcp-broker](https://github.com/maniaclab/af-mcp-platform), which lives in
a different trust domain and holds many other credentials.

Instead, this service runs as pods scheduled (affinity/tolerations) across
the condor-enabled AF login nodes (login01–04 — the pool spike found
login05–08 have no condor scheduler/client configured) where that key
already lives, hostPath-mounted read-only. The broker asks it to mint; the
key stays put.

```
 LLM client                af-mcp-platform                 Condor head node
     |                          |                                |
     |  MCP tool call           |                                |
     +------------------------->|                                |
     |                 [broker authenticates &                   |
     |                  authorizes the user]                     |
     |                          |  POST /v1/token                |
     |                          |  Bearer: AF Broker             |
     |                          |  Identity Token (RS256)        |
     |                          +------------------------------->|
     |                          |                     condor-token-service
     |                          |                                |
     |                          |                    verify JWT (broker JWKS)
     |                          |                    require unixname claim
     |                          |                                |
     |                          |                    condor_token_create
     |                          |                      -identity user@domain
     |                          |                      -lifetime 3600
     |                          |                                |
     |                          |                    [signs with POOL key,
     |                          |                     which never leaves
     |                          |                     this node]
     |                          |<-------------------------------+
     |                          |  {token, identity, expires_at} |
```

## The credential it verifies

This is the first consumer of the **AF Broker Identity Token** internal
protocol ([maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)):
a short-lived RS256 JWT minted by the broker with claims

- `iss` = the broker's issuer (`BROKER_ISSUER`)
- `sub` = the user's subject
- `aud` = `condor-token-service` (`EXPECTED_AUDIENCE`)
- `exp` / `iat` / `jti`
- `unixname` / `uid` / `gid` — identity assertions only

These are **identity assertions, not capability claims**. The broker has
already authorized the call before minting the token; this service derives no
authorization from token claims beyond identity, and must never grow logic
that does. A token without a non-empty `unixname` is refused (403).

Verification fetches the broker's JWKS from `BROKER_JWKS_URL` (TTL-cached,
single-flight refresh, stale-served on fetch failure), then enforces
signature, issuer, audience, and expiry. Every request produces exactly one
JSON audit line — subject, minted identity, broker-token `jti`, outcome
(`issued|denied|error`), request id — and neither the minted token nor the
inbound bearer is ever logged.

## API

| Endpoint | Auth | Behavior |
| --- | --- | --- |
| `POST /v1/token` | `Authorization: Bearer <AF Broker Identity Token>` | Mints an IDTOKEN for `{unixname}@{CONDOR_IDENTITY_DOMAIN}` via `condor_token_create`. Returns `{"token", "identity", "expires_at"}`. 401 invalid token, 403 missing unixname, 429 (+`Retry-After`) over the per-subject rate limit (default 30 mints / 300 s), 502 on minting failure (generic detail; stderr is logged server-side only). |
| `GET /healthz` | none | Always 200. |
| `GET /readyz` | none | 200 only when `condor_token_create` is executable and the broker JWKS is fetchable; 503 otherwise. |

Configuration is env-driven (`src/condor_token_service/config.py`):
`BROKER_JWKS_URL`, `BROKER_ISSUER`, `EXPECTED_AUDIENCE`,
`CONDOR_IDENTITY_DOMAIN`, `TOKEN_LIFETIME_SECONDS`, `RATE_LIMIT_MAX_MINTS`,
`RATE_LIMIT_WINDOW_SECONDS`, `CONDOR_TOKEN_CREATE_BIN`, `LOG_LEVEL`.

## Deployment

The Helm chart at `charts/condor-token-service/` encodes the security model:

- **Node constraint** — values-driven `nodeSelector`/`affinity`/
  `tolerations` schedule the pods across the condor-enabled login nodes
  holding the pool password (login01–04 at AF; they share no usable label,
  so the chart ships a hostname-list nodeAffinity example and notes the
  cleaner purposeful-label alternative; two replicas for HA).
- **hostPath** — `/etc/condor/passwords.d` mounted read-only at the same
  path, so `condor_token_create` works unconfigured.
- **Locked-down pod** — read-only root filesystem, all capabilities dropped,
  `RuntimeDefault` seccomp, no ServiceAccount token. `runAsUser: 0` is the
  one documented concession: the pool key is conventionally `0600 root:root`.
- **NetworkPolicy** — ingress only from the broker pods; egress only DNS and
  the broker JWKS origin.
- **No ConfigMap** — all configuration is env-from-values.

**Trust-domain gotcha**: `condor_token_create` derives the token's `iss`
claim from the *local* condor config's `TRUST_DOMAIN`, and the schedd rejects
tokens whose issuer does not match the pool's trust domain. Mounting the pool
password alone is therefore not enough — the pod needs minimal condor config
aligned with the pool (at least `TRUST_DOMAIN`, matching
`condor_config_val TRUST_DOMAIN` on the head node).

**Identity-domain gotcha**: `CONDOR_IDENTITY_DOMAIN` is the pool's
**user/UID domain** (`condor_config_val UID_DOMAIN`; at AF
`af.uchicago.edu`, matching the provisioner's `$USER@af.uchicago.edu`) —
**not** the trust domain above. The pool spike proved the schedd rejects
tokens minted for `user@<TRUST_DOMAIN>` outright (`AUTHENTICATE:1004`) while
`user@<UID_DOMAIN>` authenticates and maps correctly. The two domains
coincide in single-host pools (like the `htcondor/mini` test image), which
is exactly how the wrong assumption sneaks in.

`docs/pool-spike.md` is the AF-specific record of verifying all of this
against the real pool; `docs/deployment-checklist.md` is the generalized,
site-agnostic checklist for other operators.

```bash
helm lint charts/condor-token-service
helm template condor-token-service charts/condor-token-service
```

The `Containerfile` builds the runtime image: debian-slim with the HTCondor
apt repository (for `condor_token_create`) plus the pixi-built Python
environment.

## Local development

Everything runs through [pixi](https://pixi.sh); dependencies live in
`pixi.toml` (this package's `pyproject.toml` intentionally declares no
dependencies).

```bash
pixi run serve        # dev server with reload → http://localhost:8080/docs
pixi run test         # pytest tests/ -v
pixi run lint         # ruff check + format --check
pixi run fmt          # ruff format + autofix
pixi run typecheck    # mypy src
pixi run -e dev lint-all   # everything the CI lint job runs (ruff + mypy + pre-commit)
```

The default test suite never touches the network or a real Condor pool: the
JWKS is served by an in-process stub around a real generated RSA keypair, and
`condor_token_create` is a fake executable script on `PATH`. Two opt-in
layers sit above it:

- `CONDOR_MINI_INTEGRATION=1 pixi run -e dev pytest tests/integration_condor/`
  spins up a real [htcondor/mini](https://hub.docker.com/r/htcondor/mini)
  pool in docker and proves that tokens minted with our exact flags carry
  the expected claims and authenticate against a real schedd (via
  `condor_ping` forced to IDTOKENS) as the right identity. CI runs this in
  the `integration-condor` job.
- `tests/test_e2e.py` is skipped unless `CONDOR_E2E=1`, and requires a real
  pool, a real broker-minted token, and a deployed service — it is never
  faked.
