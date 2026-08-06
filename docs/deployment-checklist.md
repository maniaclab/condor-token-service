# Deployment Checklist (site-agnostic)

A generalized pre-deployment checklist for operators bringing
condor-token-service to **any** HTCondor pool. It parameterizes everything
the UChicago AF spike ([pool-spike.md](pool-spike.md), the worked example
and AF-specific record) discovered the hard way. Work through it top to
bottom; every parameter you record maps to a chart value or env var.

| Parameter | Where it lands |
| --- | --- |
| `<TRUST_DOMAIN>` | `config.condorTrustDomain` / `CONDOR_TRUST_DOMAIN` |
| `<IDENTITY_DOMAIN>` | `config.condorIdentityDomain` / `CONDOR_IDENTITY_DOMAIN` |
| `<SIGNING_KEY_PATH>` | `poolPassword.hostPath` |
| `<CONDOR_NODES>` | `nodeSelector` / `affinity` (+ `tolerations`) |
| `<CALLER_NS_LABELS>` / `<CALLER_POD_LABELS>` | `networkPolicy.broker.*` / `networkPolicy.jwks.*` |
| `<JWKS_URL>` | `config.brokerJwksUrl` / `BROKER_JWKS_URL` |
| `<LIFETIME_SECONDS>` | `config.tokenLifetimeSeconds` (must be > 0) |

## 1. [ ] Discover the trust domain

On a node of the kind the pod will run on (and on the central manager, to
confirm they agree):

```bash
condor_config_val TRUST_DOMAIN
```

This is what lands in a minted token's `iss` claim — chosen by the *minting
side's* config — and the schedd rejects mismatched issuers. Note whether it
is set explicitly or hostname-derived: a derived value silently changes
inside a container whose hostname differs, and differs per node.

## 2. [ ] Discover the identity domain

```bash
condor_config_val UID_DOMAIN
```

> **⚠️ Identity domain ≠ trust domain.** These are distinct concepts and at
> some sites (including UChicago AF) they DIFFER. The identity domain is the
> `@domain` half of the `user@domain` the schedd maps to a pool user; the
> trust domain is the token's issuer. The AF spike showed a token minted
> with `-identity user@<TRUST_DOMAIN>` failing authentication outright
> (`AUTHENTICATE:1004`), while `-identity user@<IDENTITY_DOMAIN>` succeeded.
> Never assume they coincide (they do in single-host pools like
> `htcondor/mini`, which is exactly how the assumption sneaks in) — verify
> empirically in step 5.

## 3. [ ] Locate the signing key and its permissions

```bash
condor_config_val SEC_TOKEN_POOL_SIGNING_KEY_FILE
ls -l "$(dirname "$(condor_config_val SEC_TOKEN_POOL_SIGNING_KEY_FILE)")"
```

Record the path (chart default `/etc/condor/passwords.d`) and owner/mode.
Root-only `0600` is conventional and is what forces the chart's documented
`runAsUser: 0`; if your site keeps it group-readable by a dedicated gid,
run the pod as that gid instead.

## 4. [ ] Establish which nodes are condor-enabled AND hold the key

Do not assume a node class ("the login nodes") is uniform — at AF only
login01–04 of eight had a condor scheduler/client configured. On each
candidate node:

```bash
condor_config_val TRUST_DOMAIN          # errors if condor is not configured
test -s <SIGNING_KEY_PATH>/POOL && echo key-present
```

Nodes failing either test are excluded, however plausible their labels look
(AF's login05 carried `partition: login` and was not condor-enabled).

## 5. [ ] Mint from a container and verify against the production schedd

The dress rehearsal for the pod: a container with only minimal condor config
and the key mount. With apptainer (typical where docker is absent from
shared nodes):

```bash
sudo apptainer exec \
  --bind <SIGNING_KEY_DIR>:<SIGNING_KEY_DIR>:ro \
  --env _condor_TRUST_DOMAIN=<TRUST_DOMAIN> \
  docker://htcondor/mini \
  condor_token_create -identity <testuser>@<IDENTITY_DOMAIN> -lifetime 3600
```

or with docker:

```bash
docker run --rm \
  -v <SIGNING_KEY_DIR>:<SIGNING_KEY_DIR>:ro \
  -e _condor_TRUST_DOMAIN=<TRUST_DOMAIN> \
  htcondor/mini \
  condor_token_create -identity <testuser>@<IDENTITY_DOMAIN> -lifetime 3600
```

Then, as the test user with the token in `~/.condor/tokens.d/`:

```bash
_condor_SEC_CLIENT_AUTHENTICATION_METHODS=IDTOKENS \
  condor_ping -type SCHEDD WRITE -verbose
```

Pass requires `Authenticated using: IDTOKENS` and a `Remote Mapping` of
`<testuser>@<IDENTITY_DOMAIN>`. While here, repeat with
`-identity <testuser>@<TRUST_DOMAIN>` to confirm empirically which domain
your schedd maps (step 2's warning).

## 6. [ ] Node placement strategy

Check whether your condor-enabled nodes share a usable label:

```bash
kubectl get nodes --show-labels
```

- Shared (or purpose-applied) label → `nodeSelector`. Applying a purposeful
  label such as `<site>/condor-client: "true"` to exactly the verified
  nodes is the cleanest option.
- No shared label → `affinity` with an explicit
  `kubernetes.io/hostname In [<CONDOR_NODES>]` list (the chart has a
  commented example).

Record any taints on those nodes (`kubectl describe node`) and add matching
`tolerations` (AF: `dedicated=ssh:NoSchedule`).

## 7. [ ] Caller (broker) NetworkPolicy selectors

Identify the namespace and pod labels of the only client allowed to call
this service:

```bash
kubectl get pods -A -l app.kubernetes.io/component=broker --show-labels
```

Fill `networkPolicy.broker.namespaceSelector/podSelector` (ingress) and
`networkPolicy.jwks.*` (egress to the JWKS origin) with what you actually
observe, not what the caller's chart documentation claims.

## 8. [ ] Broker JWKS URL

`config.brokerJwksUrl` must point at the URL where the caller publishes the
signing keys for the identity tokens this service verifies (for the AF MCP
platform: pending
[maniaclab/af-mcp-platform#162](https://github.com/maniaclab/af-mcp-platform/issues/162)).
The NetworkPolicy egress rule from step 7 must cover this origin.

## 9. [ ] Lifetime invariant

> **⚠️ `condor_token_create` without `-lifetime` mints a token with NO
> `exp` claim — it never expires.** The service always passes `-lifetime`
> and refuses to start with `TOKEN_LIFETIME_SECONDS <= 0`; keep it that way
> at your site. Pick a lifetime matching your workflow cadence (default
> 3600 s) and record for operators that these tokens are short-lived by
> design — a workflow that outlives its token re-requests one through the
> caller, and nothing long-lived is ever written to disk.

---

Once every box is checked, transcribe the recorded parameters into your
values file and deploy. For a filled-in example of this checklist run
against a real pool, see [pool-spike.md](pool-spike.md).
