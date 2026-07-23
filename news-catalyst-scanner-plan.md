# News Catalyst Scanner — Implementation Plan

**Status:** Phases 0–4 and 6 implemented on 2026-07-23. The scanner remains
disabled by default; enabling it starts log-only observation, and the separate
`apply_priority_boost` switch authorizes priority-lane changes. Phase 5
(directional mispricing) is intentionally not built.
**Goal:** Use an LLM (OpenAI API) to scan for "what's happening today" and use it as a *complementary* signal to re-prioritize which markets get fast-refresh monitoring — separate from, and in addition to, the liquidity-threshold and category-matching fixes already identified.

**Important scoping note:** This does not replace the two structural fixes already found (the $1 no-op liquidity threshold, and the hard category-partition bug blocking sports matches in `core/semantic_market_matching.py`). Those affect whether a trade is *executable at all*. This module only affects which of the executable trades you notice *first*. Build this as Phase 2, after those land.

---

## Two signals, kept as separate features

| | Signal 1: Priority Booster | Signal 2: Mispricing Detector |
|---|---|---|
| What it does | Flags which existing matched markets have active news attention right now | Independently estimates a probability from news and compares to live market price |
| Risk profile | None — purely re-ranks monitoring priority | Directional bet against a news catalyst — real risk if the LLM's read is wrong |
| Build first? | Yes — Phase 1 | No — optional Phase 5, feature-flagged off by default |
| Feeds into | Existing priority/monitoring lane (`run_with_dashboard.py`) | New, separate opportunities table — never mixed with real arb opportunities |

Keeping these separate matters: Signal 1 is a safe enhancement to an existing system. Signal 2 is a new trading strategy with its own risk profile and should not be silently blended into what currently looks like risk-free arbitrage.

---

## Phase 0 — Setup

- Add `openai` Python SDK as a dependency.
- Add config keys to `config.paper.production.yaml` (all new, all default to safe/off values):
  ```yaml
  news_catalyst:
    enabled: false                     # master switch, off until validated
    scan_interval_seconds: 1800        # 30 min; news doesn't need 60s polling
    lookback_hours: 6
    relevance_similarity_threshold: 0.60
    catalyst_boost_weight: 0.35        # tunable multiplier, start conservative
    max_news_items_per_scan: 40
    max_daily_api_calls: 60            # hard cost cap
    mispricing_detector_enabled: false # separate switch, keep off initially
  ```
- Store the OpenAI API key via env var (`OPENAI_API_KEY`), not in the YAML file.

---

## Phase 1 — News ingestion

- Use OpenAI's Responses API with the built-in `web_search` tool (not a plain chat completion — plain completions have no live internet access and will hallucinate "current" events from stale training data).
- Prompt structure: ask for structured JSON output of recent, verifiable news items relevant to the market categories you trade (politics, crypto, finance, sports, entertainment, tech), each with:
  ```json
  {
    "headline": "...",
    "summary": "...",
    "entities": ["..."],
    "topic_category": "politics|crypto|finance|sports|entertainment|tech",
    "published_at": "ISO8601",
    "source_url": "https://..."
  }
  ```
- **Hard requirement:** discard any item without a real `source_url`. This is the guardrail against hallucination — every extracted "event" must be traceable to an actual fetched page, not the model's unaided belief.
- Run this as a background async task on its own interval (`scan_interval_seconds`), separate from the existing 60s bundle-arb scan loop and the 2s priority-lane orderbook refresh. Do not block either of those loops on a news scan — LLM + web search calls can take several seconds, which is fine for a 30-minute cadence but would be a disaster if it blocked the fast lanes.

---

## Phase 2 — Map news to markets

- Reuse the embedding/retrieval infrastructure already built in `core/semantic_market_matching.py` for cross-platform market matching — the same machinery, pointed at a new comparison: news-item text vs. existing market question text (instead of Polymarket question vs. Kalshi question).
- For each news item, embed `headline + summary`, and compare against the embeddings of all currently-tracked market questions (both platforms) using cosine similarity.
- Any market scoring above `relevance_similarity_threshold` gets a `news_relevance_score` and a reference to the matching news item for that scan cycle.
- Note: if the category-partition bug (found in the earlier investigation) is still unfixed when this is built, news-to-market matching should **not** inherit that same hard-partition mistake — compare news items against markets regardless of category, since a single news item can plausibly be relevant to a market irrespective of how that market happened to get classified.

---

## Phase 3 — Feed into priority ranking

- Combine with the volume-weighted ranking already recommended for the matched-pairs priority lane:
  ```
  rank_score = similarity_score * log(1 + min(polymarket_volume, kalshi_volume)) * (1 + catalyst_boost_weight * news_relevance_score)
  ```
  where `news_relevance_score` is 0 if no news item matched within `lookback_hours`, else the similarity score from Phase 2.
- Extend `data_feed.set_priority_markets(...)` in `run_with_dashboard.py` (currently only fed the top-39 cross-platform matched pairs) so that a market with a strong news-catalyst boost can also earn a slot in the fast 2-second refresh lane, even if it wasn't already in the top-39 by pure similarity.

---

## Phase 4 — Persistence (new tables in `data/paper_performance.db`)

```sql
CREATE TABLE news_catalyst_events (
    id INTEGER PRIMARY KEY,
    headline TEXT,
    summary TEXT,
    source_url TEXT NOT NULL,
    topic_category TEXT,
    entities_json TEXT,
    published_at_utc TEXT,
    scanned_at_utc TEXT
);

CREATE TABLE news_market_relevance (
    id INTEGER PRIMARY KEY,
    news_event_id INTEGER REFERENCES news_catalyst_events(id),
    market_platform TEXT,       -- 'polymarket' | 'kalshi'
    market_id TEXT,
    relevance_score REAL,
    matched_at_utc TEXT
);
```
This mirrors the existing `semantic_pair_reviews` table pattern already in the DB — same auditability approach, so you can inspect exactly why a market got boosted on any given day.

---

## Phase 5 — Mispricing detector (optional, separate feature flag, build later)

- Only after Phase 1-4 are validated and `news_catalyst.enabled: true` has run cleanly for a while.
- For a news item with high relevance to a specific market, ask the LLM to independently estimate a probability for that market's underlying question, grounded strictly in the retrieved news content (not general knowledge).
- Compare that estimate to the market's current live price on each platform. If there's a meaningful gap, log it to a **new, separate** table (`news_probability_opportunities`) — never write into the same opportunities feed as real cross-platform/bundle arbitrage, since this is a directional bet with real risk, not a hedge.
- Keep `mispricing_detector_enabled: false` by default; this is a distinct strategy decision, not a bug fix.

---

## Phase 6 — Observability & guardrails

- Dashboard: add a small "Today's Catalysts" panel listing news items matched this scan cycle and which markets they boosted — makes the system's reasoning inspectable, consistent with the rest of the dashboard's transparency.
- Cost control: enforce `max_daily_api_calls` and `max_news_items_per_scan` hard caps in code, not just config comments — log a warning and skip the scan cycle if the daily cap is hit.
- Latency control: news scanning must never share a thread/loop with the 60s bundle-arb scan or 2s orderbook refresh. Background task writes its output to the DB; the live loops only ever *read* the latest snapshot.
- Reliability: if the `web_search` tool call fails or returns zero verifiable (source-backed) items, skip the cycle silently (log it, don't retry aggressively) rather than boosting anything based on an empty/failed scan.

---

## Phase 7 — Rollout / testing

1. Run Phase 1-4 in **log-only mode** for several days: compute `news_relevance_score` and log what *would* have been boosted, without actually feeding it into `set_priority_markets`. Manually review whether the flagged catalysts and market matches make sense.
2. Only after that manual review looks sane, leave `news_catalyst.enabled: true`
   and separately flip `news_catalyst.apply_priority_boost: true` so it can
   affect the priority lane.
3. Track before/after: does the composition of the "Monitoring" priority list shift away from pure longshot-nomination markets toward markets with active news attention? That's the success signal for this phase.
4. Leave the mispricing detector (Phase 5) off until Phases 1-4 have run cleanly for a while and you've explicitly decided you want to take on that directional-risk strategy.

---

## Why this is worth building (citations)

- [Lopez-Lira & Tang, "Can ChatGPT Forecast Stock Price Movements?" (arXiv 2309.17322)](https://arxiv.org/pdf/2309.17322) — LLM-derived sentiment scores from headlines showed statistically significant, out-of-sample predictive power for next-day returns, outperforming lexicon-based approaches.
- [PolySwarm: A Multi-Agent LLM Framework (arXiv 2604.03888)](https://arxiv.org/html/2604.03888v1) — argues LLMs' advantage over rule-based/keyword systems is exactly the nuanced context resolution (e.g. "bill failed procedurally but will be reintroduced") that a naive keyword classifier — like the one already found in this codebase's category classifier — cannot do.
- [NewsCatcher — Prediction Market Signals API](https://www.newscatcherapi.com/use-cases/prediction-markets) — a commercial product doing exactly this (open-web news → structured, timestamped, market-question-mapped events), useful as a reference for the ingestion design, or as a paid alternative to rolling your own `web_search`-based ingestion in Phase 1 if scan quality/coverage turns out to be a bottleneck.

---

## Open decisions before building (flag to Zaki, not the coding agent)

1. Data source for Phase 1: OpenAI Responses API `web_search` tool (cheaper, DIY, coverage depends on OpenAI's search quality) vs. a dedicated news API/product like NewsCatcher (better coverage/recall, added subscription cost). Recommend starting with the OpenAI-only path since it needs no new vendor, and only upgrading if scan quality is a bottleneck.
2. `scan_interval_seconds` and `max_daily_api_calls` — these directly control ongoing OpenAI API spend. Start conservative (every 30 min, 60 calls/day) and only decrease the interval if the log-only testing in Phase 7 shows real value from more frequent scans.
3. Whether to ever build Phase 5 (mispricing detector) at all — it's a materially different, higher-risk strategy than the rest of this system, and should be a deliberate decision, not an automatic follow-on.
