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

### Polymarket CLOB protocol research — 2026-07-14

The following current official primary sources were reviewed at 2026-07-14 21:53 EDT. No trading endpoint was invoked during this research.

- [Authentication](https://docs.polymarket.com/api-reference/authentication) — public market-data endpoints require no authentication. L1 uses an EIP-712 wallet signature to create or derive API credentials and to sign orders locally. L2 requires five `POLY_*` headers and an HMAC-SHA256 request signature using the API secret. L2 authentication alone is insufficient to create an order: the order payload also requires the user's EIP-712 signature. The client must use the correct signer, funder, and signature type; current types include EOA `0`, legacy proxy `1`, Gnosis Safe `2`, and deposit-wallet `POLY_1271` `3`. Secrets belong in environment variables or secure key management and authenticated signing must remain server-side.
- [CLOB V2 production migration](https://docs.polymarket.com/changelog#clob-v2-is-live-on-production) — CLOB V2 replaced V1 in production on 2026-04-28 at the unchanged `https://clob.polymarket.com` host with no V1 compatibility. V2 replaced USDC.e collateral with pUSD, removed `nonce`, `feeRateBps`, and `taker` from the signed order struct, added millisecond `timestamp`, `metadata`, and `builder`, and moved fee selection to match time. Any legacy SDK, signing schema, USDC.e assumption, or order-supplied fee is a production blocker.
- [Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle) and [order overview](https://docs.polymarket.com/trading/orders/overview) — all orders are limit orders; a "market" order is a marketable limit order. Supported time-in-force behavior is GTC, GTD, FOK, and FAK; post-only is valid only for resting GTC/GTD orders. Placement results may be `live`, `matched`, `delayed`, or `unmatched`, while resulting trades progress independently through `MATCHED`, `MINED`, `CONFIRMED`, `RETRYING`, or terminal `FAILED`. Partial fills leave only the remainder cancellable. Selected crypto/finance markets impose a 250 ms taker delay and configured sports markets may impose a longer delay; an order is pending and cannot be cancelled during either delay. Therefore an accepted response is not proof of final settlement, and timeout/recovery logic must handle a temporarily uncancellable order plus post-match chain finality.
- [Order book schema](https://docs.polymarket.com/api-reference/market-data/get-order-book) — `GET /book?token_id=...` returns string-valued `timestamp`, price, and size fields plus `market` condition ID, `asset_id`, state `hash`, sorted bids (descending), sorted asks (ascending), `min_order_size`, `tick_size`, `neg_risk`, and `last_trade_price`. Code must preserve decimal precision, enforce the returned tick and minimum size, distinguish token ID from condition ID, consume the correct best-price ends, and use timestamp/hash for freshness and change detection rather than treating a successful fetch as fresh indefinitely.
- [Fees](https://docs.polymarket.com/trading/fees) — the source audit's assumed flat 1.5% taker fee is not a valid production model. Fees are per-market, applied at match time, and discoverable through `getClobMarketInfo(conditionID)` / `feesEnabled`. Makers currently pay zero platform fee; enabled taker fees follow `shares × feeRate × price × (1 - price)`, with category-specific rates and five-decimal USDC rounding, while geopolitics is currently fee-free. Profitability checks must fetch and cache authoritative per-market fee parameters with bounded staleness and fail closed when unavailable or incompatible; maker rebates must not be counted as guaranteed execution proceeds.
- [Rate limits](https://docs.polymarket.com/api-reference/rate-limits) — limits are endpoint-specific and subject to change. Current CLOB market-data limits include `/book` 1,500/10s and `/books` 500/10s. Ledger `/trades`, `/orders`, `/notifications`, and `/order` is 900/10s; API-key endpoints are 100/10s. Trading has both burst and sustained limits, including `POST /order` and `DELETE /order` at 5,000/10s burst and 120,000/10min sustained. These exchange ceilings are not safe application defaults: the bot still needs much lower local risk/order caps, bounded retries, jittered exponential backoff, and idempotency.
- [CLOB error codes](https://docs.polymarket.com/resources/error-codes) — errors are structured JSON with an `error` field and include 401 authentication failures, 429 throttling, malformed/oversized payloads, invalid token/order/signature/owner/signer, insufficient balance or allowance, tick/minimum-size violations, duplicate orders, unavailable order books, and 503 exchange modes. Trading-disabled can reject both orders and cancels; cancel-only permits cancels but no new orders; post-only mode permits cancels and post-only orders and supplies retry timing. Error handling must classify terminal validation failures separately from retryable throttling/service modes and must never blindly retry an ambiguous placement without reconciling by order ID/state first.

Polymarket documentation coverage for authentication, lifecycle, limits, schemas, fees, errors, and safety-relevant exchange modes is complete for the audit phase. Kalshi official protocol research remains pending. These conclusions are audit inputs only; the current implementation has not yet been proven to satisfy them.

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

### 2026-07-14 — Iteration 3

- Completed the current official Polymarket CLOB documentation review across authentication, CLOB V2 migration, order and trade lifecycle, order-book schema, per-market fees, rate limits, and error/service modes.
- Converted protocol facts into explicit implementation-audit obligations, including V2-only signing/collateral, dynamic fee discovery, decimal/tick/minimum-size enforcement, delayed-order handling, settlement-state reconciliation, freshness checks, and classified/idempotent retry behavior.

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

### 2026-07-14 — Iteration 3

- Official documentation review: **PASS** — direct current Polymarket sources were opened for all seven required protocol areas and their URLs and conclusions were recorded above.
- Documentation cross-check: **PASS** — the current rate-limit page agrees with the 2026-06-01 changelog increase; the 2026-04-28 changelog confirms CLOB V2 production cutover and no V1 compatibility.
- Implementation tests were not run because this bounded iteration changed documentation only. No bot, dashboard, scanner, websocket, collector, signing flow, order construction/submission, cancellation, or balance/allowance mutation was run.

## ML data and evaluation evidence

Not evaluated yet. The source audit reports snapshot-building and collection code but no trained model, training CLI, or inference pipeline. This claim remains unverified.

## Remaining blockers

- Kalshi official authentication, lifecycle, rate-limit, schema, fee, error, and safety research has not yet been completed or recorded. The equivalent Polymarket research is complete.
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

The mandatory audit conversion, redacted Polymarket credential check, and Polymarket protocol research are complete, but implementation auditing and full verification are not. This is an interim state, not a production-readiness verdict.
