# Nightwatch Production Operations

This runbook covers the locked Polymarket Global/Kalshi executor. It does not
authorize a live exchange mutation. A minimum-size canary remains a separate,
explicit operator decision after offline and deployment verification.

## Paper release proof

The paper configuration starts with $5,000, reads public venue data, persists
every evaluated direction, and cannot submit exchange orders. Before an
all-shift paper observation, run the deterministic lifecycle proof:

```bash
python scripts/prove_paper_acceptance.py --db data/paper_acceptance.db
```

Then run `scripts/evaluate_live_pair.py` for a manually reviewed equivalent
pair. The first invocation prints the canonical pair and approval hash; the
second invocation must finish with either `after_cost_edge` or
`no_after_cost_edge`, authoritative economics, and `venue_mutations: 0`.

Start the continuous paper runtime only after both proofs pass:

```bash
python run_with_dashboard.py \
  --config config.paper.production.yaml --port 8888 --dry-run
```

The dashboard is at `http://127.0.0.1:8888`. A zero-trade run is valid only
when it still records direction evidence and exact rejection reasons. Generate
the final or in-progress shift report directly from SQLite:

```bash
python scripts/report_paper_run.py --db data/paper_performance.db
```

The reporter opens SQLite read-only and does not acquire the runtime's writer
lock. `evaluated_no_trade` means current pairs were fully evaluated but did not
clear costs and controls. `legacy_run_not_auditable` means the selected run
predates direction-level evidence and must not be used to infer missed profit.

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

The committed Linux deployment definition is
`deploy/systemd/nightwatch.service`. It runs the non-network production
preflight before startup, verifies the hash-locked release environment,
restricts writes to the durable state/log directories, waits for loopback
liveness, and restarts only on failure. `/health/live` reports process liveness.
`/health/ready` reports
critical matcher/scanner availability. In monitoring-only mode it can return 200
without a production runtime; in canary mode it also requires trading admission
and returns 503 while operator-halted. A halted but live process must not be
restarted automatically.

The production preflight must run as the `nightwatch` service user so ownership
checks reflect the runtime identity. Treat systemd's `ExecStartPre` output as
the canonical evidence; do not run the command manually as root. With execution
disabled, start the service and capture the gate result:

```bash
systemctl start nightwatch
journalctl -u nightwatch --since "5 minutes ago" --no-pager
```

## Operator controls

All calls are loopback-only by default. Use the helper, which reads
`NIGHTWATCH_OPERATOR_TOKEN` from the environment and never places it in process
arguments:

```bash
python scripts/operator_control.py status
python scripts/operator_control.py resume --reason "operator preflight complete"
python scripts/operator_control.py panic --reason "operator emergency stop"
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

The exact staffed procedure and evidence bundle are defined in
`docs/canary-protocol.md`. Running its preflight is non-mutating; entering the
execution window still requires separate human authorization.
