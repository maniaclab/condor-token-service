# Spike: condor-token-service Against the Real AF Pool

**Status: RUN and PASSED on 2026-08-05 — see [Outcome Recording](#outcome-recording).**

## Purpose

The chart in `charts/condor-token-service/` and the service defaults encode
assumptions about the production AF pool that were originally validated only
against `htcondor/mini` (see `tests/integration_condor/`). This spike is the
pre-deployment checklist that was run against the real pool before finalizing
the chart values. The production pod is scheduled on the **condor-enabled AF
login nodes (login01–04)** — the spike found login05–08 have no condor
scheduler/client configured — so every check below runs on a login node
unless it says otherwise.

A generalized, site-agnostic version of this checklist for non-AF operators
lives in [deployment-checklist.md](deployment-checklist.md); this document is
the AF-specific record.

Context: this service is the first consumer of the AF Broker Identity Token
protocol ([maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)),
brokering HTCondor IDTOKENS for MCP-driven job workflows
([maniaclab/af-mcp-platform#169](https://github.com/maniaclab/af-mcp-platform/issues/169)).

The central risk this spike retires: `condor_token_create` derives the
token's `iss` claim from the **local** condor config's `TRUST_DOMAIN`, and
the schedd rejects mismatched issuers. The production pod therefore needs
minimal condor config aligned with the pool — not just the key file. The
spike surfaced a second, subtler domain: the **identity** minted must use the
pool's user/UID domain, which at AF is *not* the trust domain (see check 3).

---

## Acceptance Checklist

The spike passes if and only if **all** of the following are confirmed:

### 1. [x] Trust domain AND identity domain discovered

Run on a login node AND on head01, and confirm they agree:

```bash
condor_config_val TRUST_DOMAIN
```

A sample token showed `iss=head01.af.uchicago.edu` — confirm that is the
value on the login nodes too, record it, and note whether it is set
explicitly in condor config or derived from the hostname (a derived value
would silently change inside a container whose hostname differs — and would
differ per login node, which the schedd would reject).

Discover the **identity domain** too — the domain half of the `user@domain`
identities the pool actually maps:

```bash
condor_config_val UID_DOMAIN
```

then verify empirically (as done in checks 3–4): mint one token with the
identity domain and one with the trust domain, and confirm which one the
schedd accepts. The two domains are distinct concepts and at AF they differ.

**Recorded TRUST_DOMAIN:** `head01.af.uchicago.edu` — identical on head01
and login01–04 (checked via `condor_config_val TRUST_DOMAIN` on each).
Whether explicit or hostname-derived was not determined (residual).
**Recorded identity domain:** `af.uchicago.edu` (matches the AF
provisioner's `$USER@af.uchicago.edu`), verified empirically in checks 3–4.

### 2. [x] Signing key present on the login nodes, path and permissions

**This is the check most likely to fail.** The provisioner that runs
`condor_token_create` today may live on head01 or a central provisioning
host — the pool signing key is not guaranteed to exist on the login nodes at
all. On each login node (or a representative sample plus config-management
inspection):

```bash
condor_config_val SEC_TOKEN_POOL_SIGNING_KEY_FILE
ls -l /etc/condor/passwords.d/
```

Confirm the key the schedd validates against is present at
`/etc/condor/passwords.d/POOL` (the chart's `poolPassword.hostPath`
assumption), and record owner/mode (root-only `0600` expected — this is what
justifies the chart's documented `runAsUser: 0`).

If the key is absent from the login nodes, decide before deployment:
distribute it there via config management, or constrain scheduling to
whichever nodes hold it. Do not copy it ad hoc.

**Recorded path / owner / mode:** `/etc/condor/passwords.d/POOL` on head01
and login01–04, `root:root 0600` (plus a `passwd-adstash` sibling in the
same directory). login05–08 do NOT have condor scheduler/client configured
(and were not verified to hold the key) — deployment targets login01–04
only.

### 3. [x] condor_token_create works with our exact flags

```bash
condor_token_create -identity testuser@<IDENTITY_DOMAIN> -lifetime 3600
```

`<IDENTITY_DOMAIN>` is the pool's **user/UID domain from check 1, NOT the
trust domain**. The distinction: `TRUST_DOMAIN` is what lands in the token's
`iss` claim (chosen by the *minting side's* condor config); the identity
domain is the `@domain` half of the `sub` the schedd maps to a pool user.
The spike proved the schedd rejects tokens whose identity uses the trust
domain (see Findings).

Confirm it succeeds, whether it requires root (try once as an unprivileged
user and record the failure mode), and that the decoded token's `iss`, `sub`,
and `exp - iat` match the flags.

**Requires root:** succeeded via `sudo` on head01 and login01–04 (with
`-lifetime 20`); the unprivileged failure mode was not recorded (residual).

### 4. [x] Mint from a container, verify against the production schedd

The dress rehearsal for the pod: on a login node, run a container with
**only** minimal condor config (`TRUST_DOMAIN = <recorded value>`) and the
`passwords.d` mount. The AF login nodes have no docker — **apptainer** was
used and worked:

```bash
sudo apptainer exec \
  --bind /etc/condor/passwords.d:/etc/condor/passwords.d:ro \
  --env _condor_TRUST_DOMAIN=head01.af.uchicago.edu \
  docker://htcondor/mini \
  condor_token_create -identity testuser@af.uchicago.edu -lifetime 3600
```

(docker equivalent, for hosts that have it:)

```bash
docker run --rm \
  -v /etc/condor/passwords.d:/etc/condor/passwords.d:ro \
  -e _condor_TRUST_DOMAIN=<TRUST_DOMAIN> \
  <condor-token-service image or htcondor/mini> \
  condor_token_create -identity <testuser>@<IDENTITY_DOMAIN> -lifetime 3600
```

Then, as a real test user with that token placed in `~/.condor/tokens.d/`:

```bash
_condor_SEC_CLIENT_AUTHENTICATION_METHODS=IDTOKENS \
  condor_ping -type SCHEDD WRITE -verbose
```

Pass requires the negotiated method to be IDTOKENS and the mapped identity
to be `<testuser>@<IDENTITY_DOMAIN>`.

**Recorded:** `Authenticated using: IDTOKENS`, `Remote Mapping:
testuser@af.uchicago.edu`, `Authorized: TRUE`. Remote schedd HTCondor
25.0.12, local client 25.0.8.

### 5. [x] Chart assumptions verified

- [x] Node placement — login01–04 share **no usable common label** (login04
      alone has `login: "true"`; login01–03 have only hostname/NFD labels;
      login05 has `partition: login` but is NOT condor-enabled — a trap).
      All of login01–04 carry the taint `dedicated=ssh:NoSchedule`. The
      chart therefore uses nodeAffinity with an explicit hostname list (or
      a purposeful label applied to the four nodes) plus the matching
      toleration.
- [x] The broker's namespace and pod labels — broker pods run in namespace
      `mcp` with labels `app.kubernetes.io/name: af-mcp-platform`,
      `app.kubernetes.io/instance: af-mcp-platform`,
      `app.kubernetes.io/component: broker` (chart defaults updated).
- [ ] The broker JWKS URL once
      [maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)
      lands — the chart's `config.brokerJwksUrl` default is still a
      placeholder.

**Recorded nodeSelector / labels / JWKS URL:** see above; JWKS URL pending
#162.

### 6. [x] Token-lifetime posture recorded

Confirm and record for operators: tokens minted by this service are
**short-lived by design** (`TOKEN_LIFETIME_SECONDS`, default 3600) — unlike
the AF provisioner's non-expiring tokens placed in users' home directories.
An MCP-driven workflow that outlives its token re-requests one through the
broker; nothing long-lived is ever written to disk by this service.

**Recorded:** confirmed, with a sharper edge than expected — see the
lifetime finding below: omitting `-lifetime` mints a token with **no `exp`
claim at all**.

---

## Outcome Recording

### Result: [x] Pass / [ ] Fail

**Date tested:** 2026-08-05
**Pool / head node:** UChicago AF (head01)
**HTCondor version:** remote schedd 25.0.12, local client 25.0.8
**Tester:** Giordon Stark

| # | Check | Result | Notes |
| --- | --- | --- | --- |
| 1 | TRUST_DOMAIN / identity domain discovered | Pass | `head01.af.uchicago.edu`, identical on head01 and login01–04; identity domain `af.uchicago.edu` |
| 2 | Signing key path/permissions | Pass | `/etc/condor/passwords.d/POOL`, `root:root 0600`, on head01 + login01–04; login05–08 not condor-enabled |
| 3 | condor_token_create flags | Pass | via sudo on head01 and login01–04 (`-lifetime 20`) |
| 4 | Container mint vs production schedd | Pass | apptainer (no docker on login nodes); IDTOKENS, mapped `testuser@af.uchicago.edu`, Authorized TRUE |
| 5 | Chart assumptions | Pass | broker ns `mcp` + labels recorded; no shared node label → hostname affinity; JWKS URL still pending #162 |
| 6 | Lifetime posture recorded | Pass | no `-lifetime` ⇒ no `exp` claim at all |

**Findings:**

1. **CRITICAL — identity domain ≠ trust domain.** Minting
   `-identity testuser@head01.af.uchicago.edu` (the trust domain) produced a
   token that FAILED authentication entirely
   (`AUTHENTICATE:1004:Failed to authenticate using IDTOKENS`);
   `-identity testuser@af.uchicago.edu` succeeded. The identity's domain is
   the pool's user/UID domain (matching the AF provisioner's
   `$USER@af.uchicago.edu`), NOT the trust domain that lands in `iss`.
   Absorbed into: `CONDOR_IDENTITY_DOMAIN` default and docs (config.py,
   README, chart values).
2. **CRITICAL — lifetime.** Without `-lifetime`, the minted token has NO
   `exp` claim at all — it never expires. Always passing `-lifetime` is a
   hard security invariant for this service, not a default. Absorbed into:
   Settings rejects `TOKEN_LIFETIME_SECONDS <= 0` (pydantic `gt=0`) and a
   test asserts the minting argv always carries `-lifetime`.
3. Container minting works with apptainer on the login nodes (no docker
   there); minimal config (`_condor_TRUST_DOMAIN` env + read-only
   `passwords.d` bind) is sufficient.
4. Broker NetworkPolicy reality: namespace `mcp`, labels
   `app.kubernetes.io/name: af-mcp-platform`,
   `app.kubernetes.io/instance: af-mcp-platform`,
   `app.kubernetes.io/component: broker`. Chart defaults updated.
5. Node placement reality: no usable shared label on login01–04; taint
   `dedicated=ssh:NoSchedule` on all four; login05 (`partition: login`) is
   not condor-enabled. Chart gains an `affinity` passthrough with a
   hostname-list example.

**Residuals (not blockers, record before/at deployment):**

- Whether TRUST_DOMAIN is set explicitly in condor config or
  hostname-derived on head01 was not determined.
- The unprivileged `condor_token_create` failure mode was not recorded
  (everything was run via sudo).
- login05–08 were not verified to hold the signing key (they are excluded
  from deployment regardless, as non-condor-enabled).
- Broker JWKS URL pending
  [maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162).

**Decision:**

Pass → `charts/condor-token-service/values.yaml` finalized with the recorded
values (NetworkPolicy selectors, affinity/tolerations examples,
login01–04-only placement); service hardened per finding 2. Deployment
proceeds once the broker JWKS URL exists (#162).
