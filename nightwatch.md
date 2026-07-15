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
- As of iteration 6, the application can explicitly select this wallet item with `POLYMARKET_PRIVATE_KEY_KEYCHAIN_LABEL` while keeping the ignored file free of wallet material. A redacted in-process validation combined the ignored L2 configuration with the selected Keychain item and satisfied all Polymarket Global live credential checks. No exchange client or network request was created by that validation.

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

Polymarket documentation coverage for authentication, lifecycle, limits, schemas, fees, errors, and safety-relevant exchange modes is complete for the audit phase. These conclusions are audit inputs only; the current implementation has not yet been proven to satisfy them.

### Kalshi Trade API protocol research — 2026-07-14

The following current official primary sources were reviewed at 2026-07-14 21:55 EDT. Only documentation and public pages were requested. No authenticated Kalshi request, order signing/construction/submission, cancellation, or other trading action was attempted.

- [API environments](https://docs.kalshi.com/getting_started/api_environments) and [authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests) — production and demo are separate environments with non-interchangeable credentials. The recommended production REST root is `https://external-api.kalshi.com/trade-api/v2`. Every authenticated request requires the API key ID, a millisecond timestamp, and a Base64 RSA-PSS/SHA-256 signature over `timestamp + uppercase HTTP method + full path`, excluding query parameters. Private keys must remain in secure storage, environment selection must be explicit, and live startup must reject demo credentials/hosts and incomplete or mismatched credential fragments.
- [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2), [Get Order](https://docs.kalshi.com/api-reference/orders/get-order), [Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2), and [order direction](https://docs.kalshi.com/getting_started/order_direction) — current event order placement uses `POST /portfolio/events/orders`, the single-book `bid`/`ask` model, fixed-point dollar price strings, fixed-point contract counts, explicit `time_in_force`, and explicit self-trade prevention. The legacy `/portfolio/orders` shape and `action`/`side` direction fields are deprecated; canonical response direction is `outcome_side` or `book_side`. Create may immediately fill partially and reports `fill_count`, `remaining_count`, average fill price/fee, and matching-engine timestamp. Order state is `resting`, `canceled`, or `executed`; cancellation reports only the amount actually reduced. `client_order_id` is the deduplication key, with duplicate submissions returning conflict, so timeouts must reconcile by client/server order ID before any retry. Production code must model partial fill plus residual exposure, use bounded GTC/IOC/FOK semantics deliberately, enable `cancel_order_on_pause` where appropriate, and never treat HTTP acceptance or a cancel response as proof that the requested full size was neutralized.
- [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses) and [Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook) — the current `orderbook_fp` schema contains ascending arrays of fixed-point `[price_dollars, count_fp]` bids in `yes_dollars` and `no_dollars`; it does not return asks. The best bid is the last level. A YES ask is exactly `1.00 - best NO bid`, and a NO ask is `1.00 - best YES bid`; that complementary order is the executable liquidity, not a synthetic estimate that warrants the source audit's arbitrary 2% haircut. Code must preserve decimal strings, walk the opposite bid book for derived-ask depth, handle empty sides, enforce the market's price-level structure, and attach its own bounded observation timestamp because the REST orderbook response does not provide one.
- [Series metadata](https://docs.kalshi.com/api-reference/market/get-series), [series fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes), [event fee changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes), and [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding) — the source audit's claim that Kalshi charges 0% for most markets is not safe production input. Each series declares `fee_type` (`quadratic`, `quadratic_with_maker_fees`, or `flat`) and `fee_multiplier`; scheduled series changes and event-level overrides can alter them. Trade fees round up to $0.0001, balance precision differs by account type, and an order-level accumulator/rebate mechanism affects fill-level net fees. The authoritative [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) was also requested, but the official host returned HTTP 429 with a Vercel browser challenge in this environment. Until the exact current schedule can be retrieved and implemented alongside live series/event metadata, economic evaluation must fail closed rather than assume zero or a single static percentage.
- [Rate limits and tiers](https://docs.kalshi.com/getting_started/rate_limits) and [account API limits](https://docs.kalshi.com/api-reference/account/get-account-api-limits) — authenticated traffic uses separate token-bucket read and write budgets, not fixed request windows. Most endpoints currently cost 10 tokens, while authoritative non-default costs come from `GET /account/endpoint_costs`; batch items are charged individually. Tier budgets and bucket capacities can change, and a 429 response currently has no `Retry-After` or rate-limit headers. The client must discover/account for current costs, keep materially lower local operating limits, and use bounded exponential backoff with jitter without blindly retrying an ambiguous order placement.
- [REST error schemas](https://docs.kalshi.com/api-reference/orders/create-order-v2), [exchange status](https://docs.kalshi.com/api-reference/exchange/get-exchange-status), and [market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle) — order APIs distinguish invalid input (400), authentication failure (401), missing orders (404), duplicate/conflicting state (409), throttling (429), and server failure (500) using structured `code`, `message`, `details`, and `service` fields. Exchange status separately exposes `exchange_active` and `trading_active` and may return 503/504; Kalshi may pause trading at any time. Markets can be `inactive`, and reactivation cancels all resting orders; closed/determined/disputed/amended states are not finalized settlement. Error handling must classify terminal versus retryable failures, gate placement on exchange and market state, reconcile ambiguous 5xx/timeouts, and recover from exchange-driven cancellations and settlement transitions.

Kalshi documentation coverage for authentication, order lifecycle, order-book and market schemas, fees, rate limits, errors, and safety-relevant exchange/market states is complete for the audit phase, subject to the explicitly recorded fee-schedule PDF access limitation. The review exposes current V2 obligations that differ materially from the source audit and must be checked against the implementation next.

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

### 2026-07-14 — Iteration 4

- Completed the current official Kalshi Trade API documentation review across authentication/environments, V2 order lifecycle and direction, fixed-point order books, dynamic fees and rounding, token-bucket limits, structured errors, and exchange/market pause and settlement states.
- Replaced source-audit assumptions with explicit implementation-audit obligations: V2 bid/ask orders, client-order idempotency and reconciliation, fixed-point precision, complementary-book depth, metadata-driven fees, bounded local throttling, and fail-closed exchange/market-state gates.

### 2026-07-14 — Iteration 5

- Began the implementation audit with G4's effective live-startup boundary. Both `main.py` and `run_with_dashboard.py` previously validated the file while it was still configured for dry-run, applied `--live` afterward, and did not revalidate before constructing the bot. The CLI override could therefore bypass loader-level live credential checks until later client initialization.
- Exposed `validate_config()` as the single reusable validation boundary and made both entrypoints revalidate after CLI mode overrides, before bot or exchange-client construction.
- Made live configuration fail closed when `mode.data_mode` is `simulation` or `mode.simulate_fills` is true. This prevents a live-labelled process from consuming generated market data or hypothetical fill behavior.
- This is a partial G4 hardening, not resolution of G4: application-level Keychain retrieval, credential-source policy, platform/host consistency, Kalshi credential completeness, and safe production defaults still require later slices.

### 2026-07-14 — Iteration 6

- Added an explicit macOS Keychain wallet-key source for live Polymarket Global startup. `POLYMARKET_PRIVATE_KEY_KEYCHAIN_LABEL` selects a generic-password item through `/usr/bin/security`; the value is captured only in process and is never placed in command arguments, configuration files, logs, or errors.
- Both CLI entrypoints resolve runtime secrets after applying mode overrides and before effective configuration validation or client construction. Resolution is idempotent because configuration loaded as live is also revalidated at the entrypoint.
- Live startup now rejects ambiguous simultaneous direct-key and Keychain-label configuration and fails with actionable, output-redacted errors when Keychain access is unavailable, denied, missing, or empty.
- Updated tracked templates to document the Keychain alternative without embedding any secret. G4 remains open for tracked-versus-ignored credential-source enforcement, venue/host consistency, Kalshi credential completeness, and conservative production defaults.

### 2026-07-14 — Iteration 7 (22:07 EDT)

- Continued G4 with fail-closed venue and credential coherence before any client construction. Live Polymarket Global now requires the documented production CLOB, websocket, and Gamma endpoints plus Polygon chain ID 137; live Polymarket US requires its configured production API and gateway endpoints; live Kalshi monitoring requires the documented production Trade API V2 base URL.
- Rejected contradictory `cross_platform_enabled: true` / `kalshi_enabled: false` settings in every mode.
- Kalshi authentication remains optional for public-only monitoring, which is the only Kalshi behavior currently wired into the dashboard. If either credential fragment is supplied, configuration now requires both the API key ID and RSA private-key path and verifies that the referenced path is an existing regular file. No key content is read by configuration validation.
- G4 remains open only for tracked-versus-ignored credential-source enforcement and a final conservative-default audit; Kalshi order capability and authenticated credential validation remain correctly assigned to G2 rather than being represented as complete here.

### 2026-07-14 — Iteration 8 (22:09 EDT)

- Corrected the automated-test boundary for the explicitly invoked, networked `test_kalshi_connection.py` diagnostic. The module now declares `__test__ = False`, so pytest does not mistake its async connectivity helper for an offline unit test; its existing importable function and standalone CLI behavior are unchanged.
- Formatted the touched diagnostic with the repository's configured Black version. No connectivity check, exchange client, or network request was run.

### 2026-07-14 — Iteration 9 (22:14 EDT)

- Cleared the 23 previously recorded mypy errors in the four exchange-client modules by making optional-client narrowing explicit, correcting the abstract async-iterator contract, annotating response collections and mixed-type query parameters, and separating dry-run/live local collection names. These changes do not add or enable order behavior.
- Made Kalshi key loading fail closed when a parseable PEM contains a non-RSA private key. Kalshi authentication requires RSA-PSS; accepting another key family would defer the mismatch to a less actionable signing failure.
- Added offline regression coverage proving that an Ed25519 PEM is rejected as a Kalshi credential. No production key file was parsed.

### 2026-07-14 — Iteration 10 (22:15 EDT)

- Cleared all 14 mypy errors in the arbitrage-engine implementation and its tests. The missing-price guard now explicitly narrows each optional YES/NO bid/ask before price arithmetic and sizing; runtime skip behavior is unchanged.
- Bundle-opportunity tests now prove the optional opportunity is present before inspecting it, matching the model contract without suppressing static checks.

### 2026-07-15 — Iteration 11 (06:40 EDT)

- Began G5 by adding an explicit dollar-notional ceiling at `RiskManager.check_order()`, the final deterministic admission boundary before `ExecutionEngine` calls an exchange client. The existing `trading.max_order_size` limits shares/contracts and could not prevent an individually oversized dollar commitment when aggregate exposure remained available.
- Added `risk.max_order_notional` to configuration, validation, both runtime entrypoints, and the offline backtest wiring. Non-finite/non-positive caps and caps above global exposure now fail configuration validation rather than silently creating a missing or ineffective per-order guard. Orders with non-finite or non-positive computed notionals also fail closed at admission.
- Conservative defaults cap a single order at $15 notional; balanced and aggressive profile defaults are $20 and $30. Both tracked live templates use a stricter $10 cap. This resolves only G5's per-order-dollar-cap slice; order-rate, daily-order, and position-count caps remain unaudited and unresolved.

### 2026-07-15 — Iteration 12 (06:45 EDT)

- Hardened G5 admission accounting so acknowledged but unfilled orders reserve their remaining limit-price notional against both per-market and global exposure caps. Before this change, only filled positions consumed those caps, so multiple individually valid open orders could collectively commit more than the configured exposure limits.
- Centralized reservation lifecycle in `ExecutionEngine` tracking: tracking an order idempotently establishes its remaining reservation, each partial fill releases the filled size at the order's limit price while actual fill exposure is recorded, and final untracking/cancellation releases any residual. Availability, utilization, limit-health, and risk-summary calculations now include filled plus pending committed exposure.
- This closes G5's simultaneous-open-order dollar-exposure accounting gap for the existing single-venue execution engine. It does not supply an order-rate cap, daily-order cap, cross-venue shared ledger, or restart recovery; those remain unresolved.

### 2026-07-15 — Iteration 13 (06:48 EDT)

- Added an explicit fail-closed `risk.max_open_orders` admission limit before any exchange-client placement call. It counts the existing idempotent residual-exposure reservations, so repeated tracking cannot inflate the count, partial fills retain the slot, and full fill, cancellation, or final untracking releases it.
- Configuration now rejects non-integer, Boolean, zero, and negative caps. Conservative, balanced, and aggressive defaults are 4, 6, and 8; the tracked aggressive dry-run config uses 8, while both live templates use a stricter cap of 3. Both runtime entrypoints and offline backtest construction carry the validated cap into `RiskManager`.
- Risk summaries expose current and maximum open-order counts, and risk health fails if reconciled state ever contains more orders than the configured cap. This resolves G5's explicit in-process open-order-count slice only; it does not claim restart reconciliation, cross-venue accounting, atomic venue execution, or rate/daily-order controls.

### 2026-07-15 — Iteration 14 (06:51 EDT)

- Removed `ExecutionEngine._place_order()`'s unconditional three-attempt retry loop. A timeout or transport error may occur after venue acceptance, so repeating the request without first reconciling a stable client/server order id could create duplicate exposure.
- Placement now makes exactly one exchange-client call and fails closed on every exception. The unused retry configuration was removed, and the offline regression test injects an ambiguous timeout through a mock and proves only one client call occurs.
- This closes the known blind-placement-retry gap only. It does not provide a client-generated idempotency key, ambiguous-result reconciliation, crash recovery, or a safe retry path; those remain required before G3/G7 can be resolved.

### 2026-07-15 — Iteration 15 (06:55 EDT)

- Added fail-closed local G5 placement-attempt budgets at the final execution boundary: a rolling one-minute cap and a UTC-calendar-day cap. The attempt is charged immediately before the sole exchange-client call, so a timeout or other ambiguous result continues to consume capacity instead of allowing repeated uncertain placements to evade the limits.
- Configuration now validates both limits as positive non-Boolean integers and carries them through the main runtime, dashboard runtime, and offline backtest construction. Conservative defaults are 10 attempts/minute and 100/day; balanced and aggressive profiles use 20/250 and 30/500; both tracked live templates use stricter 6/minute and 50/day limits.
- Risk summaries expose current and maximum attempt counts, and mocked execution coverage proves that after an ambiguous first placement reaches the client, the exhausted local cap rejects the next placement before a second client call. This closes the in-process order-rate and daily-order-count slice only; the counters are not crash-durable or shared across processes/venues, so G5/G7 remain open for reconciliation-backed restart recovery, a shared cross-venue ledger, and position-count policy.

### 2026-07-15 — Iteration 16 (06:58 EDT)

- Added a fail-closed `risk.max_open_positions` policy at the final deterministic admission boundary. It counts distinct markets carrying either filled exposure or acknowledged pending residual exposure, so multiple orders in one market consume one position slot and a new market is rejected once portfolio breadth reaches the cap.
- Orders in an already-committed market continue through the remaining dollar, volume, strategy, loss, and drawdown checks; clearing the last residual reservation for a market releases its slot. Risk health also fails if reconciled state is already above the configured limit, and summaries expose current and maximum position counts.
- Configuration now rejects non-integer, Boolean, zero, and negative caps. Conservative, balanced, and aggressive defaults are 4, 6, and 8; the tracked aggressive dry-run config uses 8, while both live templates use a stricter cap of 3. Both runtime entrypoints and offline backtest construction carry the validated cap into `RiskManager`.
- This closes G5's in-process position-count policy only. It does not make the ledger crash-durable or cross-process/cross-venue, prove venue reconciliation, or provide two-leg atomicity; those remain unresolved G3/G5/G7 requirements.

### 2026-07-15 — Iteration 17 (07:01 EDT)

- Hardened G7 cancellation accounting so a successful live cancel request no longer releases residual exposure by itself. `ExecutionEngine.cancel_order()` now performs one authoritative order refresh, applies any fills that raced with cancellation, and releases the remaining reservation only after the venue reports a terminal cancelled, expired, or rejected state (or the racing fill fully closes the order).
- If reconciliation fails or the refreshed order remains open or partially filled, cancellation returns failure and deliberately retains local order tracking plus pending and strategy exposure. The timeout monitor, shutdown cancellation, market cancellation, and strategy cancellation paths all use this same fail-closed boundary.
- Mocked regression coverage proves both sides of the lifecycle: a non-terminal refresh retains the entire residual, while a terminal cancellation with a racing partial fill books the filled exposure before releasing only the cancelled residual. This narrows G7's optimistic-cancellation gap; it does not provide restart discovery, durable state, stable placement idempotency, cross-venue reconciliation, or two-leg residual hedging.

### 2026-07-15 — Iteration 18 (07:04 EDT)

- Added a read-only, fail-closed G7 live-startup gate before any execution task is created. The engine now requires successful venue open-order and position reads and refuses to start when either an orphan open order or any nonzero position exists. It never auto-cancels or mutates the account; the operator must reconcile non-flat state externally.
- Live `PolymarketClient` open-order and position read failures no longer degrade to empty collections. Returning an empty account on timeout, missing bridge state, or another read error could falsely satisfy recovery checks, so these paths now raise explicit output-redacted errors.

### 2026-07-15 — Iteration 19 (07:48 EDT)

- Began G14 in the corrected mandatory order by adding a versioned, deterministic point-in-time dataset contract for cross-platform opportunity ranking. The explicit feature schema contains current top-of-book prices, sizes, spreads, edge/cost values, capacity, and venue observation skew; labels are later observed gross and cost-adjusted top-of-book edge plus a positive-after-costs indicator.
- Label construction now carries explicit Polymarket and Kalshi taker-fee, per-leg gas, per-leg slippage, decision-latency, forecast-horizon, and maximum-label-delay assumptions. Labels are explicitly future quote observations, not fills or realized PnL.
- Added fail-closed schema/identity/time validation and timestamp-grouped chronological train/validation/test splitting. Earlier partitions purge every example whose label reaches into the next partition, preventing label-window overlap.
- Dataset construction now reports candidate and exclusion counts. Incomplete required quote features are excluded and counted rather than silently imputed or allowed to abort all otherwise valid rows; duplicate pair/timestamp observations and malformed schemas still fail closed.
- This is a partial G14 foundation only. Model training, baseline comparison, calibration/economic metrics, versioned artifact persistence, fail-closed loading, subordinate inference integration, and a final honest evaluation remain required.
- This establishes a conservative flat-only restart boundary, not crash recovery. The current live positions reader remains a placeholder REST integration and therefore blocks live startup until an authoritative account-position implementation exists. Durable local state, stable placement idempotency, sustained reconciliation, cross-venue recovery, and two-leg residual hedging remain unresolved.

### 2026-07-15 — Iteration 20 (07:53 EDT)

- Added a deterministic standardized logistic ranker for the G14 positive-future-edge label. Normalization and class prevalence are derived exclusively from the chronological training partition; optimization uses fixed settings and no random operations. The model explicitly predicts a later observed cost-adjusted quote-edge class, not fill probability or realized profit, and its output is not an execution authorization.
- Added untouched validation/test evaluation against a training-prevalence baseline. Evidence includes Brier score, log loss, ROC AUC when both classes exist, expected calibration error, and auditable calibration bins. Invalid dimensions, schemas, model versions, non-finite values, probabilities outside `[0, 1]`, and single-class training data all fail closed.
- The best legitimate local dataset cannot train this model: all 36 training, 4 validation, and 12 test examples have a negative-after-costs label under the recorded cost/latency assumptions. Refusing to fit a one-class model is the honest result. This is an external data-evidence blocker, not evidence of model quality or profitability.
- G14 remains partial. Versioned artifact serialization/loading, runtime inference constrained beneath deterministic gates, and evaluation on sufficiently diverse real matched observations remain required.

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

### 2026-07-14 — Iteration 4

- Official documentation review: **PASS** — current first-party Kalshi pages were retrieved for all required protocol areas, and their direct URLs and implementation consequences are recorded above.
- Documentation currency check: **PASS** — the official sitemap reports the reviewed API reference at OpenAPI version 3.24.0 with pages updated 2026-07-09 or later; the current order docs identify the event-order V2 migration and deprecated legacy direction fields.
- Authoritative fee schedule PDF retrieval: **BLOCKED EXTERNALLY** — `https://kalshi.com/docs/kalshi-fee-schedule.pdf` returned HTTP 429 with a Vercel browser challenge. Official API metadata and fee-rounding/change documentation were reviewed, but the exact schedule formula was not guessed or copied from a secondary source.
- `git diff --check`: **PASS**.
- Implementation tests were not run because this bounded iteration changed documentation only. No authenticated Kalshi request, bot, dashboard, scanner, websocket, collector, signing flow, order construction/submission, cancellation, or account mutation was run.

### 2026-07-14 — Iteration 5

- `uv run --with-requirements requirements.txt python -m pytest tests/test_config_loader.py -q`: **PASS** — 7 tests passed, including regressions for post-CLI live revalidation and rejection of simulation data and hypothetical fills in live mode.
- `uv run --with-requirements requirements.txt python -m pytest tests test_real_data.py -q`: **PASS** — 114 tests passed with 266 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PARTIAL / PRE-EXISTING COLLECTION DEFECT** — 114 tests passed and the manually oriented root-level `test_kalshi_connection.py::test_kalshi_connection` failed collection because its async function has no pytest async marker. This file was not changed in this iteration; correcting the canonical test boundary remains later work.
- `uv run --with pyyaml python -m py_compile utils/config_loader.py utils/__init__.py main.py run_with_dashboard.py`: **PASS**.
- `uv run --with-requirements requirements.txt python -m compileall -q core dashboard kalshi_client polymarket_client polymarket_us_client scripts utils main.py run_with_dashboard.py`: **PASS**.
- Narrow in-process fail-closed probe that changed a default `BotConfig` from dry-run to live and called `validate_config()`: **PASS** — validation rejected all four missing Polymarket Global credential fields. No client was constructed and no network request occurred.
- `uv run --with-requirements requirements.txt black --check tests/test_config_loader.py utils/__init__.py`: **PASS** after formatting the changed test file. A broader check reports that the pre-existing `main.py`, `run_with_dashboard.py`, and `utils/config_loader.py` files would be reformatted wholesale; they were not mechanically rewritten in this bounded safety fix.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports utils/config_loader.py`: **PASS**. A broader ad hoc mypy invocation found 101 existing errors across ten transitively imported files and missing third-party stubs; the repository has no mypy configuration or clean baseline yet.
- `git diff --check`: **PASS**.
- Environment discovery: no repository `.venv` exists; Homebrew `python3` lacks both PyYAML and pytest; `/Users/zaki/.local/bin/uv` successfully created an ephemeral environment for the focused checks. Canonical dependency installation remains unresolved.
- No bot, dashboard, scanner, websocket, collector, exchange client, order/signing flow, or network probe was started. No background process was created.

### 2026-07-14 — Iteration 6

- `uv run --with-requirements requirements.txt python -m pytest tests/test_config_loader.py -q`: **PASS** — 10 tests passed, including mocked Keychain resolution, repeated-resolution idempotency, ambiguous-source rejection, and output-redacted failure behavior.
- `uv run --with-requirements requirements.txt python -m pytest tests test_real_data.py -q`: **PASS** — 117 tests passed with 266 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core dashboard kalshi_client polymarket_client polymarket_us_client scripts utils main.py run_with_dashboard.py`: **PASS**.
- `uv run --with-requirements requirements.txt black --check tests/test_config_loader.py utils/__init__.py`: **PASS** — both focused formatted files unchanged.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports utils/config_loader.py`: **PASS**.
- Redacted in-process effective-config probe with `POLYMARKET_PRIVATE_KEY_KEYCHAIN_LABEL=polymarket-trading-wallet`: **PASS** — the ignored L2 configuration plus selected Keychain wallet item satisfied live Polymarket Global validation. The probe printed only a generic pass message; it constructed no exchange client and made no network request.
- `git diff --check`: **PASS**.
- No bot, dashboard, scanner, websocket, collector, exchange client, order/signing flow, or network request was started. No background process was created.

### 2026-07-14 — Iteration 7 (22:07 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_config_loader.py -q`: **PASS** — 21 tests passed, including Global and US production endpoint/chain pinning, cross-platform mode coherence, paired Kalshi credential fragments, and missing private-key-file rejection.
- `uv run --with-requirements requirements.txt python -m pytest tests test_real_data.py -q`: **PASS** — 128 maintained tests passed with 266 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PARTIAL / PRE-EXISTING COLLECTION DEFECT** — 129 tests passed; the same manually oriented root-level `test_kalshi_connection.py::test_kalshi_connection` failed because its async function has no pytest async marker.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt black --check tests/test_config_loader.py`: **PASS**. The broader loader check still reports that the pre-existing `utils/config_loader.py` file would be reformatted wholesale; this bounded change did not introduce a new project-wide formatting baseline.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports utils/config_loader.py`: **PASS**.
- Redacted Keychain-backed effective-config validation against ignored `config.polymarket.yaml`: **PASS** with the new production endpoint, Polygon chain, mode-coherence, and Kalshi checks. The probe emitted only a generic pass message and created no client or network request.
- `git check-ignore -v config.polymarket.yaml`: **PASS** — still ignored by `.gitignore:71`; `git diff --check`: **PASS**.
- No bot, dashboard, scanner, websocket, collector, exchange client, authenticated request, key parsing, order/signing flow, or network request was started. No background process was created.

### 2026-07-14 — Iteration 8 (22:09 EDT)

- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered suite passed with 129 tests and 266 pre-existing deprecation warnings; the manual Kalshi connectivity diagnostic was not executed.
- `uv run --with-requirements requirements.txt python -m py_compile test_kalshi_connection.py`: **PASS**.
- `uv run --with-requirements requirements.txt black --check test_kalshi_connection.py`: **PASS** after formatting the touched file.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports test_kalshi_connection.py`: **PARTIAL / PRE-EXISTING TYPE BASELINE** — traversing the diagnostic's imported exchange clients found 23 existing errors across `polymarket_client/clob_bridge.py`, `polymarket_client/api.py`, `kalshi_client/api.py`, and `polymarket_us_client/api.py`; none points to this iteration's pytest-boundary declaration.
- `git diff --check`: **PASS**.
- No bot, dashboard, scanner, websocket, collector, exchange client, connectivity diagnostic, order/signing flow, or network request was started. No background process was created.

### 2026-07-14 — Iteration 9 (22:14 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_kalshi_auth.py tests/test_kalshi_client.py tests/test_clob_bridge.py tests/test_polymarket_factory.py tests/test_polymarket_us_parsing.py -q`: **PASS** — 18 focused exchange-client tests passed.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports test_kalshi_connection.py`: **PASS** — the transitive exchange-client slice that reported 23 errors in iteration 8 now reports no issues.
- `uv run --with-requirements requirements.txt black --check kalshi_client/auth.py tests/test_kalshi_auth.py`: **PASS**. The legacy exchange API modules remain outside a repository-wide Black baseline, so they were not reformatted wholesale merely to satisfy this bounded slice.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered suite passed with 130 tests and 266 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports .`: **FAIL / DISCOVERY CONFIGURATION** — without package-base guidance, mypy sees `scripts/backtest_cross_platform.py` under two module names because `scripts/` is not a package.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases .`: **PARTIAL / REPOSITORY TYPE BASELINE** — checked 61 source files and found 91 remaining errors in ten files outside this iteration's exchange-client slice. Full static verification is therefore still outstanding and is not represented as passing.
- `git diff --check`: **PASS**.
- No bot, dashboard, scanner, websocket, collector, exchange client, connectivity diagnostic, production credential parser, authenticated request, order/signing flow, or network request was started. No background process was created.

### 2026-07-14 — Iteration 10 (22:15 EDT)

- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/arb_engine.py tests/test_arb_engine.py`: **PASS** — no issues in the arbitrage engine or its tests.
- `uv run --with-requirements requirements.txt python -m pytest tests/test_arb_engine.py -q`: **PASS** — 15 focused tests passed with 134 deprecation warnings.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered suite passed with 130 tests and 266 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases .`: **PARTIAL / IMPROVED REPOSITORY TYPE BASELINE** — checked 61 source files and found 77 remaining errors in eight files, down from 91 errors in ten files. Neither `core/arb_engine.py` nor `tests/test_arb_engine.py` reports an error.
- `uv run --with-requirements requirements.txt black --check core/arb_engine.py tests/test_arb_engine.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — both legacy files would be reformatted wholesale. The bounded type-safety edits were kept in their existing local style rather than creating unrelated formatting churn.
- No bot, dashboard, scanner, websocket, collector, exchange client, connectivity diagnostic, authenticated request, order/signing flow, or network request was started. No background process was created.

### 2026-07-15 — Iteration 11 (06:40 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_risk_manager.py tests/test_config_loader.py -q`: **PASS** — 49 focused tests passed, including rejection above the configured notional ceiling, acceptance exactly at the ceiling, fail-closed non-finite/non-positive values, profile wiring, and invalid-cap configuration failures.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 141 tests and 290 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/risk_manager.py utils/config_loader.py`: **PASS** — no issues in the changed risk/configuration source slice.
- `uv run --with-requirements requirements.txt python -c 'from utils.config_loader import load_config; c=load_config("config.yaml"); assert c.risk.max_order_notional == 30.0; print("configured notional cap validation: PASS")'`: **PASS** — the tracked dry-run aggressive profile resolves to its intended $30 single-order ceiling.
- `uv run --with-requirements requirements.txt black --check tests/test_risk_manager.py tests/test_config_loader.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — `tests/test_config_loader.py` passes, while the legacy `tests/test_risk_manager.py` would require whole-file whitespace and wrapping churn unrelated to this bounded safety slice. The diff was inspected and not applied.
- `git diff --check`: **PASS**.
- No bot, dashboard, scanner, websocket, collector, exchange client, authenticated request, order construction/signing/submission/cancellation, simulated submission, or account mutation was started or performed. No background process was created.

### 2026-07-15 — Iteration 12 (06:45 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_risk_manager.py tests/test_execution_visibility.py -q`: **PASS** — 29 focused tests passed, including idempotent reservations, market/global admission rejection with pending commitments, partial-fill transfer from pending to filled exposure, and residual release on untracking.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/risk_manager.py core/execution.py tests/test_execution_visibility.py`: **PASS** — no issues in the changed source and lifecycle-test slice.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 144 tests and 306 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt black --check core/risk_manager.py core/execution.py tests/test_risk_manager.py tests/test_execution_visibility.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — `tests/test_execution_visibility.py` passes; the three legacy files would be reformatted wholesale. No broad formatting cleanup was applied, per the priority correction.
- `git diff --check`: **PASS**.
- Verification exercised only offline unit tests, including the repository's existing in-memory dry-run client tests. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, authenticated request, signing flow, external order submission/cancellation, network request, credential access, or account mutation was started or performed. No background process was created.

### 2026-07-15 — Iteration 13 (06:48 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_risk_manager.py tests/test_config_loader.py tests/test_execution_visibility.py -q`: **PASS** — 60 focused tests passed, including exact-boundary rejection, idempotent reservation counting, partial-residual slot retention, full-release slot recovery, over-cap health detection, profile wiring, and invalid-cap configuration failures.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 149 tests and 314 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/risk_manager.py utils/config_loader.py tests/test_risk_manager.py tests/test_config_loader.py`: **PASS** — no issues in the changed risk/configuration implementation and test slice.
- Broader targeted mypy including `main.py` and `run_with_dashboard.py`: **PARTIAL / PRE-EXISTING TYPE BASELINE** — traversed known cross-platform/dashboard/runtime entrypoint debt and reported 65 errors in four files. No broad mypy cleanup was attempted, per the priority correction.
- `uv run --with-requirements requirements.txt python -c 'from utils.config_loader import load_config; c=load_config("config.yaml"); assert c.risk.max_open_orders == 8; print("configured open-order cap validation: PASS")'`: **PASS** — the tracked aggressive dry-run configuration resolves to its intended eight-order ceiling.
- `uv run --with-requirements requirements.txt black --check utils/config_loader.py tests/test_config_loader.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — `tests/test_config_loader.py` passes, while legacy `utils/config_loader.py` would be reformatted wholesale. No broad formatting cleanup was applied.
- `git diff --check`: **PASS**.
- Verification was entirely offline and mocked/in-memory. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, exchange client, authenticated request, order construction/signing/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 14 (06:51 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_execution_visibility.py -q`: **PASS** — 5 focused offline tests passed, including proof that an ambiguous timeout produces exactly one awaited client call.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/execution.py tests/test_execution_visibility.py`: **PASS** — no issues in the changed implementation and regression-test slice.
- `uv run --with-requirements requirements.txt black --check tests/test_execution_visibility.py`: **PASS**.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 150 tests and 316 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m py_compile $(git ls-files '*.py')`: **PASS**.
- `git diff --check`: **PASS** before the documentation update and repeated after it below.
- Verification used an `AsyncMock` only. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, real exchange client, authenticated request, signing, external submission/cancellation, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 15 (06:55 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_risk_manager.py tests/test_config_loader.py tests/test_execution_visibility.py -q`: **PASS** — 72 focused offline tests passed, including rolling-window expiry, UTC-day reset, invalid configuration, profile wiring, and proof that an ambiguous placement consumes the cap before the only client call.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 161 tests and 363 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core polymarket_client utils main.py run_with_dashboard.py`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --follow-imports=skip --ignore-missing-imports core/risk_manager.py core/execution.py utils/config_loader.py`: **PASS** — no issues in the changed risk, execution, and configuration source slice.
- Broader targeted mypy including `main.py` and `run_with_dashboard.py`: **PARTIAL / PRE-EXISTING TYPE BASELINE** — traversal reached known cross-platform, dashboard, and runtime-entrypoint debt and reported 68 errors in six files. No broad mypy cleanup was attempted, per the priority correction.
- `uv run --with-requirements requirements.txt python -c 'from utils.config_loader import load_config; c=load_config("config.yaml"); assert c.risk.max_order_attempts_per_minute == 30; assert c.risk.max_daily_order_attempts == 500; print("configured placement-attempt caps: PASS")'`: **PASS** — the tracked aggressive dry-run configuration resolves to 30 attempts/minute and 500/day.
- `uv run --with-requirements requirements.txt black --check core/risk_manager.py core/execution.py utils/config_loader.py main.py run_with_dashboard.py tests/test_risk_manager.py tests/test_config_loader.py tests/test_execution_visibility.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — all eight legacy files would be reformatted wholesale. No broad formatting cleanup was applied, per the priority correction.
- `git diff --check`: **PASS** before the documentation update and repeated after it below.
- Verification was entirely offline and used deterministic timestamps plus `AsyncMock`; the mock never constructed, signed, serialized, or submitted an exchange order. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, real exchange client, authenticated request, external submission/cancellation, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 16 (06:58 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_risk_manager.py tests/test_config_loader.py tests/test_execution_visibility.py -q`: **PASS** — 78 focused offline tests passed, including distinct filled/pending market counting, same-market admission at the cap, slot release after the last residual reservation, reconciled overage health failure, profile wiring, and invalid-cap configuration failures.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 167 tests and 378 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core utils main.py run_with_dashboard.py polymarket_client kalshi_client polymarket_us_client`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --ignore-missing-imports --explicit-package-bases core/risk_manager.py utils/config_loader.py tests/test_risk_manager.py tests/test_config_loader.py`: **PASS** — no issues in the changed risk/configuration implementation and test slice.
- `uv run --with-requirements requirements.txt python -c 'from utils.config_loader import load_config; c=load_config("config.yaml"); assert c.risk.max_open_positions == 8; print("configured open-position cap: PASS")'`: **PASS** — the tracked aggressive dry-run configuration resolves to its intended eight-market ceiling.
- `uv run --with-requirements requirements.txt black --check core/risk_manager.py utils/config_loader.py tests/test_risk_manager.py tests/test_config_loader.py`: **PARTIAL / PRE-EXISTING FORMAT BASELINE** — all four legacy files would be reformatted wholesale. No broad formatting cleanup was applied, per the priority correction.
- `git diff --check`: **PASS** before the documentation update and repeated after it below.
- Verification was entirely offline. Unit tests instantiated only internal unsigned order models and in-memory risk state; no bot, dashboard, scanner, websocket, collector, connectivity diagnostic, real exchange client, authenticated request, exchange-order payload/signing/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 17 (07:01 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_execution_visibility.py -q`: **PASS** — 8 focused offline tests passed, including non-terminal cancel reconciliation retaining residual exposure and terminal cancellation applying a racing partial fill before release.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 169 tests and 394 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core polymarket_client utils main.py run_with_dashboard.py`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --follow-imports=skip --ignore-missing-imports core/execution.py tests/test_execution_visibility.py`: **PASS** — no issues in the changed execution and regression-test slice.
- `uv run --with-requirements requirements.txt black --check tests/test_execution_visibility.py`: **PASS** after formatting the touched test file. The legacy `core/execution.py` formatting baseline was not broadened.
- `git diff --check`: **PASS** before and after the documentation update.
- Verification used `AsyncMock` exchange boundaries and internal unsigned order/trade models only. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, real exchange client, authenticated request, exchange-order construction/signing/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 18 (07:04 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_execution_visibility.py tests/test_clob_bridge.py -q`: **PASS** — 17 focused offline tests passed, including unreadable-state rejection, non-flat-account rejection before task creation, flat-state acceptance at the isolated preflight boundary, and proof that live client read failures cannot masquerade as empty venue state.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 174 tests and 405 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core polymarket_client utils main.py run_with_dashboard.py`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --follow-imports=skip --ignore-missing-imports core/execution.py polymarket_client/api.py tests/test_execution_visibility.py tests/test_clob_bridge.py`: **PASS** — no issues in the changed execution, client, and regression-test slice.
- `uv run --with-requirements requirements.txt black --check tests/test_execution_visibility.py tests/test_clob_bridge.py`: **PASS**. The legacy source files were not broadly reformatted.
- `git diff --check`: **PASS** before the documentation update and repeated after it below.
- Verification used only `AsyncMock`, internal unsigned order/position models, and a deliberately uninitialized local client. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, connected exchange client, authenticated request, order construction/signing/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 19 (07:48 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_ml_dataset.py tests/test_training_data.py -q`: **PASS** — 12 focused offline tests passed, covering point-in-time label timing, explicit cost adjustment, stale-label exclusion, duplicate rejection, auditable incomplete-feature exclusion, purged chronological splitting, and invalid-assumption rejection.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 183 tests and 405 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt python -m compileall -q core dashboard kalshi_client polymarket_client polymarket_us_client scripts utils main.py run_with_dashboard.py tests`: **PASS**.
- `uv run --with-requirements requirements.txt mypy --follow-imports=skip --ignore-missing-imports utils/ml_dataset.py tests/test_ml_dataset.py`: **PASS** — no issues in the new dataset implementation and tests.
- `uv run --with-requirements requirements.txt black --check utils/ml_dataset.py tests/test_ml_dataset.py`: **PASS** after formatting the two new files.
- `python3 -m compileall -q utils/ml_dataset.py tests/test_ml_dataset.py`: **PASS**.
- `git diff --check`: **PASS** before the documentation update and repeated after it below.
- Deterministic read-only build against `data/training/live_snapshots.jsonl`: **PARTIAL / DATA INSUFFICIENCY** — 38 stored snapshots yielded 112 candidate directions, 84 point-in-time examples across only two pairs and 23 feature timestamps; exclusions were 2 without an eligible future snapshot, 14 without the same future direction, and 12 with incomplete required quote features. The purged chronological split contained 36 train, 4 validation, and 12 test examples. This proves the builder can process the best local paired top-of-book file; it is far too small and narrow to establish model quality, calibration, generalization, fill probability, or profitability.
- Verification only read committed local JSONL and exercised deterministic pure functions. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, exchange client, authenticated request, exchange-order construction/signing/serialization/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

### 2026-07-15 — Iteration 20 (07:53 EDT)

- `uv run --with-requirements requirements.txt python -m pytest tests/test_ml_model.py tests/test_ml_dataset.py tests/test_training_data.py -q`: **PASS** — 25 focused offline tests passed, including deterministic repeatability, train-only baseline construction, calibration metrics, perfect synthetic-separation discrimination, one-class training rejection, unsupported-model rejection, probability validation, and the earlier dataset/collector coverage.
- `uv run --with-requirements requirements.txt python -m pytest -q`: **PASS** — the complete discovered offline suite passed with 196 tests and 405 pre-existing deprecation warnings.
- `uv run --with-requirements requirements.txt mypy --follow-imports=skip --ignore-missing-imports utils/ml_model.py tests/test_ml_model.py`: **PASS** — no issues in the new model/evaluation slice.
- `uv run --with-requirements requirements.txt python -m compileall -q utils/ml_model.py tests/test_ml_model.py`: **PASS**.
- `uv run --with-requirements requirements.txt black utils/ml_model.py tests/test_ml_model.py`: **PASS** after formatting; the legacy repository was not broadly reformatted.
- Deterministic read-only train/evaluate attempt against `data/training/live_snapshots.jsonl`: **FAIL CLOSED / DATA INSUFFICIENCY** — the prior 36/4/12 chronological partitions contained 0/0/0 positive-after-costs examples. Training stopped with `training labels must contain both classes`; therefore no fitted model or local quality/calibration claim was produced.
- Verification read only committed local JSONL and exercised deterministic pure functions. No bot, dashboard, scanner, websocket, collector, connectivity diagnostic, exchange client, authenticated request, exchange-order construction/signing/serialization/submission/cancellation, simulated submission, network request, credential access, account mutation, or background process was started or performed.

## ML data and evaluation evidence

G14 is **PARTIAL** as of 2026-07-15 07:53 EDT. `utils/ml_dataset.py` and `tests/test_ml_dataset.py` provide and verify the versioned point-in-time example schema, explicit cost/slippage/latency assumptions, future-quote labels, leakage guards, auditable exclusions, and purged chronological splits. `utils/ml_model.py` and `tests/test_ml_model.py` now provide deterministic train-only fitting and later-period model-versus-prevalence-baseline evaluation with discrimination and calibration metrics.

The only current paired local top-of-book training file has 38 snapshots and produces 84 eligible directional examples across two market pairs. Its labels are future observed executable quote edges, not fills or realized PnL. Every example in the purged 36/4/12 train/validation/test partitions is negative after recorded costs, so production training correctly fails closed rather than emitting a meaningless one-class artifact. The comparison machinery is verified on deterministic fixtures, but no legitimate local fitted model, quality result, calibration result, artifact, inference decision, or profitability conclusion exists.

## Remaining blockers

- G1–G2 and G6–G13 have not yet been fully audited against current code or current official protocol behavior. G14 now has verified point-in-time dataset/split and deterministic training/evaluation foundations, but still lacks artifact persistence, fail-closed loading, subordinate inference integration, and sufficiently diverse real matched data for an honest fitted-model evaluation. G3 now fails closed instead of blindly retrying ambiguous placement errors, live cancellation retains residual exposure until one terminal refresh, and live startup requires readable flat venue state before creating execution tasks. It still lacks stable idempotency keys, an authoritative implemented position reader, durable/sustained reconciliation, two-leg residual-exposure handling, and actual crash recovery. G4 now has live-override, simulation-mode, Keychain-source, production venue/chain, mode-coherence, and Kalshi-fragment hardening, but is not complete. G5 has verified per-order notional, single-venue pending-order exposure accounting, in-process open-order and distinct-position caps, and in-process rolling/daily placement-attempt caps, but remains open for cross-venue shared-ledger accounting and reconciliation-backed restart recovery.
- The authoritative Kalshi fee-schedule PDF is blocked by an external HTTP 429 browser challenge in this environment. Exact schedule retrieval remains required before any production fee model can be validated; code must also consume current series and event fee metadata rather than relying on the PDF alone.
- G4 remains open: tracked-versus-ignored credential-source enforcement and conservative production defaults have not yet been fully reconciled. Actual Kalshi authenticated credential validation remains part of G2 because no Kalshi order lifecycle is implemented.
- The default `uv run` environment currently lacks PyYAML despite its declaration in `requirements.txt`; the canonical installed environment and dependency checks remain unresolved.
- Full static verification remains outstanding with 77 known mypy errors in eight files outside the exchange-client and arbitrage-engine slices. Configuration, offline matched-data, ML, and dependency/security verification also remain outstanding; the complete discovered test suite and Python compilation currently pass.
- Live-only evidence is prohibited during this run and must never be implied.

## Commits

- Baseline: `cac0f12`.
- Nightwatch commits after baseline: none at iteration 1; the GNHF orchestrator handles commits.

## Final verdict

**NOT YET ASSESSED**

The mandatory audit conversion, redacted Polymarket credential check, and official Polymarket and Kalshi protocol research are complete, but implementation auditing and full verification are not. This is an interim state, not a production-readiness verdict.
