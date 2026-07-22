# Future Convergence Strategy

Status: deferred research candidate  
Recorded: July 21, 2026, 9:28 PM EDT

## Strategy chosen for production

The first production strategy is **locked cross-venue arbitrage**:

- Trade only when an after-fee spread already exists between equivalent contracts.
- Execute the scarcer/less-liquid leg with immediate-or-cancel behavior.
- Hedge only the quantity authoritatively filled on the first leg, using the deeper venue.
- If the hedge fails or its result is ambiguous, stop new trading, reconcile authoritative venue state, and surface any residual exposure.
- Never turn a failed second leg into an unplanned directional position.
- A completed complementary pair may remain held until settlement; the bot is not waiting for the spread to appear after buying.

This favors many small-to-medium, immediately hedged opportunities over a large unhedged bet. Sequential execution can miss some fleeting spreads, but it limits uncertainty to one venue mutation at a time and makes partial fills and crash recovery tractable.

## Boundary for a future convergence strategy

Convergence trading would buy an apparently underpriced contract before a complete hedge is available, then wait for the price difference to narrow. That introduces directional, timing, model, liquidity, and capital-duration risk. It must have a separate enablement flag, risk budget, dataset, backtest, monitoring, and production gate. It must never be an automatic fallback when locked arbitrage cannot complete.

## Initial sources

- Rothschild and Pennock report persistent cross-exchange price lags and arbitrage between equivalent prediction contracts: https://doi.org/10.3233/AF-140031
- Ito, Yamada, Takayasu, and Takayasu show that multi-leg arbitrage opportunities can disappear within seconds and that execution risk changes expected profit: https://www.nber.org/papers/w26706
- Research on political prediction markets discusses liquidity, contract limits, and market structure as constraints on arbitrage profitability: https://www.ubplj.org/index.php/jpm/article/download/1796/1605/5823
- Polymarket documents immediate FOK/FAK execution versus resting GTC/GTD orders: https://docs.polymarket.com/trading/orders/overview
- Kalshi V2 documents IOC/FOK execution, client order IDs, and immediate fill quantities: https://docs.kalshi.com/api-reference/orders/create-order-v2

## Research queue

1. Build a versioned, manually verified corpus of equivalent contracts including full resolution rules, deadlines, sources, orientation, and amendment history.
2. Collect synchronized order-book depth from both venues with clock-skew, staleness, and disconnect diagnostics.
3. Measure spread size, duration, convergence probability, time to convergence, and maximum adverse excursion.
4. Replay executable bid/ask depth with fees, slippage, partial fills, size increments, and conservative queue assumptions.
5. Separate normal periods from breaking news, market close, scheduled maintenance, and final hours before resolution.
6. Audit resolution-basis differences between apparently equivalent contracts.
7. Benchmark ML against no-trade, fixed-threshold, time-decayed, and liquidity-conditioned strategies.
8. Use chronological walk-forward validation with related event groups isolated across splits.
9. Define maximum unhedged notional, holding time, loss, staleness, per-event concentration, venue concentration, and forced-exit limits.
10. Calculate return on capital after expected holding time and settlement delay, not just cents per contract.
11. Complete a live paper shadow run and compare simulated fills with later authoritative venue data.
12. Require an independent evidence matrix and explicit operator approval before enabling convergence trading.

## Suggested searches

- `cross exchange prediction market price convergence empirical study`
- `prediction market pairs trading transaction costs liquidity`
- `limits to arbitrage prediction markets contract resolution risk`
- `cross venue arbitrage leg risk optimal execution partial fills`
- `survival analysis price convergence trading signals`
- `walk forward validation event grouped financial machine learning leakage`
- `Kalshi Polymarket equivalent contracts settlement rule differences`

Prefer peer-reviewed papers, working papers with disclosed methodology, official exchange documentation, and reproducible datasets. Treat promotional profit claims only as leads requiring verification.
