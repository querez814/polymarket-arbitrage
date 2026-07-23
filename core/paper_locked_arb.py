"""Conservative, non-mutating shadow ledger for locked cross-venue arbitrage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math

from core.cross_platform_arb import CrossPlatformOpportunity
from utils.paper_trade_store import PaperTradeStore


@dataclass(frozen=True)
class PaperLockedTrade:
    pair_id: str
    token: str
    buy_platform: str
    hedge_platform: str
    observed_buy_price: float
    observed_sell_price: float
    contracts: float
    committed_capital: float
    projected_locked_pnl: float
    effective_net_edge: float
    observed_at_utc: str


class PaperLockedArbitrageLedger:
    """Reserve paper capital without assuming settlement or recycling proceeds."""

    def __init__(
        self,
        *,
        initial_balance: float,
        max_plan_capital: float,
        required_observations: int = 2,
        slippage_buffer_per_contract: float = 0.02,
        liquidity_fraction: float = 0.10,
        min_effective_edge: float = 0.01,
        approved_market_ids: set[str] | frozenset[str] | None = None,
        store: PaperTradeStore | None = None,
    ):
        values = (
            initial_balance,
            max_plan_capital,
            slippage_buffer_per_contract,
            liquidity_fraction,
            min_effective_edge,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("paper ledger limits must be finite")
        if initial_balance <= 0 or max_plan_capital <= 0:
            raise ValueError("paper balances must be positive")
        if required_observations < 1:
            raise ValueError("required_observations must be positive")
        if not 0 < liquidity_fraction <= 1:
            raise ValueError("liquidity_fraction must be in (0, 1]")
        if slippage_buffer_per_contract < 0 or min_effective_edge < 0:
            raise ValueError("paper edge deductions must be non-negative")
        self.initial_balance = initial_balance
        self.max_plan_capital = max_plan_capital
        self.required_observations = required_observations
        self.slippage_buffer_per_contract = slippage_buffer_per_contract
        self.liquidity_fraction = liquidity_fraction
        self.min_effective_edge = min_effective_edge
        self.approved_market_ids = (
            None if approved_market_ids is None else frozenset(approved_market_ids)
        )
        self._store = store
        self._confirmations: dict[str, int] = {}
        self._used_pairs: set[str] = set()
        self._trades: list[PaperLockedTrade] = []
        self._unapproved_opportunity_count = 0

    @property
    def committed_capital(self) -> float:
        return sum(trade.committed_capital for trade in self._trades)

    @property
    def available_capital(self) -> float:
        return max(0.0, self.initial_balance - self.committed_capital)

    @property
    def projected_locked_pnl(self) -> float:
        return sum(trade.projected_locked_pnl for trade in self._trades)

    def observe(self, opportunity: CrossPlatformOpportunity) -> PaperLockedTrade | None:
        """Record a trade only after repeated executable observations."""
        pair_id = opportunity.market_pair.pair_id
        if self.approved_market_ids is not None:
            pair_market_ids = {
                opportunity.market_pair.polymarket_execution_id,
                opportunity.market_pair.kalshi_ticker,
            }
            if pair_market_ids != self.approved_market_ids:
                self._unapproved_opportunity_count += 1
                return None
        if pair_id in self._used_pairs:
            return None
        effective_edge = opportunity.net_edge - self.slippage_buffer_per_contract
        if (
            not math.isfinite(effective_edge)
            or effective_edge < self.min_effective_edge
        ):
            self._confirmations.pop(pair_id, None)
            return None
        capital_per_contract = 1.0 - effective_edge
        if capital_per_contract <= 0 or capital_per_contract > 1:
            self._confirmations.pop(pair_id, None)
            return None
        confirmations = self._confirmations.get(pair_id, 0) + 1
        self._confirmations[pair_id] = confirmations
        if confirmations < self.required_observations:
            return None

        visible_contracts = min(
            opportunity.suggested_size,
            opportunity.buy_liquidity * self.liquidity_fraction,
            opportunity.sell_liquidity * self.liquidity_fraction,
        )
        capital_limit = min(self.available_capital, self.max_plan_capital)
        contracts = min(visible_contracts, capital_limit / capital_per_contract)
        if not math.isfinite(contracts) or contracts <= 0:
            return None

        committed = contracts * capital_per_contract
        trade = PaperLockedTrade(
            pair_id=pair_id,
            token=opportunity.token,
            buy_platform=opportunity.buy_platform,
            hedge_platform=opportunity.sell_platform,
            observed_buy_price=opportunity.buy_price,
            observed_sell_price=opportunity.sell_price,
            contracts=contracts,
            committed_capital=committed,
            projected_locked_pnl=contracts * effective_edge,
            effective_net_edge=effective_edge,
            observed_at_utc=datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        self._trades.append(trade)
        self._used_pairs.add(pair_id)
        if self._store:
            self._store.record_event(
                event_type="filled",
                market_id=pair_id,
                market_question=(
                    f"{opportunity.market_pair.polymarket_question} / "
                    f"{opportunity.market_pair.kalshi_title}"
                ),
                token_type=opportunity.token,
                side="LOCKED_PAIR",
                price=capital_per_contract,
                size=contracts,
                notional=committed,
                fee=max(0.0, contracts * (opportunity.gross_edge - effective_edge)),
                strategy_tag="cross_platform_arb",
                status="capital_locked",
                reason_code="conservative_shadow_fill",
                reason_detail="Projected locked PnL; capital is not recycled before settlement.",
                is_simulated=True,
                simulation_label="conservative_shadow_fill",
                pnl_source="projected_locked_paper",
                run_equity=self.initial_balance + self.projected_locked_pnl,
                run_pnl=self.projected_locked_pnl,
            )
        return trade

    def summary(self) -> dict:
        return {
            "initial_balance": self.initial_balance,
            "committed_capital": self.committed_capital,
            "available_capital": self.available_capital,
            "projected_locked_pnl": self.projected_locked_pnl,
            "projected_equity_at_settlement": (
                self.initial_balance + self.projected_locked_pnl
            ),
            "capital_return_pct": (
                self.projected_locked_pnl / self.committed_capital
                if self.committed_capital
                else 0.0
            ),
            "trade_count": len(self._trades),
            "capital_recycled": False,
            "pnl_source": "projected_locked_paper",
            "pair_approval_required": self.approved_market_ids is not None,
            "approved_market_ids": sorted(self.approved_market_ids or ()),
            "unapproved_opportunity_count": self._unapproved_opportunity_count,
            "trades": [asdict(trade) for trade in self._trades[-100:]],
        }
