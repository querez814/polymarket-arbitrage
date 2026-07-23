"""Detection-only Polymarket multi-market arbitrage.

Arbitrary topical similarity does not establish a payout partition. This module only
evaluates venue-declared negative-risk event groups, where exactly one YES outcome is
expected to settle. It emits observations for review and never constructs orders.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from polymarket_client.models import MarketState


@dataclass(frozen=True)
class CombinatorialLeg:
    market_id: str
    outcome_label: str
    token: str
    price: float
    visible_size: float


@dataclass(frozen=True)
class CombinatorialOpportunity:
    opportunity_id: str
    event_id: str
    event_title: str
    kind: str
    total_price: float
    guaranteed_payout: float
    gross_edge: float
    net_edge: float
    max_size: float
    legs: tuple[CombinatorialLeg, ...]
    detected_at_utc: str


class SamePlatformArbitrageDetector:
    """Group event states in O(n), then evaluate only explicit payout partitions."""

    def __init__(
        self,
        *,
        min_edge: float,
        taker_fee_rate: float = 0.015,
        cooldown_seconds: float = 30.0,
        max_group_size: int = 50,
        clock: Callable[[], datetime] | None = None,
    ):
        if not 0 <= min_edge <= 1 or not 0 <= taker_fee_rate <= 1:
            raise ValueError("edge and fee rates must be in [0, 1]")
        if cooldown_seconds <= 0 or max_group_size < 2:
            raise ValueError("detector limits must be positive")
        self.min_edge = min_edge
        self.taker_fee_rate = taker_fee_rate
        self.cooldown_seconds = cooldown_seconds
        self.max_group_size = max_group_size
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_emitted: dict[str, datetime] = {}

    def detect(
        self, states: Mapping[str, MarketState]
    ) -> list[CombinatorialOpportunity]:
        groups: dict[str, list[MarketState]] = {}
        for state in states.values():
            market = state.market
            if not market.active or market.closed or not market.negative_risk:
                continue
            if not market.event_id:
                continue
            groups.setdefault(market.event_id, []).append(state)

        opportunities: list[CombinatorialOpportunity] = []
        for event_id, group in groups.items():
            if not 2 <= len(group) <= self.max_group_size:
                continue
            opportunity = self._yes_bundle(event_id, group)
            if opportunity and self._cooldown_allows(opportunity):
                opportunities.append(opportunity)
        return opportunities

    def _yes_bundle(
        self, event_id: str, group: list[MarketState]
    ) -> CombinatorialOpportunity | None:
        legs: list[CombinatorialLeg] = []
        for state in group:
            price = state.order_book.best_ask_yes
            size = state.order_book.yes.best_ask_size
            if price is None or size is None or not math.isfinite(price + size):
                return None
            if not 0 < price < 1 or size <= 0:
                return None
            legs.append(
                CombinatorialLeg(
                    market_id=state.market.market_id,
                    outcome_label=state.market.outcome_label or state.market.question,
                    token="YES",
                    price=price,
                    visible_size=size,
                )
            )
        legs.sort(key=lambda leg: leg.market_id)
        total = sum(leg.price for leg in legs)
        gross_edge = 1.0 - total
        fees = total * self.taker_fee_rate
        net_edge = gross_edge - fees
        if net_edge < self.min_edge:
            return None
        fingerprint = hashlib.sha256(
            f"{event_id}|yes|{'|'.join(leg.market_id for leg in legs)}".encode()
        ).hexdigest()[:16]
        now = self._now()
        return CombinatorialOpportunity(
            opportunity_id=f"combo_{fingerprint}",
            event_id=event_id,
            event_title=group[0].market.event_title,
            kind="negative_risk_yes_bundle",
            total_price=total,
            guaranteed_payout=1.0,
            gross_edge=gross_edge,
            net_edge=net_edge,
            max_size=min(leg.visible_size for leg in legs),
            legs=tuple(legs),
            detected_at_utc=now.isoformat().replace("+00:00", "Z"),
        )

    def _cooldown_allows(self, opportunity: CombinatorialOpportunity) -> bool:
        now = self._now()
        last = self._last_emitted.get(opportunity.opportunity_id)
        if last and (now - last).total_seconds() < self.cooldown_seconds:
            return False
        self._last_emitted[opportunity.opportunity_id] = now
        return True

    def _now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
