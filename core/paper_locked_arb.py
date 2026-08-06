"""Conservative, non-mutating shadow ledger for locked cross-venue arbitrage."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Callable
import uuid

from core.cross_platform_arb import CrossPlatformOpportunity
from utils.paper_trade_store import PaperTradeStore


@dataclass(frozen=True)
class PaperLockedTrade:
    trade_id: str
    pair_id: str
    token: str
    buy_platform: str
    hedge_platform: str
    observed_buy_price: float
    observed_sell_price: float
    simulated_buy_price: float
    simulated_sell_price: float
    fee_cost_per_contract: float
    slippage_per_contract: float
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
        max_contracts_per_trade: float | None = None,
        max_pair_capital: float | None = None,
        max_total_capital: float | None = None,
        required_observations: int = 2,
        slippage_buffer_per_contract: float = 0.02,
        liquidity_fraction: float = 0.10,
        min_effective_edge: float = 0.01,
        approved_market_ids: set[str] | frozenset[str] | None = None,
        blacklisted_market_ids: set[str] | frozenset[str] | None = None,
        max_open_pairs: int | None = None,
        allow_verified_auto_approval: bool = False,
        auto_approval_confidence: float = 0.94,
        pair_cooldown_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        store: PaperTradeStore | None = None,
    ):
        normalized_max_pair_capital = (
            initial_balance if max_pair_capital is None else max_pair_capital
        )
        normalized_max_total_capital = (
            initial_balance if max_total_capital is None else max_total_capital
        )
        values = (
            initial_balance,
            max_plan_capital,
            normalized_max_pair_capital,
            normalized_max_total_capital,
            slippage_buffer_per_contract,
            liquidity_fraction,
            min_effective_edge,
            auto_approval_confidence,
            pair_cooldown_seconds,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("paper ledger limits must be finite")
        if max_contracts_per_trade is not None and (
            not isinstance(max_contracts_per_trade, (int, float))
            or isinstance(max_contracts_per_trade, bool)
            or not math.isfinite(max_contracts_per_trade)
            or max_contracts_per_trade <= 0
        ):
            raise ValueError("max_contracts_per_trade must be finite and positive")
        if (
            initial_balance <= 0
            or max_plan_capital <= 0
            or normalized_max_pair_capital <= 0
            or normalized_max_total_capital <= 0
        ):
            raise ValueError("paper balances must be positive")
        if (
            not isinstance(required_observations, int)
            or isinstance(required_observations, bool)
            or required_observations < 1
        ):
            raise ValueError("required_observations must be a positive integer")
        if max_open_pairs is not None and (
            not isinstance(max_open_pairs, int)
            or isinstance(max_open_pairs, bool)
            or max_open_pairs < 1
        ):
            raise ValueError("max_open_pairs must be a positive integer")
        if not 0 < liquidity_fraction <= 1:
            raise ValueError("liquidity_fraction must be in (0, 1]")
        if slippage_buffer_per_contract < 0 or min_effective_edge < 0:
            raise ValueError("paper edge deductions must be non-negative")
        if not 0 <= auto_approval_confidence <= 1:
            raise ValueError("auto_approval_confidence must be in [0, 1]")
        if pair_cooldown_seconds <= 0:
            raise ValueError("pair_cooldown_seconds must be positive")
        self.initial_balance = initial_balance
        self.max_plan_capital = max_plan_capital
        self.max_contracts_per_trade = max_contracts_per_trade
        self.max_pair_capital = normalized_max_pair_capital
        self.max_total_capital = normalized_max_total_capital
        self.required_observations = required_observations
        self.slippage_buffer_per_contract = slippage_buffer_per_contract
        self.liquidity_fraction = liquidity_fraction
        self.min_effective_edge = min_effective_edge
        self.approved_market_ids = (
            None if approved_market_ids is None else frozenset(approved_market_ids)
        )
        self.blacklisted_market_ids = frozenset(blacklisted_market_ids or ())
        self.max_open_pairs = max_open_pairs
        self.allow_verified_auto_approval = allow_verified_auto_approval
        self.auto_approval_confidence = auto_approval_confidence
        self.pair_cooldown_seconds = pair_cooldown_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._store = store
        self._confirmations: dict[str, int] = {}
        self._last_traded_at: dict[str, datetime] = {}
        self._consumed_contracts_by_quote: dict[tuple[object, ...], float] = {}
        self._trades: list[PaperLockedTrade] = []
        self._unapproved_opportunity_count = 0
        self._auto_approved_trade_count = 0
        self._decision_counts: Counter[str] = Counter()
        self._last_decision = "not_evaluated"

    def _decide(self, reason_code: str) -> None:
        self._decision_counts[reason_code] += 1
        self._last_decision = reason_code

    @staticmethod
    def _quote_capacity_key(
        opportunity: CrossPlatformOpportunity,
    ) -> tuple[object, ...]:
        return (
            opportunity.market_pair.pair_id,
            opportunity.token,
            opportunity.buy_platform,
            opportunity.sell_platform,
            round(opportunity.buy_price, 10),
            round(opportunity.sell_price, 10),
        )

    @property
    def committed_capital(self) -> float:
        return sum(trade.committed_capital for trade in self._trades)

    @property
    def committed_capital_by_pair(self) -> dict[str, float]:
        committed: dict[str, float] = {}
        for trade in self._trades:
            committed[trade.pair_id] = (
                committed.get(trade.pair_id, 0.0) + trade.committed_capital
            )
        return committed

    @property
    def available_capital(self) -> float:
        return max(0.0, self.initial_balance - self.committed_capital)

    @property
    def remaining_deployable_capital(self) -> float:
        return max(
            0.0,
            min(
                self.available_capital,
                self.max_total_capital - self.committed_capital,
            ),
        )

    @property
    def projected_locked_pnl(self) -> float:
        return sum(trade.projected_locked_pnl for trade in self._trades)

    @property
    def last_decision(self) -> str:
        return self._last_decision

    def observe(self, opportunity: CrossPlatformOpportunity) -> PaperLockedTrade | None:
        """Record a trade only after repeated executable observations."""
        pair_id = opportunity.market_pair.pair_id
        sizing_values = (
            opportunity.suggested_size,
            opportunity.max_size,
            opportunity.buy_liquidity,
            opportunity.sell_liquidity,
        )
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
            for value in sizing_values
        ):
            self._decide("invalid_opportunity_sizing")
            return None
        economic_values = (
            opportunity.buy_price,
            opportunity.sell_price,
            opportunity.gross_edge,
            opportunity.net_edge,
        )
        if (
            not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in economic_values
            )
            or not 0 < opportunity.buy_price < 1
            or not 0 < opportunity.sell_price < 1
        ):
            self._decide("invalid_opportunity_economics")
            return None
        pair_market_ids = {
            opportunity.market_pair.polymarket_execution_id,
            opportunity.market_pair.kalshi_ticker,
        }
        if pair_market_ids & self.blacklisted_market_ids:
            self._decide("pair_blacklisted")
            return None
        if self.approved_market_ids is not None:
            manually_approved = pair_market_ids == self.approved_market_ids
            auto_approved = (
                self.allow_verified_auto_approval
                and opportunity.market_pair.auto_approved
                and opportunity.market_pair.semantic_relation == "equivalent"
                and opportunity.market_pair.verification_confidence
                >= self.auto_approval_confidence
            )
            if not manually_approved and not auto_approved:
                self._unapproved_opportunity_count += 1
                self._decide("pair_not_approved")
                return None
        else:
            auto_approved = False
        pair_is_open = pair_id in self.committed_capital_by_pair
        if (
            not pair_is_open
            and self.max_open_pairs is not None
            and len(self.committed_capital_by_pair) >= self.max_open_pairs
        ):
            self._decide("max_open_pairs_reached")
            return None
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        last_traded_at = self._last_traded_at.get(pair_id)
        if last_traded_at is not None:
            elapsed = (now - last_traded_at).total_seconds()
            if elapsed < self.pair_cooldown_seconds:
                self._decide("pair_cooldown_active")
                return None
        effective_edge = opportunity.net_edge - self.slippage_buffer_per_contract
        if (
            not math.isfinite(effective_edge)
            or effective_edge < self.min_effective_edge
        ):
            self._confirmations.pop(pair_id, None)
            self._decide("effective_edge_below_threshold")
            return None
        capital_per_contract = 1.0 - effective_edge
        if capital_per_contract <= 0 or capital_per_contract > 1:
            self._confirmations.pop(pair_id, None)
            self._decide("invalid_capital_per_contract")
            return None
        visible_contracts = min(
            opportunity.suggested_size,
            opportunity.max_size,
            opportunity.buy_liquidity * self.liquidity_fraction,
            opportunity.sell_liquidity * self.liquidity_fraction,
            (
                self.max_contracts_per_trade
                if self.max_contracts_per_trade is not None
                else opportunity.suggested_size
            ),
        )
        quote_capacity_key = self._quote_capacity_key(opportunity)
        unconsumed_visible_contracts = max(
            0.0,
            visible_contracts
            - self._consumed_contracts_by_quote.get(quote_capacity_key, 0.0),
        )
        if unconsumed_visible_contracts <= 0:
            self._confirmations.pop(pair_id, None)
            self._decide("displayed_depth_already_consumed")
            return None
        confirmations = self._confirmations.get(pair_id, 0) + 1
        self._confirmations[pair_id] = confirmations
        if confirmations < self.required_observations:
            self._decide("awaiting_confirmation")
            return None

        remaining_pair_capital = max(
            0.0,
            self.max_pair_capital - self.committed_capital_by_pair.get(pair_id, 0.0),
        )
        remaining_total_capital = max(
            0.0,
            self.max_total_capital - self.committed_capital,
        )
        capital_limit = min(
            self.available_capital,
            self.max_plan_capital,
            remaining_pair_capital,
            remaining_total_capital,
        )
        contracts = min(
            unconsumed_visible_contracts,
            capital_limit / capital_per_contract,
        )
        if not math.isfinite(contracts) or contracts <= 0:
            self._decide("insufficient_paper_capital_or_liquidity")
            return None

        committed = contracts * capital_per_contract
        fee_cost_per_contract = max(
            0.0,
            opportunity.gross_edge - opportunity.net_edge,
        )
        half_slippage = self.slippage_buffer_per_contract / 2
        simulated_buy_price = opportunity.buy_price + half_slippage
        simulated_sell_price = opportunity.sell_price - half_slippage
        if not 0 < simulated_buy_price < 1 or not 0 < simulated_sell_price < 1:
            self._decide("simulated_leg_price_out_of_bounds")
            return None
        trade = PaperLockedTrade(
            trade_id=f"paper_xplat_{uuid.uuid4().hex[:20]}",
            pair_id=pair_id,
            token=opportunity.token,
            buy_platform=opportunity.buy_platform,
            hedge_platform=opportunity.sell_platform,
            observed_buy_price=opportunity.buy_price,
            observed_sell_price=opportunity.sell_price,
            simulated_buy_price=simulated_buy_price,
            simulated_sell_price=simulated_sell_price,
            fee_cost_per_contract=fee_cost_per_contract,
            slippage_per_contract=self.slippage_buffer_per_contract,
            contracts=contracts,
            committed_capital=committed,
            projected_locked_pnl=contracts * effective_edge,
            effective_net_edge=effective_edge,
            observed_at_utc=now.isoformat().replace("+00:00", "Z"),
        )
        if self._store:
            market_question = (
                f"{opportunity.market_pair.polymarket_question} / "
                f"{opportunity.market_pair.kalshi_title}"
            )
            platform_market_ids = {
                "polymarket": opportunity.market_pair.polymarket_execution_id,
                "kalshi": opportunity.market_pair.kalshi_ticker,
            }
            projected_pnl_after_trade = (
                self.projected_locked_pnl + trade.projected_locked_pnl
            )
            self._store.record_cross_platform_paper_trade(
                trade={
                    "trade_id": trade.trade_id,
                    "pair_id": pair_id,
                    "market_question": market_question,
                    "token": trade.token,
                    "contracts": trade.contracts,
                    "gross_edge_per_contract": opportunity.gross_edge,
                    "fee_cost_per_contract": trade.fee_cost_per_contract,
                    "slippage_per_contract": trade.slippage_per_contract,
                    "effective_edge_per_contract": trade.effective_net_edge,
                    "committed_capital": trade.committed_capital,
                    "projected_locked_pnl": trade.projected_locked_pnl,
                    "observed_at_utc": trade.observed_at_utc,
                    "pnl_source": "projected_locked_paper",
                },
                legs=[
                    {
                        "leg_role": "buy",
                        "platform": trade.buy_platform,
                        "market_id": platform_market_ids[trade.buy_platform],
                        "side": "buy",
                        "observed_price": trade.observed_buy_price,
                        "simulated_price": trade.simulated_buy_price,
                        "size": trade.contracts,
                    },
                    {
                        "leg_role": "hedge",
                        "platform": trade.hedge_platform,
                        "market_id": platform_market_ids[trade.hedge_platform],
                        "side": "sell",
                        "observed_price": trade.observed_sell_price,
                        "simulated_price": trade.simulated_sell_price,
                        "size": trade.contracts,
                    },
                ],
                run_equity=self.initial_balance + projected_pnl_after_trade,
                run_pnl=projected_pnl_after_trade,
            )
        self._trades.append(trade)
        self._consumed_contracts_by_quote[quote_capacity_key] = (
            self._consumed_contracts_by_quote.get(quote_capacity_key, 0.0)
            + trade.contracts
        )
        self._last_traded_at[pair_id] = now
        self._confirmations.pop(pair_id, None)
        if auto_approved:
            self._auto_approved_trade_count += 1
        self._decide("paper_trade_recorded")
        return trade

    def summary(self) -> dict:
        return {
            "initial_balance": self.initial_balance,
            "committed_capital": self.committed_capital,
            "committed_capital_by_pair": self.committed_capital_by_pair,
            "max_pair_capital": self.max_pair_capital,
            "max_total_capital": self.max_total_capital,
            "max_contracts_per_trade": self.max_contracts_per_trade,
            "max_open_pairs": self.max_open_pairs,
            "available_capital": self.available_capital,
            "cash_balance": self.available_capital,
            "remaining_deployable_capital": self.remaining_deployable_capital,
            "reserved_cost_basis": self.committed_capital,
            "unrealized_mark_to_market_pnl": 0.0,
            "realized_settlement_pnl": 0.0,
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
            "auto_approved_trade_count": self._auto_approved_trade_count,
            "verified_auto_approval_enabled": self.allow_verified_auto_approval,
            "auto_approval_confidence": self.auto_approval_confidence,
            "pair_cooldown_seconds": self.pair_cooldown_seconds,
            "decision_counts": dict(sorted(self._decision_counts.items())),
            "last_decision": self._last_decision,
            "trades": [asdict(trade) for trade in self._trades[-100:]],
        }
