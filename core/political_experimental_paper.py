"""Capital-constrained, no-mutation political experimental paper accounting.

This focused domain boundary owns political paper semantics while the backing
tables remain in :mod:`utils.platform_opportunity_store`, allowing later book
evidence, signals, fills, and accounting events to share one SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from utils.platform_opportunity_store import PlatformOpportunityStore

_ONE_CENT = Decimal("0.01")
_ONE_CENTICENT = Decimal("0.0001")
_MICROS_PER_DOLLAR = Decimal("1000000")


@dataclass(frozen=True)
class PoliticalPaperTradeEconomics:
    """One conservative, independently rounded ordinary-account fill quote.

    The fee is a Kalshi taker quadratic fee rounded up to a centicent per
    fill.  Ordinary account balances cannot retain sub-cent amounts: entries
    debit upward and exits credit downward.  An order accumulator/rebate is a
    later settlement concern and is intentionally not silently invented here.
    """

    quantity: int
    effective_price: str
    raw_fee: str
    rounded_trade_fee: str
    balance_change_micros: int
    direction: str

    @property
    def debit_micros(self) -> int:
        """Positive reserved debit for entries, zero for an exit."""
        return -self.balance_change_micros if self.balance_change_micros < 0 else 0

    def proportional_basis_micros(
        self, *, total_basis_micros: int, total_quantity: int
    ) -> int:
        """Release integer-micro cost basis exactly proportionally to this fill."""
        if total_basis_micros < 0 or total_quantity <= 0:
            raise ValueError("position basis and quantity must be positive")
        if self.quantity > total_quantity:
            raise ValueError("exit quantity exceeds open position quantity")
        return total_basis_micros * self.quantity // total_quantity


def _price(value: str | Decimal) -> Decimal:
    try:
        price = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("displayed price must be an exact decimal") from exc
    if not price.is_finite() or not Decimal("0") < price < Decimal("1"):
        raise ValueError("displayed price must be strictly between zero and one")
    return price


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return rendered or "0"


class PoliticalExperimentalPaperLedger:
    """Initialize the durable experimental-paper account in integer micros.

    Fill/exit mechanics intentionally do not live here yet.  Keeping account
    initialization idempotent makes later worker restart recovery unable to
    reset capital or duplicate the opening accounting event.
    """

    def __init__(self, *, store: PlatformOpportunityStore, cohort_id: str) -> None:
        if not cohort_id.strip():
            raise ValueError("cohort_id must be non-empty")
        self.store = store
        self.cohort_id = cohort_id

    def initialize(
        self, *, starting_cash_micros: int, initialized_at: datetime
    ) -> dict[str, int | bool]:
        return self.store.initialize_political_experimental_paper_account(
            cohort_id=self.cohort_id,
            starting_cash_micros=starting_cash_micros,
            initialized_at=initialized_at,
        )

    @staticmethod
    def entry_economics(
        *, quantity: int, displayed_ask: str | Decimal
    ) -> PoliticalPaperTradeEconomics:
        """Quote an all-in YES/NO entry at ask plus exactly one cent adversity."""
        return PoliticalExperimentalPaperLedger._trade_economics(
            quantity=quantity,
            displayed_price=displayed_ask,
            direction="entry",
        )

    @staticmethod
    def exit_economics(
        *, quantity: int, displayed_bid: str | Decimal
    ) -> PoliticalPaperTradeEconomics:
        """Quote a conservative exit at bid minus exactly one cent adversity."""
        return PoliticalExperimentalPaperLedger._trade_economics(
            quantity=quantity,
            displayed_price=displayed_bid,
            direction="exit",
        )

    @staticmethod
    def _trade_economics(
        *, quantity: int, displayed_price: str | Decimal, direction: str
    ) -> PoliticalPaperTradeEconomics:
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError(
                "political paper quantity must be a positive whole contract count"
            )
        price = _price(displayed_price)
        effective = price + _ONE_CENT if direction == "entry" else price - _ONE_CENT
        if not Decimal("0") < effective < Decimal("1"):
            raise ValueError("adverse-slippage price is outside contract bounds")
        raw_fee = (
            Decimal(quantity) * Decimal("0.07") * effective * (Decimal("1") - effective)
        )
        rounded_fee = raw_fee.quantize(_ONE_CENTICENT, rounding=ROUND_CEILING)
        if direction == "entry":
            ordinary_balance_amount = (effective * quantity + rounded_fee).quantize(
                _ONE_CENT, rounding=ROUND_CEILING
            )
            balance_change = -int(ordinary_balance_amount * _MICROS_PER_DOLLAR)
        elif direction == "exit":
            ordinary_balance_amount = (effective * quantity - rounded_fee).quantize(
                _ONE_CENT, rounding=ROUND_FLOOR
            )
            balance_change = int(ordinary_balance_amount * _MICROS_PER_DOLLAR)
        else:
            raise ValueError("political paper direction must be entry or exit")
        return PoliticalPaperTradeEconomics(
            quantity=quantity,
            effective_price=_format_decimal(effective),
            raw_fee=_format_decimal(raw_fee),
            rounded_trade_fee=_format_decimal(rounded_fee),
            balance_change_micros=balance_change,
            direction=direction,
        )

    def record_pending_signal(
        self,
        *,
        signal_id: str,
        replay_sequence: int,
        event_id: str,
        milestone_id: str,
        contract_id: str,
        side: str,
        base_lane: str,
        phase: str,
        signal_request_started_at: datetime,
        signal_received_at: datetime,
        expires_at: datetime,
        model_version: str,
        config_hash: str,
        state_hash: str,
        fee_hash: str,
        features: dict[str, Any],
    ) -> bool:
        """Durably hand a qualified reaction to later-book resolution only."""
        return self.store.record_political_experimental_pending_signal(
            signal_id=signal_id,
            cohort_id=self.cohort_id,
            replay_sequence=replay_sequence,
            event_id=event_id,
            milestone_id=milestone_id,
            contract_id=contract_id,
            side=side,
            base_lane=base_lane,
            phase=phase,
            signal_request_started_at=signal_request_started_at,
            signal_received_at=signal_received_at,
            expires_at=expires_at,
            model_version=model_version,
            config_hash=config_hash,
            state_hash=state_hash,
            fee_hash=fee_hash,
            features=features,
        )
