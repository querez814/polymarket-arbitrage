# Nightwatch Production Operations

This runbook covers the locked Polymarket Global/Kalshi executor. It does not
authorize a live exchange mutation. A minimum-size canary remains a separate,
explicit operator decision after offline and deployment verification.

## Safety invariants

- Each process start persists a halted state. Trading cannot resume until the
  private Kalshi lifecycle stream has completed subscribe-before-REST
  reconciliation, both accounts are flat, and an authenticated operator arms
  the runtime.
- The scarcer leg is submitted IOC first. Only its authoritative confirmed fill
  may size the hedge. Ambiguous placement, cancellation, recovery, or residual
  exposure trips the durable halt and emits an external alert.
- Every possible two-leg plan reserves two order attempts in a SQLite-backed
  rolling/daily budget before any venue read or mutation. Per-order,
  cumulative strategy/global/per-market, and position-count caps are enforced;
  both venue market IDs must be explicitly whitelisted and both venue balances
  must authoritatively cover their planned collateral immediately after recovery.
- Current pair-bound fee metadata is mandatory and expires after
  `economics_max_age_seconds`. Missing, inconsistent, stale, or unsupported fee
  data rejects the opportunity.

## Configure

Copy `config.live.yaml.example` outside version control. Keep
`cross_platform_execution_enabled: false` until the deployment and canary are
approved. Provide credentials and controls only through the documented
environment variables; use a random operator token of at least 32 characters
and an HTTPS alert webhook.

Before enabling execution, set a deliberately small positive
`risk.strategy_exposure_limits.cross_platform_arb`, whitelist both the
Polymarket condition ID and Kalshi ticker for every approved pair, enable both
cross-platform monitoring and Kalshi, and retain the kill switch. Configuration
validation rejects incomplete combinations.

## Start and supervise

Run the entrypoint under a supervisor that restarts on failure, sends SIGTERM,
captures stdout/stderr externally, restricts filesystem access to the working
directory, and alerts on restart loops. Keep both SQLite files on durable local
storage with owner-only permissions and back them up together while the process
is stopped. Never run two executor processes against the same accounts.

In live mode the dashboard binds to `127.0.0.1`. If remote operator access is
required, expose it only through an authenticated TLS reverse proxy. Do not bind
the bearer-token routes directly to a plaintext network.

## Operator controls

All calls are loopback-only by default:

```bash
curl -H "Authorization: Bearer $NIGHTWATCH_OPERATOR_TOKEN" \
  http://127.0.0.1:8888/api/operator/status

curl -X POST -H "Authorization: Bearer $NIGHTWATCH_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"operator preflight complete"}' \
  http://127.0.0.1:8888/api/operator/resume

curl -X POST -H "Authorization: Bearer $NIGHTWATCH_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"operator emergency stop"}' \
  http://127.0.0.1:8888/api/operator/panic
```

Resume is fail-closed if the external alert cannot be delivered. After any
panic, runtime error, restart, private-stream loss, or residual exposure,
inspect both venues directly, cancel/reconcile manually, confirm both accounts
are flat, preserve logs and SQLite state, and only then consider a new resume.

## Deployment and canary gate

Before the first mutation, require a pinned build, dependency/security scan,
supervisor restart test, alert delivery test, backup/restore test, log retention,
clock synchronization, and a documented rollback owner. Then approve one
minimum-size, whitelisted matched pair during staffed hours. Verify the complete
private-stream and REST lifecycle, venue fee debits, fill precision, latency,
cancellation behavior, durable journal replay, and operator alerting. Any
discrepancy ends the canary and leaves trading halted.
