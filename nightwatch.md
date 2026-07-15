# Nightwatch Production-Readiness Record

## Run identity

- Branch: `gnhf/nightwatch-production-readiness`
- Baseline commit: `cac0f12` (`chore: snapshot production readiness work for nightwatch`)
- Worktree: `/Users/zaki/personal-projects/polymarket-arbitrage-nightwatch`
- Time zone for this record: Eastern Time
- Started: 2026-07-14 21:47 EDT (iteration 1)

## Safety boundary

No bot, dashboard, scanner, websocket feed, live collector, or other long-running trading process was started. No exchange order was placed, cancelled, signed, constructed for submission, or simulated as submitted. No funds, balances, or allowances were modified. Secret values must never appear in this record or command output.

## Baseline and source audit

- `.lavish/production-readiness.html` is the starting audit and remains unchanged.
- `.lavish/production-readiness.md` was authored as a readable, faithful Markdown conversion preserving the report's architecture, claimed current state, all G1–G14 findings, severities, efforts, dependencies, completion criteria, roadmap, budget proposal, risks, actions, and open questions.
- The converted report explicitly labels source claims as unverified hypotheses and separates live-action recommendations from the current no-live-execution boundary.

## Credential and configuration validation

Completed at 2026-07-14 21:51 EDT using redacted checks only:

- `config.polymarket.yaml` exists, is untracked, and is explicitly ignored by `.gitignore:71`. Its permissions were tightened from `0644` to owner-only `0600` during this iteration.
- A structural YAML inspection printed field names and presence booleans only. The file contains exactly one top-level `api` section with non-empty `api_key`, `api_secret`, and `passphrase` fields. It contains no wallet private key, platform override, chain override, mode setting, Kalshi credential, or Polymarket US credential.
- The macOS Keychain item labelled `polymarket-trading-wallet` is discoverable. Its value was never printed, logged, serialized, copied, or written to disk.
- The official Python CLOB v2 SDK successfully authenticated a read-only `get_open_orders(None, True)` request against `https://clob.polymarket.com` by combining the ignored L2 credentials with the Keychain wallet value entirely in process. The response was a list with zero open orders. No create, sign, post, cancel, balance-changing, or allowance-changing method was invoked.
- This proves that the current L2 credential triplet and matching wallet can authenticate a read-only CLOB query. It does **not** prove order permissions, balances, allowances, funding, live placement, cancellation, profitability, or operational readiness.
- The application config loader does not retrieve the wallet key from Keychain, and the ignored file intentionally has no `private_key`. Consequently, current application live startup cannot use this credential arrangement without separate environment injection. That mismatch remains part of G4 and will be audited in a later implementation slice.

## Official documentation consulted

- [Polymarket CLOB Authentication](https://docs.polymarket.com/api-reference/authentication) — consulted 2026-07-14. The official documentation defines L1 wallet authentication and L2 HMAC authentication; L2 uses API key, secret, passphrase, signer address, timestamp, and request signature. It explicitly includes querying open orders and balances/allowances among L2 operations.
- [Polymarket Order Overview](https://docs.polymarket.com/trading/orders/overview) — consulted 2026-07-14. The official documentation states that order queries require L2 authentication and shows the Python client query flow. Only the read-only open-order query was exercised here.

Broader current Polymarket and Kalshi protocol research remains pending; these two sources were consulted narrowly to select and verify the credential probe.

## Findings by severity

The initial finding register is preserved in `.lavish/production-readiness.md`:

- Critical: G1 Polymarket Global Live Order; G2 Kalshi Order Placement; G3 Cross-Platform Execution Pipeline.
- High: G4 Live Mode Config & Secrets; G5 Order Safety Limits for Live; G6 Fee & Gas Model Validation; G7 Crash Recovery / Position Reconciliation.
- Medium: G8 Monitoring & Alerting; G9 Market Match Quality Validation; G10 Kalshi Orderbook Depth Limitation; G11 Manual Kill Switch.
- Low: G12 Deployment & Persistence; G13 Polymarket US Live Validation; G14 ML Training Data Pipeline.

No finding is marked resolved on the strength of the source report alone.

## Changes made

### 2026-07-14 — Iteration 1

- Converted the HTML production-readiness audit into a structured Markdown audit without changing implementation code.
- Initialized this durable record with branch/baseline identity, safety constraints, finding inventory, and explicit pending validation.

### 2026-07-14 — Iteration 2

- Tightened the ignored production credential file to owner-only permissions.
- Completed the mandatory redacted Polymarket credential/config validation and recorded its limits without changing application implementation.

## Verification evidence

### 2026-07-14 — Iteration 1

- `python3` read-only parity script comparing the HTML gap table and roadmap/risk/question structures with the Markdown: **PASS** — 14/14 G1–G14 rows; severity distribution 3 Critical, 4 High, 4 Medium, 3 Low; 24/24 roadmap tasks; 6/6 risks; 4/4 open questions.
- `git diff --check`: **PASS**.
- Redacted pattern scan of the two new documents for assigned private keys, API secrets/passphrases, or 64-hex wallet material: **PASS** (no matches).
- Implementation tests were not run because this iteration changed documentation only. No live workflow was run.

### 2026-07-14 — Iteration 2

- `git check-ignore -v config.polymarket.yaml`: **PASS** — ignored by `.gitignore:71`.
- `git ls-files --error-unmatch config.polymarket.yaml`: **PASS** — command did not find the file, proving it is untracked.
- Redacted YAML structural inspection: **PASS** — L2 triplet present; wallet key and unrelated venue credentials absent; no credential values emitted.
- `security find-generic-password -l polymarket-trading-wallet` with all output suppressed: **PASS** — label discoverable without reading or printing its value.
- `stat -f ... config.polymarket.yaml`: **PASS** after hardening — mode `-rw-------`, owner `zaki`.
- Official SDK read-only `ClobClient.get_open_orders(None, True)` probe: **PASS** — authenticated list response, zero open orders; no order or account mutation endpoint invoked.
- Initial SDK probe using the documentation's generic `get_orders()` name: **EXPECTED NON-MUTATING FAILURE** — installed Python v2 SDK exposes `get_open_orders()` instead. Method signature inspection identified the correct read-only call before the successful probe.
- `uv run python -c 'import yaml'`: **FAIL** — the repository's default uv environment does not currently contain declared dependency PyYAML. The probe used ephemeral `uv run --with pyyaml --with py-clob-client-v2`; dependency/environment reconciliation remains for later verification.
- No implementation tests were run because this iteration changed only the durable record and local permissions on an ignored credential file. No live workflow was run.

## ML data and evaluation evidence

Not evaluated yet. The source audit reports snapshot-building and collection code but no trained model, training CLI, or inference pipeline. This claim remains unverified.

## Remaining blockers

- Broader official Polymarket and Kalshi documentation research has not yet been completed or recorded.
- G1–G14 have not yet been audited against current code or current official protocol behavior.
- G4 remains open: the authenticated Keychain-backed arrangement is not integrated with the application config loader, and production startup safety has not yet been reconciled.
- The default `uv run` environment currently lacks PyYAML despite its declaration in `requirements.txt`; the canonical installed environment and dependency checks remain unresolved.
- Full static, test, configuration, offline matched-data, ML, and dependency/security verification remains outstanding.
- Live-only evidence is prohibited during this run and must never be implied.

## Commits

- Baseline: `cac0f12`.
- Nightwatch commits after baseline: none at iteration 1; the GNHF orchestrator handles commits.

## Final verdict

**NOT YET ASSESSED**

The mandatory audit conversion is complete, but technical validation has not started. This is an interim state, not a production-readiness verdict.
