# Arb Engine Overhaul Plan

Purpose: raise the trade/opportunity frequency of `polymarket-arbitrage-nightwatch` by fixing four distinct, independent problems identified from the live database (`data/paper_performance.db`) and logs (`logs/bot.log`) as of 2026-07-22. This is a paper-trading / dry-run system (`mode.trading_mode: dry_run` in `config.paper.production.yaml`) — keep it in dry-run throughout this overhaul unless explicitly told otherwise.

Work through phases in order. Each phase is independent enough to ship and validate on its own. Do not skip Phase 0 — later phases will not produce any visible trades until it's addressed.

---

## Diagnosis recap (why this plan exists)

- `data/paper_performance.db` → `paper_trade_events` table has 0 rows despite the bot running for hours.
- `core/cross_platform_arb.py` matches Polymarket ↔ Kalshi markets using `difflib.SequenceMatcher` (plain character-level string ratio, see the `from difflib import SequenceMatcher` import and the `text_sim = SequenceMatcher(...)` call around line 401-402) against `min_match_similarity: 0.90`. In `logs/bot.log`, 16 of 19 completed matching cycles today found **0 pairs** out of ~175,243 candidates scanned; only 2 cycles found any (147, then 33).
- `config.paper.production.yaml` has `trading.whitelist: []`. `core/paper_locked_arb.py` (`PaperLockedArbitrageLedger.observe`) checks `approved_market_ids` and returns `None` for anything not on it — the file's own comment says "Empty means observation-only." This is why zero trades are logged even on the rare cycle that finds a match.
- Even an approved pair needs `paper_confirmation_observations: 3` repeated observations before being logged, and once traded, `_used_pairs` permanently excludes that pair from trading again.
- `config.paper.production.yaml` has `bundle_arb_enabled: false` and `mm_enabled: false` — the only active strategy is cross-platform matching, which is the flakiest one.
- Full matching sweeps take multiple minutes and are interleaved with repeated `404 Not Found` responses from `clob.polymarket.com/book` for stale/closed token IDs, adding latency inside the same loop that's supposed to refresh every `cross_platform_refresh_seconds: 300`.

---

## Phase 0 — Unblock trade logging (config/process, no algorithm work)

**Goal:** stop the pipeline from discarding every opportunity via the empty whitelist gate.

**Files:** `config.paper.production.yaml`, `core/paper_locked_arb.py`, `core/decision_journal.py`

**Tasks:**
1. Decide and implement one of:
   - (a) **Manual review queue**: log every matched-and-verified candidate pair (post Phase 1 verification) to `core/decision_journal.py` or a new reviewable file/table, and add a small CLI or config entry to approve a pair into `trading.whitelist` without a code deploy.
   - (b) **Auto-approve on high confidence**: once Phase 1's verification stage (see below) is in place and its false-positive rate is measured to be low, allow `approved_market_ids` to be populated automatically for pairs that clear a strict confidence bar, with a config flag to fall back to manual review for anything below it.
2. Whichever option is chosen, keep `mode.trading_mode: dry_run` — this only affects whether paper trades get logged/simulated, not real order placement.

**Acceptance criteria:** at least one real matched pair reaches `paper_trade_events` end-to-end without manually hand-editing the YAML whitelist mid-run.

---

## Phase 1 — Replace the market matcher (the core fix)

**Primary source:** Jonas Gebele & Florian Matthes, *"Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets"* — [arXiv:2601.01706](https://arxiv.org/html/2601.01706v1). Follow this paper's pipeline as the target architecture.

**Secondary/practical source:** *"Cross Market Arbitrage"* project writeup — [wutis.at PDF](https://wutis.at/wp-content/uploads/cross_market_arb_v5.pdf) — a lighter-weight, non-peer-reviewed implementation of the same idea using open sentence-transformer embeddings instead of a paid LLM API. Use this if API cost/latency for OpenAI embeddings + LLM verification is a concern.

**File:** `core/cross_platform_arb.py`, specifically `class MarketMatcher` (~line 92) and `async def find_matches` (~line 506).

**Replace the current single-step `SequenceMatcher` ratio with a three-stage pipeline:**

1. **Structural filtering** (cheap, do this first to shrink the candidate set):
   - Cross-platform constraint (already implicit — only compare Polymarket ↔ Kalshi).
   - Category matching — the paper uses an LLM classifier into 20 semantic categories; your logs already show category buckets (`Matching crypto:`, `Matching finance:`, `Matching entertainment:`, `Matching tech:` in `logs/bot.log`), so this may already exist in some form — audit `core/cross_platform_arb.py` for the existing category-bucketing logic before adding a new one, and only build what's missing.
   - Temporal overlap — require validity windows to overlap. Per the paper, this should already reduce ~10^10 naive comparisons for ~100k events down to roughly 10^4 candidates per market.

2. **Embedding-based retrieval:**
   - Represent each market with a vector embedding built from title + description + outcome labels + resolution metadata.
   - Paper's choice: OpenAI `text-embedding-3-large` (3072-dim), cosine similarity, retrieve k=20 nearest neighbors per market. The paper found k=20 captures >99.9% of true equivalent/subset relations (Appendix 0.C of the paper).
   - Cheaper alternative (per the wutis.at writeup): a small open SentenceTransformers model (their guidance: "small, effective model — not too computationally demanding") is sufficient for candidate generation; use this if API cost or per-cycle latency from calling OpenAI's embedding endpoint on every refresh is a concern. Cache embeddings for markets that haven't changed since last cycle — don't re-embed everything every 5 minutes.

3. **Verification stage** (this is what gets false-positive rate down, replacing the fragile fixed 0.90 threshold):
   - Two-pass check per the paper: (i) a high-recall plausibility filter that discards clearly incompatible pairs, (ii) a structured comparison of resolution criteria — cutoff times, oracle identity, dispute procedures, scope qualifiers — to confirm the two markets resolve identically in all states (equivalence) or one implies the other (subset relation).
   - The paper implements both passes with LLM calls and reports <2% false positives after this stage, with residual errors concentrated in stale markets, highly specific resolution semantics, and implicit scope qualifiers — expect and log for these edge cases rather than trying to eliminate them entirely.
   - This verification output is what should feed Phase 0's auto-approve path, if that option is chosen.

**Acceptance criteria:** matching cycles reliably find pairs across most runs (not 0/19), and spot-checking N found pairs manually confirms they are genuinely equivalent markets (target: sample and hand-verify at least 20 pairs before trusting auto-approval).

---

## Phase 2 — Enable same-platform combinatorial arbitrage (new, independent opportunity source)

**Primary source:** Oriol Saguillo, Vahid Ghafouri, Lucianna Kiffer, Guillermo Suarez-Tangil, *"Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets"* — [arXiv:2508.03474](https://arxiv.org/abs/2508.03474). This paper studies Polymarket-only arbitrage (no second venue needed) and reports ~$40M in realized historical profit from two types: **market rebalancing arbitrage** (a single market's condition prices don't sum to $1) and **combinatorial arbitrage** (a derived/composite market's price diverges from the sum of its component markets on the same platform).

**Reference implementation for the combinatorial logic:** [m0wer/polyarb](https://github.com/m0wer/polyarb) — implements exactly this pattern: a derived market (e.g. "Republican wins Presidency") priced as the logical OR of mutually exclusive atomic outcomes; arbitrage exists when the derived market's price diverges from the sum of its components.

**File:** `core/arb_engine.py` — this file already contains bundle-arbitrage logic (`net_edge_long` / `net_edge_short` around lines 325-444, comparing `total_ask`/`total_bid` against `1 ± min_edge`), but it's disabled via `bundle_arb_enabled: false` and `mm_enabled: false` in `config.paper.production.yaml`.

**Tasks:**
1. Audit the existing bundle-arb code in `core/arb_engine.py` against the Saguillo et al. definitions of rebalancing vs. combinatorial arbitrage — confirm it correctly implements both, not just simple same-market rebalancing.
2. Implement the paper's heuristic candidate-reduction (timeliness, topical similarity, combinatorial relationship) so combinatorial arbitrage scanning across many Polymarket events stays tractable — the paper notes naive enumeration is O(2^(n+m)) and infeasible without this reduction.
3. Enable `bundle_arb_enabled: true` in a test/staging config once validated, keep `mm_enabled` decision separate (market-making is a different risk profile, not covered by this plan).

**Acceptance criteria:** the engine detects and logs at least a handful of same-platform combinatorial or rebalancing opportunities per day in the existing `logs/opportunities.log`, independent of whether any cross-platform Kalshi match exists.

---

## Phase 3 — Reduce detection-to-decision latency (two-tier monitoring)

**Primary source:** *"Cross Market Arbitrage"* writeup — [wutis.at PDF](https://wutis.at/wp-content/uploads/cross_market_arb_v5.pdf), "Real-time monitoring design" section: each matched pair gets its own monitoring instance subscribed to orderbook updates, with two operating modes — a small set of "hot" pairs polled/streamed at high frequency, and a broader sweep of all matched pairs updated at lower frequency.

**Supporting evidence for why this matters:** Guang Cheng, Jiaxin Yang, Haoxuan Zou, *"Arbitrage Analysis in Polymarket NBA Markets"* ([Semantic Scholar](https://www.semanticscholar.org/paper/3bce6e1689be5020b067861a1c82c82b19cf8721)) found single-market arbitrage episodes persist a median of only 3.6 seconds. A single global rescan every 5 minutes (and observed in your logs to sometimes take up to ~1h45m between completed cycles) cannot catch most opportunities regardless of matching quality — this is a separate bottleneck from Phase 1.

**Files:** `core/cross_platform_arb.py` (`CrossPlatformArbEngine`, ~line 707), `core/production_runtime.py` (`ProductionArbitrageRuntime.evaluate_pair`, ~line 348), `core/data_feed.py` (already does websocket order-book streaming — extend this pattern rather than replacing it).

**Tasks:**
1. Split matched pairs into two tiers after Phase 1 produces a stable match set: a small "hot" set (e.g. pairs with recent near-threshold edges, or highest historical opportunity frequency) monitored continuously via the existing websocket `DataFeed`, and a "cold" set matched/refreshed on the existing ~5-minute cadence.
2. Decouple the expensive full 175k-candidate matching sweep (Phase 1's structural filter + embedding retrieval) from the fast opportunity-evaluation loop — matching should run on its own cadence (e.g. every 30-60 min, since market rosters don't change every 5 minutes) while price/edge evaluation on already-matched pairs runs continuously off the websocket feed.
3. Fix the `404 Not Found` spam: before querying `clob.polymarket.com/book` for a token ID, check it against the currently active/open market list (per [Polymarket's own error-code docs](https://docs.polymarket.com/resources/error-codes), 404 means "No orderbook exists for the requested token id" — i.e., the market is closed/stale). Skip known-closed IDs instead of repeatedly querying them.

**Acceptance criteria:** time between an edge appearing on the order book and the engine evaluating it drops from minutes to single-digit seconds for hot-tier pairs; 404 rate in `logs/bot.log` drops close to zero.

---

## Phase 4 — Rate-limit and request-efficiency hygiene

**Source:** general crypto-trading-bot rate-limit practice, e.g. [Crypto API Rate Limiting: Best Practices for Trading Bots](https://dev.to/gunnarthorderson/crypto-api-rate-limiting-best-practices-for-trading-bots-2022) — caching, batching, exponential backoff with jitter, and circuit breakers on repeated failures are reported to cut request volume 5-20x versus naive polling.

**Tasks (apply wherever the bot calls Polymarket/Kalshi REST endpoints, e.g. `polymarket_client/api.py`, `kalshi_client/api.py`):**
1. Cache the active-market list with a TTL instead of refetching every cycle.
2. Add exponential backoff with jitter on `429`/`5xx` responses (Polymarket's docs explicitly recommend this for `429`).
3. Add a simple circuit breaker: if the rolling failure rate over the last N calls exceeds a threshold, pause that endpoint's calls briefly instead of continuing to hammer it.

**Acceptance criteria:** total REST call volume per matching cycle drops measurably; no more thundering-herd retry bursts visible in `logs/bot.log`.

---

## Phase 5 — Revisit the conservative gating parameters (tuning, not algorithmic)

**Files:** `config.paper.production.yaml`, `core/paper_locked_arb.py`

These aren't bugs — they're deliberate conservatism from the original design — but they should be revisited once Phases 1-4 make the matching/latency pipeline trustworthy:

1. `paper_confirmation_observations: 3` — each required confirmation costs time the opportunity may not have (median 3.6s windows per Phase 3's source). Consider lowering once matching false-positive rate is measured and known to be low (Phase 1 target: <2%, per the paper).
2. `_used_pairs` in `core/paper_locked_arb.py` permanently excludes a pair after one trade. Consider a cooldown-and-reset instead of permanent exclusion, so a pair can be traded again once its opportunity resets.
3. `min_edge: 0.02` (cross-platform) / `min_edge: 0.01` (bundle default in `core/arb_engine.py`) — validate these against realistic post-fee, post-slippage edges observed in the papers above rather than leaving them as defaults.

**Acceptance criteria:** each parameter change is tested independently (one variable at a time) against the metrics from the validation plan below, so you can attribute any frequency change to a specific cause.

---

## Validation plan (run after each phase)

1. Track per-cycle metrics over time: pairs found (Phase 1), opportunities logged in `logs/opportunities.log` (Phases 1-2), time-from-detection-to-evaluation (Phase 3), REST call count and 404/429 rate (Phase 4), and trades landing in `data/paper_performance.db` → `paper_trade_events` (all phases, ultimate success metric).
2. Backtest the new matcher (Phase 1) against the existing historical snapshots already in the repo (`data/historical/*.jsonl`, `data/training/live_snapshots.jsonl`) to compare recall/precision against the old `SequenceMatcher` approach before relying on it live.
3. Keep `mode.trading_mode: dry_run` throughout — this plan only targets paper-trade frequency, not live capital deployment.

---

## Source list (full citations)

- Jonas Gebele, Florian Matthes. "Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets." arXiv:2601.01706. https://arxiv.org/html/2601.01706v1
- Oriol Saguillo, Vahid Ghafouri, Lucianna Kiffer, Guillermo Suarez-Tangil. "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets." arXiv:2508.03474. https://arxiv.org/abs/2508.03474
- Guang Cheng, Jiaxin Yang, Haoxuan Zou. "Arbitrage Analysis in Polymarket NBA Markets." https://www.semanticscholar.org/paper/3bce6e1689be5020b067861a1c82c82b19cf8721
- K. P. Tsang, Zichao Yang. "The Anatomy of a Blockchain Prediction Market: Polymarket in the 2024 U.S. Presidential Election." https://www.semanticscholar.org/paper/128f4ea08395d57d60ae59719a366150be39aab6
- "Cross Market Arbitrage" project writeup. https://wutis.at/wp-content/uploads/cross_market_arb_v5.pdf
- Matúš Klečka. "Arbitrage bot" (thesis, Charles University). https://dspace.cuni.cz/bitstream/handle/20.500.11956/202468/130436593.pdf
- Polymarket API error-code documentation. https://docs.polymarket.com/resources/error-codes
- "Crypto API Rate Limiting: Best Practices for Trading Bots." https://dev.to/gunnarthorderson/crypto-api-rate-limiting-best-practices-for-trading-bots-2022
- Reference implementation: m0wer/polyarb (same-platform combinatorial arbitrage). https://github.com/m0wer/polyarb

Note: a GitHub search for "polymarket kalshi arbitrage bot" surfaces many keyword-stuffed, scam-pattern repos (near-duplicate spammy descriptions). Do not use those as reference implementations.
