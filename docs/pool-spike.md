# Spike: condor-token-service Against the Real AF Pool

## Purpose

The chart in `charts/condor-token-service/` and the service defaults encode
assumptions about the production AF pool that have only been validated
against `htcondor/mini` (see `tests/integration_condor/`). This spike is the
pre-deployment checklist to run against the real pool before finalizing the
chart values. The production pod is scheduled on the AF **login nodes
(login01–08)** — not the head node — so every check below runs on a login
node unless it says otherwise.

Context: this service is the first consumer of the AF Broker Identity Token
protocol ([maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)),
brokering HTCondor IDTOKENS for MCP-driven job workflows
([maniaclab/af-mcp-platform#169](https://github.com/maniaclab/af-mcp-platform/issues/169)).

The central risk this spike retires: `condor_token_create` derives the
token's `iss` claim from the **local** condor config's `TRUST_DOMAIN`, and
the schedd rejects mismatched issuers. The production pod therefore needs
minimal condor config aligned with the pool — not just the key file.

---

## Acceptance Checklist

The spike passes if and only if **all** of the following are confirmed:

### 1. [ ] Trust domain discovered

Run on a login node AND on head01, and confirm they agree:

```bash
condor_config_val TRUST_DOMAIN
```

A sample token showed `iss=head01.af.uchicago.edu` — confirm that is the
value on the login nodes too, record it, and note whether it is set
explicitly in condor config or derived from the hostname (a derived value
would silently change inside a container whose hostname differs — and would
differ per login node, which the schedd would reject).

**Recorded TRUST_DOMAIN:** ______________________

### 2. [ ] Signing key present on the login nodes, path and permissions

**This is the check most likely to fail.** The provisioner that runs
`condor_token_create` today may live on head01 or a central provisioning
host — the pool signing key is not guaranteed to exist on login01–08 at
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
distribute it there via config management, or constrain `nodeSelector` to
whichever nodes hold it. Do not copy it ad hoc.

**Recorded path / owner / mode:** ______________________

### 3. [ ] condor_token_create works with our exact flags

```bash
condor_token_create -identity testuser@<TRUST_DOMAIN> -lifetime 3600
```

Confirm it succeeds, whether it requires root (try once as an unprivileged
user and record the failure mode), and that the decoded token's `iss`, `sub`,
and `exp - iat` match the flags.

**Requires root:** [ ] yes / [ ] no

### 4. [ ] Mint from a container, verify against the production schedd

The dress rehearsal for the pod: on a login node, run a container with **only**
minimal condor config (`TRUST_DOMAIN = <recorded value>`) and the
`passwords.d` mount:

```bash
docker run --rm \
  -v /etc/condor/passwords.d:/etc/condor/passwords.d:ro \
  -e _condor_TRUST_DOMAIN=<TRUST_DOMAIN> \
  <condor-token-service image or htcondor/mini> \
  condor_token_create -identity <testuser>@<TRUST_DOMAIN> -lifetime 3600
```

Then, as a real test user with that token placed in `~/.condor/tokens.d/`:

```bash
_condor_SEC_CLIENT_AUTHENTICATION_METHODS=IDTOKENS \
  condor_ping -type SCHEDD WRITE
```

Pass requires the negotiated method to be IDTOKENS and the mapped identity
to be `<testuser>@<TRUST_DOMAIN>`.

### 5. [ ] Chart assumptions verified

- [ ] The Kubernetes label shared by login01–08 (the `nodeSelector`
      value) — the pod schedules across the login nodes, so a shared
      role label beats a hostname pin, and `replicaCount: 2` gives the
      mint path HA across them.
- [ ] The broker's namespace and pod labels — the chart's
      `networkPolicy.broker`/`networkPolicy.jwks` selector defaults
      (`kubernetes.io/metadata.name: af-mcp`,
      `app.kubernetes.io/component: broker`).
- [ ] The broker JWKS URL once
      [maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)
      lands — the chart's `config.brokerJwksUrl` default is a placeholder.

**Recorded nodeSelector / labels / JWKS URL:** ______________________

### 6. [ ] Token-lifetime posture recorded

Confirm and record for operators: tokens minted by this service are
**short-lived by design** (`TOKEN_LIFETIME_SECONDS`, default 3600) — unlike
the AF provisioner's non-expiring tokens placed in users' home directories.
An MCP-driven workflow that outlives its token re-requests one through the
broker; nothing long-lived is ever written to disk by this service.

---

## Outcome Recording

Update this section after the spike is run.

### Result: [ ] Pass / [ ] Fail

**Date tested:**
**Pool / head node:**
**HTCondor version:**
**Tester:**

| # | Check | Result | Notes |
| --- | --- | --- | --- |
| 1 | TRUST_DOMAIN discovered | | |
| 2 | Signing key path/permissions | | |
| 3 | condor_token_create flags | | |
| 4 | Container mint vs production schedd | | |
| 5 | Chart assumptions | | |
| 6 | Lifetime posture recorded | | |

**Findings:**

**Decision:**
- Pass → finalize `charts/condor-token-service/values.yaml` with the
  recorded values and proceed to deployment.
- Fail → record which assumption broke and fix the chart/service (or the
  pool config) before any deployment; do not work around a mismatch with
  ad-hoc pod config.
