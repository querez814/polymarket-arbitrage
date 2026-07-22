# Nightwatch Minimum-Size Canary Protocol

This protocol is an authorization checklist, not authorization. Do not enable
cross-platform execution or mutate either venue until the named operator and
incident responder approve a staffed window.

## Required evidence before the window

- Deploy one reviewed Git commit with Python 3.12.13 and `requirements.lock`.
- `systemd-analyze verify` passes for `nightwatch.service`.
- The deployment preflight passes with execution disabled.
- SIGTERM/restart, state backup/restore, log retention, clock synchronization,
  TLS-proxied operator access, and real alert delivery have been drilled.
- Both accounts have sufficient collateral and no untracked orders/positions.
- One equivalent pair has been manually reviewed for wording, resolution
  source, close time, outcome direction, settlement rules, and market status.

## Freeze one-pair scope

Copy the deployed config to a canary-specific ignored file. Set exactly:

- `cross_platform_execution_enabled: true`;
- `risk.whitelist` to the Polymarket condition ID and Kalshi ticker only;
- the smallest venue-valid `cross_platform_max_order_size`;
- `max_order_notional` exactly to the approved per-order dollar notional;
- `max_position_per_market` exactly to the approved contract count, and
  `max_global_exposure` plus `cross_platform_arb` exposure exactly to twice that
  contract count so only the two-leg lifecycle is admitted;
- per-minute and daily attempts to `2`.

Make the finished canary config `root:nightwatch` mode `0640`. The service user
must be able to read it but cannot be allowed to modify it between preflight and
runtime load.

Record the two independently approved caps as numbers and install the reviewed
canary drop-in with those same values. The service's `ExecStartPre` runs the
non-mutating gate as `nightwatch` and binds it to the same root-owned config path
used by `ExecStart`. Do not invoke this ownership-sensitive command manually as
root. Capture its JSON from the journal. The effective command is:

```bash
/opt/nightwatch/current/.venv/bin/python scripts/production_preflight.py \
  --config /etc/nightwatch/config.canary.yaml \
  --config-owner-uid 0 \
  --phase canary \
  --working-directory /opt/nightwatch/current \
  --state-directory /var/lib/nightwatch \
  --secret-file /etc/nightwatch/nightwatch.env \
  --secret-file-owner-uid 0 \
  --canary-max-contracts APPROVED_CONTRACT_CAP \
  --canary-max-order-notional APPROVED_ORDER_NOTIONAL_CAP
```

Any failed check is a no-go. The output contains check names only and must be
attached to the canary record.

## Staffed execution window

1. Copy `deploy/systemd/nightwatch-canary.conf.example` to the service drop-in,
   set the exact canary config and the same two approved caps, daemon-reload, and
   record `systemctl cat nightwatch` plus the config checksum. Start the service.
   Confirm `/health/live` is 200 and `/health/ready` is 503.
2. Confirm the external `startup_halted` alert arrived with the expected route.
3. Read both venues directly: trading active, approved market active, no
   untracked open orders, journal-matching positions, and sufficient collateral.
4. Confirm current fee metadata, order-book timestamps, and both top-of-book
   quantities support the configured minimum size.
5. Record approver, incident responder, commit SHA, config checksum, pair IDs,
   UTC start/end, and rollback release.
6. Authenticate locally through the TLS operator path and resume once. Readiness
   must become 200 only after recovery, private-stream reconciliation, and alert
   delivery succeed.
7. Permit at most one opportunity. Do not manually retry a placement. The
   durable two-attempt budget prevents another two-leg plan.
8. Immediately panic after the plan reaches a terminal phase, even when both
   legs complete.
9. Stop the service, remove the canary drop-in, daemon-reload, and restore the
   execution-disabled deployment before any later start.

## Evidence to capture

- Preflight JSON and immutable release SHA.
- Redacted operator status before resume, after resume, and after final panic.
- Both venue order IDs/client IDs, requested and confirmed fill quantities,
  fee debits, timestamps, status transitions, and private-stream event IDs.
- Journal/operator-state backup and integrity result.
- Gross edge, modeled costs, actual costs, residual exposure, and timing from
  detection through both authoritative terminal reads.
- Journal evidence for clean completion; external alert receipts for startup,
  resume, panic, and any residual or failure that occurred.

Never record credentials, signatures, wallet keys, bearer tokens, or raw account
balances in the evidence bundle.

## Automatic no-go and abort conditions

Abort and leave the durable halt set for any identity mismatch, stale book or
economics snapshot, insufficient collateral, private-stream reconnect, alert
failure, ambiguous mutation, resting IOC, fee/precision discrepancy, partial
hedge, unexpected position, readiness regression, or unrecognized venue state.
Resolve the accounts manually and open a new reviewed canary window; never
continue the same window after an ambiguity.

## Promotion boundary

One clean canary proves only one minimum-size lifecycle. Promotion requires a
reviewed evidence bundle, a second restart/recovery check against the resulting
venue state, and explicit operator approval. It does not prove profitability,
capacity, or unattended operation.
