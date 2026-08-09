"""Capital-constrained, no-mutation political experimental paper accounting.

This focused domain boundary owns political paper semantics while the backing
tables remain in :mod:`utils.platform_opportunity_store`, allowing later book
evidence, signals, fills, and accounting events to share one SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

from utils.platform_opportunity_store import PlatformOpportunityStore
from utils.platform_opportunity_store import ReplayEvidenceIntegrityError

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
    if not (
        rate.is_finite()
        and rate > 0
        and exponent.is_finite()
        and exponent > 0
        and multiplier.is_finite()
        and multiplier >= 0
    ):
        raise ValueError("authoritative replay fee terms are invalid")
    return rate, exponent, multiplier


class PoliticalExperimentalPaperLedger:
    """Authoritative durable accounting boundary for experimental paper only."""

    def __init__(self, *, store: PlatformOpportunityStore, cohort_id: str) -> None:
        if not cohort_id.strip():
            raise ValueError("cohort_id must be non-empty")
        self.store = store
        self.cohort_id = cohort_id

    def initialize(
        self,
        *,
        starting_cash_micros: int,
        initialized_at: datetime,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, int | bool]:
        return self.store.initialize_political_experimental_paper_account(
            cohort_id=self.cohort_id,
            starting_cash_micros=starting_cash_micros,
            initialized_at=initialized_at,
            policy=policy,
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

    @staticmethod
    def _order_level_payloads(
        *,
        levels: list[tuple[str, PoliticalPaperTradeEconomics]],
        direction: str,
    ) -> tuple[list[dict[str, int | str]], int]:
        """Settle ordinary-cent rounding across one simulated order.

        The exchange fee remains rounded per displayed level (to a centicent),
        but the ordinary-account cent residual belongs to the whole order.  A
        cent of accumulated conservative rounding is rebated within that order
        only; a later entry or exit starts with a fresh accumulator.
        """
        accumulator = Decimal("0")
        payloads: list[dict[str, int | str]] = []
        total_balance_change = 0
        for displayed_price, economics in levels:
            effective = Decimal(economics.effective_price)
            fee = Decimal(economics.rounded_trade_fee)
            gross = effective * economics.quantity
            raw_balance = gross + fee if direction == "entry" else gross - fee
            provisional = raw_balance.quantize(
                _ONE_CENT,
                rounding=ROUND_CEILING if direction == "entry" else ROUND_FLOOR,
            )
            residual = (
                provisional - raw_balance
                if direction == "entry"
                else raw_balance - provisional
            )
            before = accumulator
            accumulator += residual
            rebate = (accumulator // _ONE_CENT) * _ONE_CENT
            accumulator -= rebate
            settled = (
                provisional - rebate if direction == "entry" else provisional + rebate
            )
            balance_change = int(
                settled * _MICROS_PER_DOLLAR * (-1 if direction == "entry" else 1)
            )
            total_balance_change += balance_change
            level_name = "displayed_ask" if direction == "entry" else "displayed_bid"
            payloads.append(
                {
                    level_name: displayed_price,
                    "quantity": economics.quantity,
                    "effective_price": economics.effective_price,
                    "raw_fee": economics.raw_fee,
                    "rounded_trade_fee": economics.rounded_trade_fee,
                    "balance_change_micros": balance_change,
                    "ordinary_rounding_micros": int(residual * _MICROS_PER_DOLLAR),
                    "rounding_accumulator_before_micros": int(
                        before * _MICROS_PER_DOLLAR
                    ),
                    "rounding_rebate_micros": int(rebate * _MICROS_PER_DOLLAR),
                    "rounding_accumulator_after_micros": int(
                        accumulator * _MICROS_PER_DOLLAR
                    ),
                }
            )
        return payloads, total_balance_change

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

    def process_observation(self, *, replay_sequence: int) -> list[dict[str, Any]]:
        """Apply deterministic old-position and pending-signal work for one token.

        This is deliberately the runtime-facing transition boundary: callers
        provide only the durable replay sequence.  The method never resolves
        a signal from its own observation, and leaves scoring/new-signal
        creation to happen afterwards at the sealed observation boundary.
        """
        if not isinstance(replay_sequence, int) or isinstance(replay_sequence, bool):
            raise ValueError("political paper replay sequence must be an integer")
        events = {
            int(item["sequence"]): item
            for item in self.store.replay_observation_events(cohort_id=self.cohort_id)
        }
        event = events.get(replay_sequence)
        if event is None:
            raise ValueError(
                "political paper observation requires durable replay evidence"
            )
        contract_id = str(event["contract_id"])
        received_at = datetime.fromisoformat(str(event["received_at"]))
        transitions: list[dict[str, Any]] = []
        # A maximum hold is a frozen part of the political v2 model, not an
        # instruction from the runtime caller.  Keep the legacy focused
        # cohorts on the published ten-minute rule while accepting an explicit
        # immutable policy value for newly initialized accounts.
        policy = self.store.political_experimental_paper_policy(
            cohort_id=self.cohort_id
        )["policy"]
        exit_rule = policy.get("exit_rule", {})
        try:
            maximum_hold_seconds = Decimal(
                str(exit_rule.get("maximum_hold_seconds", "600"))
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "immutable political paper max-hold rule is invalid"
            ) from exc
        if not maximum_hold_seconds.is_finite() or maximum_hold_seconds < 0:
            raise ValueError("immutable political paper max-hold rule is invalid")
        for position in self.store.political_experimental_positions(
            cohort_id=self.cohort_id
        ):
            if str(position["contract_id"]) != contract_id:
                continue
            opened_at = datetime.fromisoformat(str(position["opened_at"]))
            trigger = self._automatic_exit_trigger(
                event=event,
                position=position,
                received_at=received_at,
                opened_at=opened_at,
                maximum_hold_seconds=maximum_hold_seconds,
            )
            if trigger is None:
                continue
            exit_result = self._exit_position(
                position_id=str(position["position_id"]),
                replay_sequence=replay_sequence,
                trigger=trigger,
            )
            transitions.append(exit_result)
        candidates = [
            signal
            for signal in self.store.political_experimental_pending_signals(
                cohort_id=self.cohort_id
            )
            if str(signal["contract_id"]) == contract_id
            and int(signal["replay_sequence"]) < replay_sequence
        ]
        if not candidates:
            return transitions
        # Signal reads are sequence ordered.  Resolving more than the first
        # prior signal from one book would cherry-pick a single observation
        # for multiple pending intents and violates the causal one-transition
        # boundary.
        signal = candidates[0]
        transitions.append(
            self._resolve_pending_signal(
                signal_id=str(signal["signal_id"]), replay_sequence=replay_sequence
            )
        )
        return transitions

    def _automatic_exit_trigger(
        self,
        *,
        event: Mapping[str, Any],
        position: Mapping[str, Any],
        received_at: datetime,
        opened_at: datetime,
        maximum_hold_seconds: Decimal,
    ) -> str | None:
        """Select the frozen exit rule from sealed evidence in precedence order."""
        sticky_trigger = position.get("liquidation_trigger")
        if isinstance(sticky_trigger, str) and sticky_trigger:
            # A forced liquidation can partial-fill against the 10%-of-depth
            # paper convention.  Its remaining quantity must continue to
            # liquidate on later canonical books even if the original market
            # condition has disappeared.
            return sticky_trigger
        # Reviewed-lock provenance was sealed onto this replay token at
        # persistence. A token from the same reviewed event therefore supplies
        # the authoritative boundary without consulting mutable catalog state.
        if (
            event["reviewed_lock_event_id"] is not None
            and str(event["reviewed_lock_event_id"]) == str(position["event_id"])
            and event["reviewed_event_end_at"] is not None
        ):
            try:
                event_end_at = datetime.fromisoformat(
                    str(event["reviewed_event_end_at"])
                )
            except ValueError as exc:
                raise ValueError(
                    "sealed reviewed event boundary must be an ISO datetime"
                ) from exc
            if event_end_at.tzinfo is None:
                raise ValueError(
                    "sealed reviewed event boundary must be timezone-aware"
                )
            if received_at >= event_end_at:
                return "event_boundary"
        if received_at >= opened_at + timedelta(seconds=float(maximum_hold_seconds)):
            return "max_hold_10_minutes"

        book = self.store.replay_book_state(str(event["state_hash"]))
        fee_schedule = self.store.replay_fee_schedule(str(event["fee_hash"]))
        token = book.get(str(position["side"]))
        bids = token.get("bids", []) if isinstance(token, dict) else []
        if bids:
            top_exit = self.exit_economics(
                quantity=1,
                displayed_bid=str(bids[0][0]),
                fee_schedule=fee_schedule,
            )
            basis_per_contract = Decimal(str(position["cost_basis_micros"])) / (
                Decimal(str(position["quantity"])) * _MICROS_PER_DOLLAR
            )
            if basis_per_contract > 0:
                net_return = (
                    Decimal(top_exit.balance_change_micros) / _MICROS_PER_DOLLAR
                    - basis_per_contract
                ) / basis_per_contract
                if net_return <= Decimal("-0.05"):
                    return "hard_stop_net_return_minus_0.05"

        yes = book.get("yes")
        if not isinstance(yes, dict):
            return None
        bid_depth = sum(Decimal(str(level[1])) for level in yes.get("bids", []))
        ask_depth = sum(Decimal(str(level[1])) for level in yes.get("asks", []))
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return None
        imbalance = (bid_depth - ask_depth) / total_depth
        if (str(position["side"]) == "yes" and imbalance <= Decimal("-0.25")) or (
            str(position["side"]) == "no" and imbalance >= Decimal("0.25")
        ):
            return "signal_reversal"
        return None

    def snapshot(self) -> dict[str, Any]:
        """Return the invariant-checked state needed for paper-only reporting."""
        return self.store.political_experimental_paper_snapshot(
            cohort_id=self.cohort_id
        )

    def _replay_evidence_timing_reason(
        self, *, event: Mapping[str, Any], fee_schedule: Mapping[str, Any]
    ) -> str | None:
        """Apply typed, immutable evidence-age limits before a paper fill.

        Older unit cohorts intentionally did not bind timing limits.  Those
        legacy fixtures retain their historical semantics, while every
        runtime-created account has the three typed policy fields below and
        therefore fails closed on incomplete, slow, or stale evidence.
        """
        policy = self.store.political_experimental_paper_policy(
            cohort_id=self.cohort_id
        )["policy"]
        limit_keys = (
            "max_book_request_latency_seconds",
            "max_fee_fetch_latency_seconds",
            "max_fee_schedule_age_seconds",
        )
        if not any(key in policy for key in limit_keys):
            return None
        if not all(key in policy for key in limit_keys):
            return "replay_evidence_invalid"
        try:
            book_limit = Decimal(str(policy[limit_keys[0]]))
            fee_limit = Decimal(str(policy[limit_keys[1]]))
            age_limit = Decimal(str(policy[limit_keys[2]]))
            book_latency_ms = event["book_latency_ms"]
            fee_latency_ms = event["fee_latency_ms"]
            received_at = datetime.fromisoformat(str(event["received_at"]))
            fee_observed_at = datetime.fromisoformat(str(fee_schedule["observed_at"]))
            fee_fetched_at = datetime.fromisoformat(str(fee_schedule["fetched_at"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return "replay_evidence_invalid"
        if not all(
            limit.is_finite() and limit >= 0
            for limit in (book_limit, fee_limit, age_limit)
        ):
            return "replay_evidence_invalid"
        if book_latency_ms is None:
            return "missing_book_timing"
        if fee_latency_ms is None:
            return "missing_fee_metadata"
        if fee_fetched_at < fee_observed_at:
            return "replay_evidence_invalid"
        if Decimal(int(book_latency_ms)) / 1000 > book_limit:
            return "book_request_too_slow"
        if Decimal(int(fee_latency_ms)) / 1000 > fee_limit:
            return "fee_metadata_too_slow"
        if Decimal(str((received_at - fee_observed_at).total_seconds())) > age_limit:
            return "fee_metadata_stale"
        return None

    def _entry_limits(self) -> tuple[int, int, int, Decimal]:
        """Load the immutable entry limits; callers cannot select risk policy.

        Older focused fixtures predate policy binding and retain the published
        control defaults.  A policy that declares any entry-limit field must
        declare the complete set, so a partially persisted runtime policy
        never quietly falls back to a looser limit.
        """
        policy = self.store.political_experimental_paper_policy(
            cohort_id=self.cohort_id
        )["policy"]
        fields = (
            "max_total_reserved_micros",
            "max_position_reserved_micros",
            "max_open_positions",
            "entry_depth_fraction",
        )
        present = [field in policy for field in fields]
        if not any(present):
            return 100_000_000, 25_000_000, 4, Decimal("0.10")
        if not all(present):
            raise ValueError("immutable political paper entry policy is incomplete")
        try:
            total_reserved = int(policy["max_total_reserved_micros"])
            position_reserved = int(policy["max_position_reserved_micros"])
            max_open_positions = int(policy["max_open_positions"])
            depth_fraction = Decimal(str(policy["entry_depth_fraction"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "immutable political paper entry policy is invalid"
            ) from exc
        if (
            total_reserved <= 0
            or position_reserved <= 0
            or max_open_positions <= 0
            or not Decimal("0") < depth_fraction <= Decimal("1")
        ):
            raise ValueError("immutable political paper entry policy is invalid")
        return total_reserved, position_reserved, max_open_positions, depth_fraction

    def _exit_limits(self) -> tuple[Decimal, Decimal]:
        """Load immutable hold and displayed-depth limits for paper exits.

        Runtime-created accounts bind both fields into their canonical policy.
        The legacy focused fixtures intentionally retain the published control
        defaults, but a partially declared exit policy fails closed rather
        than allowing a caller to shorten the hold or widen usable liquidity.
        """
        policy = self.store.political_experimental_paper_policy(
            cohort_id=self.cohort_id
        )["policy"]
        fields = ("minimum_hold_seconds", "entry_depth_fraction")
        present = [field in policy for field in fields]
        if not any(present):
            return Decimal("2"), Decimal("0.10")
        if not all(present):
            raise ValueError("immutable political paper exit policy is incomplete")
        try:
            minimum_hold_seconds = Decimal(str(policy["minimum_hold_seconds"]))
            displayed_depth_fraction = Decimal(str(policy["entry_depth_fraction"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "immutable political paper exit policy is invalid"
            ) from exc
        if (
            not minimum_hold_seconds.is_finite()
            or minimum_hold_seconds < 0
            or not displayed_depth_fraction.is_finite()
            or not Decimal("0") < displayed_depth_fraction <= Decimal("1")
        ):
            raise ValueError("immutable political paper exit policy is invalid")
        return minimum_hold_seconds, displayed_depth_fraction

    def _resolve_pending_signal(
        self,
        *,
        signal_id: str,
        replay_sequence: int,
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
        (
            max_total_reserved_micros,
            max_position_reserved_micros,
            max_open_positions,
            displayed_depth_fraction,
        ) = self._entry_limits()
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
        attempted_at = (
            datetime.fromisoformat(str(event["received_at"]))
            if event is not None
            else (
                datetime.fromisoformat(str(signal["signal_received_at"]))
                if signal is not None
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
        )
        # Store the causal no-fill even when no replay payload can safely be
        # decoded.  Dummy economics are never applied on those paths.
        if signal is None:
            return self.store._resolve_political_experimental_pending_signal(
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
            return self.store._resolve_political_experimental_pending_signal(
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
        timing_reason = self._replay_evidence_timing_reason(
            event=event, fee_schedule=fee_schedule
        )
        if timing_reason is not None:
            return self.store._resolve_political_experimental_pending_signal(
                cohort_id=self.cohort_id,
                signal_id=signal_id,
                replay_sequence=replay_sequence,
                attempted_at=attempted_at,
                quantity=1,
                debit_micros=1,
                max_total_reserved_micros=max_total_reserved_micros,
                max_position_reserved_micros=max_position_reserved_micros,
                max_open_positions=max_open_positions,
                economics={"preflight_reason": timing_reason},
            )
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
            return self.store._resolve_political_experimental_pending_signal(
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
        first_eligible_economics: PoliticalPaperTradeEconomics | None = None
        for price, displayed_size in asks:
            eligible_quantity = int(
                Decimal(str(displayed_size)) * displayed_depth_fraction
            )
            if eligible_quantity <= 0:
                continue
            if first_eligible_economics is None:
                # Keep one authoritative whole-contract quote even when no
                # contract fits the remaining account budget.  Passing that
                # quote through the atomic store transition lets its existing
                # cap/cash/open-position precedence record the truthful
                # blocker instead of misreporting a capital shortfall as a
                # fractional-depth failure.
                first_eligible_economics = self.entry_economics(
                    quantity=1,
                    displayed_ask=str(price),
                    fee_schedule=fee_schedule,
                )
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
            if first_eligible_economics is not None:
                return self.store._resolve_political_experimental_pending_signal(
                    cohort_id=self.cohort_id,
                    signal_id=signal_id,
                    replay_sequence=replay_sequence,
                    attempted_at=attempted_at,
                    quantity=1,
                    debit_micros=first_eligible_economics.debit_micros,
                    max_total_reserved_micros=max_total_reserved_micros,
                    max_position_reserved_micros=max_position_reserved_micros,
                    max_open_positions=max_open_positions,
                    economics={
                        "sizing_context": {
                            "minimum_whole_contract_debit_micros": (
                                first_eligible_economics.debit_micros
                            )
                        },
                    },
                )
            return self.store._resolve_political_experimental_pending_signal(
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
        levels, balance_change_micros = self._order_level_payloads(
            levels=level_economics, direction="entry"
        )
        debit_micros = -balance_change_micros
        return self.store._resolve_political_experimental_pending_signal(
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

    def _exit_position(
        self,
        *,
        position_id: str,
        replay_sequence: int,
        trigger: str | None = None,
    ) -> dict[str, Any]:
        """Close as much of an open position as the later canonical bids allow.

        The replay receipt, rather than a caller clock, is the paper exit
        time.  Each displayed bid contributes at most the configured whole
        contract fraction and all economics remain independently conservative.
        """
        minimum_hold_seconds, displayed_depth_fraction = self._exit_limits()
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
        if received_at < opened_at + timedelta(seconds=float(minimum_hold_seconds)):
            raise ValueError("political paper minimum hold has not elapsed")
        try:
            book = self.store.replay_book_state(str(event["state_hash"]))
            fee_schedule = self.store.replay_fee_schedule(str(event["fee_hash"]))
        except (KeyError, ReplayEvidenceIntegrityError, ValueError):
            return self.store._record_political_experimental_no_exit(
                cohort_id=self.cohort_id,
                position_id=position_id,
                replay_sequence=replay_sequence,
                attempted_at=received_at,
                reason="replay_evidence_invalid",
                trigger=trigger,
            )
        timing_reason = self._replay_evidence_timing_reason(
            event=event, fee_schedule=fee_schedule
        )
        if timing_reason is not None:
            return self.store._record_political_experimental_no_exit(
                cohort_id=self.cohort_id,
                position_id=position_id,
                replay_sequence=replay_sequence,
                attempted_at=received_at,
                reason=timing_reason,
                trigger=trigger,
            )
        token = book.get(str(position["side"]))
        bids = token.get("bids", []) if isinstance(token, dict) else []
        if not bids:
            return self.store._record_political_experimental_no_exit(
                cohort_id=self.cohort_id,
                position_id=position_id,
                replay_sequence=replay_sequence,
                attempted_at=received_at,
                reason="empty_executable_bids",
                trigger=trigger,
            )
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
            return self.store._record_political_experimental_no_exit(
                cohort_id=self.cohort_id,
                position_id=position_id,
                replay_sequence=replay_sequence,
                attempted_at=received_at,
                reason="fractional_or_zero_executable_depth",
                trigger=trigger,
            )
        quantity = sum(item.quantity for _, item in level_economics)
        levels, credit_micros = self._order_level_payloads(
            levels=level_economics, direction="exit"
        )
        basis_release_micros = (
            int(position["cost_basis_micros"]) * quantity // int(position["quantity"])
        )
        return self.store.exit_political_experimental_position(
            cohort_id=self.cohort_id,
            position_id=position_id,
            replay_sequence=replay_sequence,
            attempted_at=received_at,
            quantity=quantity,
            credit_micros=credit_micros,
            basis_release_micros=basis_release_micros,
            economics={
                "levels": levels,
                **({"exit_trigger": trigger} if trigger is not None else {}),
            },
        )
