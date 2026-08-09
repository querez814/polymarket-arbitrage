"""Capital-constrained, no-mutation political experimental paper accounting.

This focused domain boundary owns political paper semantics while the backing
tables remain in :mod:`utils.platform_opportunity_store`, allowing later book
evidence, signals, fills, and accounting events to share one SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def _authoritative_kalshi_fee_terms(
    fee_schedule: dict[str, Any],
) -> tuple[Decimal, Decimal, Decimal]:
    """Return replay-proven Kalshi fee terms without guessing a fee curve.

    Paper fills must consume the exact fee schedule retained beside their book
    evidence.  In particular, the historical ``0.07`` convention is not a
    fallback: the authoritative payload supplies the rate, exponent, and
    multiplier explicitly.  Freshness relative to a prospective fill is
    checked by the later causal resolver; this boundary verifies that the
    retained schedule itself contains observed and fetched provenance.
    """
    if not isinstance(fee_schedule, dict):
        raise ValueError("authoritative replay fee schedule is required")
    if fee_schedule.get("schema_version") != 1:
        raise ValueError("unsupported authoritative replay fee schema")
    if fee_schedule.get("venue") != "kalshi":
        raise ValueError("political paper requires an authoritative Kalshi fee")
    if fee_schedule.get("fee_type") != "kalshi_quadratic":
        raise ValueError("unsupported authoritative Kalshi fee type")
    if not all(
        fee_schedule.get(key) is not None for key in ("observed_at", "fetched_at")
    ):
        raise ValueError("authoritative replay fee timing is required")
    try:
        observed_at = datetime.fromisoformat(str(fee_schedule["observed_at"]))
        fetched_at = datetime.fromisoformat(str(fee_schedule["fetched_at"]))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "authoritative replay fee timing must be ISO datetimes"
        ) from exc
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if fetched_at.astimezone(timezone.utc) < observed_at.astimezone(timezone.utc):
        raise ValueError("authoritative replay fee was fetched before observation")
    try:
        rate = Decimal(str(fee_schedule["rate"]))
        exponent = Decimal(str(fee_schedule["exponent"]))
        multiplier = Decimal(str(fee_schedule["multiplier"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "authoritative replay fee terms must be exact decimals"
        ) from exc
    if not all(
        value.is_finite() and value > 0 for value in (rate, exponent, multiplier)
    ):
        raise ValueError("authoritative replay fee terms must be positive")
    return rate, exponent, multiplier


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
        *,
        quantity: int,
        displayed_ask: str | Decimal,
        fee_schedule: dict[str, Any],
    ) -> PoliticalPaperTradeEconomics:
        """Quote an all-in YES/NO entry at ask plus exactly one cent adversity."""
        return PoliticalExperimentalPaperLedger._trade_economics(
            quantity=quantity,
            displayed_price=displayed_ask,
            direction="entry",
            fee_schedule=fee_schedule,
        )

    @staticmethod
    def exit_economics(
        *,
        quantity: int,
        displayed_bid: str | Decimal,
        fee_schedule: dict[str, Any],
    ) -> PoliticalPaperTradeEconomics:
        """Quote a conservative exit at bid minus exactly one cent adversity."""
        return PoliticalExperimentalPaperLedger._trade_economics(
            quantity=quantity,
            displayed_price=displayed_bid,
            direction="exit",
            fee_schedule=fee_schedule,
        )

    @staticmethod
    def _trade_economics(
        *,
        quantity: int,
        displayed_price: str | Decimal,
        direction: str,
        fee_schedule: dict[str, Any],
    ) -> PoliticalPaperTradeEconomics:
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError(
                "political paper quantity must be a positive whole contract count"
            )
        price = _price(displayed_price)
        effective = price + _ONE_CENT if direction == "entry" else price - _ONE_CENT
        if not Decimal("0") < effective < Decimal("1"):
            raise ValueError("adverse-slippage price is outside contract bounds")
        rate, exponent, multiplier = _authoritative_kalshi_fee_terms(fee_schedule)
        raw_fee = (
            Decimal(quantity)
            * multiplier
            * rate
            * (effective * (Decimal("1") - effective)) ** exponent
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

    def expire_pending(self, *, as_of: datetime) -> list[dict[str, Any]]:
        """Durably expire causal signals whose later-book window has elapsed."""
        return self.store.expire_political_experimental_pending_signals(
            cohort_id=self.cohort_id,
            as_of=as_of,
        )

    def resolve_pending_signal(
        self,
        *,
        signal_id: str,
        replay_sequence: int,
        attempted_at: datetime,
        max_total_reserved_micros: int = 100_000_000,
        max_position_reserved_micros: int = 25_000_000,
        max_open_positions: int = 4,
        displayed_depth_fraction: Decimal = Decimal("0.10"),
    ) -> dict[str, Any]:
        """Open from one strictly later replay book, never from the signal book.

        The resolver is intentionally supplied only a replay sequence.  It
        rehydrates that sequence's exact persisted book and fee schedule, so a
        runtime caller cannot sneak raw adapter depth into a paper fill.
        Every qualifying canonical ask can contribute only its configured
        whole-contract fraction.  Per-level pricing and balance rounding are
        retained with the one causal fill attempt; callers never supply a
        synthetic aggregate price or depth total.
        """
        if not Decimal("0") < displayed_depth_fraction <= Decimal("1"):
            raise ValueError("displayed depth fraction must be in (0, 1]")
        signals = {
            item["signal_id"]: item
            for item in self.store.political_experimental_pending_signals(
                cohort_id=self.cohort_id
            )
        }
        signal = signals.get(signal_id)
        events = {
            int(item["sequence"]): item
            for item in self.store.replay_observation_events(cohort_id=self.cohort_id)
        }
        event = events.get(replay_sequence)
        # Store the causal no-fill even when no replay payload can safely be
        # decoded.  Dummy economics are never applied on those paths.
        if signal is None:
            return self.store.resolve_political_experimental_pending_signal(
                cohort_id=self.cohort_id,
                signal_id=signal_id,
                replay_sequence=replay_sequence,
                attempted_at=attempted_at,
                quantity=1,
                debit_micros=1,
                max_total_reserved_micros=max_total_reserved_micros,
                max_position_reserved_micros=max_position_reserved_micros,
                max_open_positions=max_open_positions,
                economics={"preflight_reason": "missing_signal"},
            )
        if event is None:
            return self.store.resolve_political_experimental_pending_signal(
                cohort_id=self.cohort_id,
                signal_id=signal_id,
                replay_sequence=replay_sequence,
                attempted_at=attempted_at,
                quantity=1,
                debit_micros=1,
                max_total_reserved_micros=max_total_reserved_micros,
                max_position_reserved_micros=max_position_reserved_micros,
                max_open_positions=max_open_positions,
                economics={},
            )
        book = self.store.replay_book_state(str(event["state_hash"]))
        fee_schedule = self.store.replay_fee_schedule(str(event["fee_hash"]))
        account = self.store.political_experimental_paper_account(
            cohort_id=self.cohort_id
        )
        available_debit_micros = min(
            max_position_reserved_micros,
            max_total_reserved_micros - int(account["reserved_micros"]),
            int(account["cash_micros"]),
        )
        token = book.get(str(signal["side"]))
        asks = token.get("asks", []) if isinstance(token, dict) else []
        if not asks:
            # Let the store record the causal outcome; a zero quantity is never
            # passed across the accounting boundary.
            return self.store.resolve_political_experimental_pending_signal(
                cohort_id=self.cohort_id,
                signal_id=signal_id,
                replay_sequence=replay_sequence,
                attempted_at=attempted_at,
                quantity=1,
                debit_micros=max_position_reserved_micros + 1,
                max_total_reserved_micros=max_total_reserved_micros,
                max_position_reserved_micros=max_position_reserved_micros,
                max_open_positions=max_open_positions,
                economics={"preflight_reason": "empty_executable_asks"},
            )
        level_economics: list[tuple[str, PoliticalPaperTradeEconomics]] = []
        unconsumed_levels: list[dict[str, int | str]] = []
        for price, displayed_size in asks:
            eligible_quantity = int(
                Decimal(str(displayed_size)) * displayed_depth_fraction
            )
            if eligible_quantity <= 0:
                continue
            # Take the largest whole-contract prefix of this displayed level
            # that remains affordable.  Pricing a whole level then rejecting
            # its aggregate debit would falsely make deeper books less
            # executable than shallow ones.
            low, high = 0, eligible_quantity
            while low < high:
                candidate = (low + high + 1) // 2
                candidate_economics = self.entry_economics(
                    quantity=candidate,
                    displayed_ask=str(price),
                    fee_schedule=fee_schedule,
                )
                if candidate_economics.debit_micros <= available_debit_micros:
                    low = candidate
                else:
                    high = candidate - 1
            quantity = low
            if quantity <= 0:
                unconsumed_levels.append(
                    {
                        "displayed_ask": str(price),
                        "eligible_quantity": eligible_quantity,
                        "unconsumed_quantity": eligible_quantity,
                    }
                )
                break
            economics = self.entry_economics(
                quantity=quantity,
                displayed_ask=str(price),
                fee_schedule=fee_schedule,
            )
            level_economics.append((str(price), economics))
            available_debit_micros -= economics.debit_micros
            if quantity < eligible_quantity:
                unconsumed_levels.append(
                    {
                        "displayed_ask": str(price),
                        "eligible_quantity": eligible_quantity,
                        "unconsumed_quantity": eligible_quantity - quantity,
                    }
                )
                break
        if not level_economics:
            return self.store.resolve_political_experimental_pending_signal(
                cohort_id=self.cohort_id,
                signal_id=signal_id,
                replay_sequence=replay_sequence,
                attempted_at=attempted_at,
                quantity=1,
                debit_micros=max_position_reserved_micros + 1,
                max_total_reserved_micros=max_total_reserved_micros,
                max_position_reserved_micros=max_position_reserved_micros,
                max_open_positions=max_open_positions,
                economics={"preflight_reason": "fractional_or_zero_executable_depth"},
            )
        quantity = sum(economics.quantity for _, economics in level_economics)
        debit_micros = sum(economics.debit_micros for _, economics in level_economics)
        levels = [
            {
                "displayed_ask": displayed_ask,
                "quantity": economics.quantity,
                "effective_price": economics.effective_price,
                "raw_fee": economics.raw_fee,
                "rounded_trade_fee": economics.rounded_trade_fee,
                "balance_change_micros": economics.balance_change_micros,
            }
            for displayed_ask, economics in level_economics
        ]
        return self.store.resolve_political_experimental_pending_signal(
            cohort_id=self.cohort_id,
            signal_id=signal_id,
            replay_sequence=replay_sequence,
            attempted_at=attempted_at,
            quantity=quantity,
            debit_micros=debit_micros,
            max_total_reserved_micros=max_total_reserved_micros,
            max_position_reserved_micros=max_position_reserved_micros,
            max_open_positions=max_open_positions,
            economics={
                "levels": levels,
                "unconsumed_levels": unconsumed_levels,
                "balance_change_micros": -debit_micros,
            },
        )

    def exit_position(
        self,
        *,
        position_id: str,
        replay_sequence: int,
        minimum_hold_seconds: int = 2,
        displayed_depth_fraction: Decimal = Decimal("0.10"),
    ) -> dict[str, Any]:
        """Close as much of an open position as the later canonical bids allow.

        The replay receipt, rather than a caller clock, is the paper exit
        time.  Each displayed bid contributes at most the configured whole
        contract fraction and all economics remain independently conservative.
        """
        if minimum_hold_seconds < 0 or not Decimal(
            "0"
        ) < displayed_depth_fraction <= Decimal("1"):
            raise ValueError("political paper exit limits are invalid")
        positions = {
            str(item["position_id"]): item
            for item in self.store.political_experimental_positions(
                cohort_id=self.cohort_id
            )
        }
        position = positions.get(position_id)
        events = {
            int(item["sequence"]): item
            for item in self.store.replay_observation_events(cohort_id=self.cohort_id)
        }
        event = events.get(replay_sequence)
        if position is None:
            raise ValueError("political paper exit requires an open position")
        if event is None:
            raise ValueError("political paper exit requires a durable replay event")
        received_at = datetime.fromisoformat(str(event["received_at"]))
        opened_at = datetime.fromisoformat(str(position["opened_at"]))
        if received_at < opened_at + timedelta(seconds=minimum_hold_seconds):
            raise ValueError("political paper minimum hold has not elapsed")
        book = self.store.replay_book_state(str(event["state_hash"]))
        fee_schedule = self.store.replay_fee_schedule(str(event["fee_hash"]))
        token = book.get(str(position["side"]))
        bids = token.get("bids", []) if isinstance(token, dict) else []
        remaining = int(position["quantity"])
        level_economics: list[tuple[str, PoliticalPaperTradeEconomics]] = []
        for price, displayed_size in bids:
            quantity = min(
                remaining,
                int(Decimal(str(displayed_size)) * displayed_depth_fraction),
            )
            if quantity <= 0:
                continue
            economics = self.exit_economics(
                quantity=quantity,
                displayed_bid=str(price),
                fee_schedule=fee_schedule,
            )
            level_economics.append((str(price), economics))
            remaining -= quantity
            if remaining == 0:
                break
        if not level_economics:
            raise ValueError(
                "political paper exit has no whole-contract executable bids"
            )
        quantity = sum(item.quantity for _, item in level_economics)
        credit_micros = sum(item.balance_change_micros for _, item in level_economics)
        basis_release_micros = (
            int(position["cost_basis_micros"]) * quantity // int(position["quantity"])
        )
        levels = [
            {
                "displayed_bid": displayed_bid,
                "quantity": economics.quantity,
                "effective_price": economics.effective_price,
                "raw_fee": economics.raw_fee,
                "rounded_trade_fee": economics.rounded_trade_fee,
                "balance_change_micros": economics.balance_change_micros,
            }
            for displayed_bid, economics in level_economics
        ]
        return self.store.exit_political_experimental_position(
            cohort_id=self.cohort_id,
            position_id=position_id,
            replay_sequence=replay_sequence,
            attempted_at=received_at,
            quantity=quantity,
            credit_micros=credit_micros,
            basis_release_micros=basis_release_micros,
            economics={"levels": levels},
        )
