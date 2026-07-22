"""Authoritative, fail-closed cost snapshots for cross-venue execution."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Protocol

from core.cross_platform_arb import MarketPair


class EconomicsUnavailableError(RuntimeError):
    """Current venue economics cannot safely authorize an execution."""


@dataclass(frozen=True)
class PairEconomics:
    """One current, pair-bound snapshot of authoritative fee metadata.

    Polymarket's V2 market metadata supplies the rate and exponent for its
    price-dependent fee curve. Kalshi's IOC taker fee follows its current
    published quadratic schedule.
    """

    pair_id: str
    polymarket_market_id: str
    kalshi_ticker: str
    polymarket_fee_rate: Decimal
    polymarket_fee_exponent: Decimal
    polymarket_taker_only: bool
    polymarket_order_gas_cost: Decimal
    polymarket_gas_source: str
    kalshi_fee_type: str
    kalshi_fee_multiplier: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.pair_id or not self.polymarket_market_id or not self.kalshi_ticker:
            raise ValueError("economics snapshot identity must be complete")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("economics observed_at must be timezone-aware")
        for name in (
            "polymarket_fee_rate",
            "polymarket_fee_exponent",
            "polymarket_order_gas_cost",
            "kalshi_fee_multiplier",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if self.polymarket_fee_rate > 1:
            raise ValueError("Polymarket fee rate exceeds the safety bound")
        if (
            self.polymarket_fee_exponent < 1
            or self.polymarket_fee_exponent > 10
            or self.polymarket_fee_exponent
            != self.polymarket_fee_exponent.to_integral_value()
        ):
            raise ValueError("Polymarket fee exponent must be an integer from 1 to 10")
        if not isinstance(self.polymarket_taker_only, bool):
            raise ValueError("Polymarket taker-only flag must be boolean")
        if not self.polymarket_gas_source.strip():
            raise ValueError("Polymarket gas source must be documented")

    def require_pair(self, pair: MarketPair) -> None:
        if (
            self.pair_id != pair.pair_id
            or self.polymarket_market_id != pair.polymarket_execution_id
            or self.kalshi_ticker != pair.kalshi_ticker
        ):
            raise EconomicsUnavailableError("economics snapshot does not match market pair")

    def require_fresh(
        self,
        *,
        max_age: timedelta,
        now: datetime | None = None,
    ) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        age = checked_at.astimezone(timezone.utc) - self.observed_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > max_age:
            raise EconomicsUnavailableError("authoritative economics snapshot is stale")

    def total_taker_cost(
        self,
        *,
        token: str,
        polymarket_price: Decimal,
        kalshi_price: Decimal,
        size: Decimal,
    ) -> Decimal:
        normalized_token = token.strip().upper()
        if normalized_token not in {"YES", "NO"}:
            raise EconomicsUnavailableError("unsupported outcome token")
        for name, value in (
            ("polymarket_price", polymarket_price),
            ("kalshi_price", kalshi_price),
            ("size", size),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise EconomicsUnavailableError(f"{name} is not authoritative decimal data")
        if not Decimal("0") < polymarket_price < Decimal("1"):
            raise EconomicsUnavailableError("Polymarket price is outside contract bounds")
        if not Decimal("0") < kalshi_price < Decimal("1"):
            raise EconomicsUnavailableError("Kalshi price is outside contract bounds")
        if size <= 0:
            raise EconomicsUnavailableError("execution size must be positive")

        if not self.polymarket_taker_only:
            raise EconomicsUnavailableError(
                "unsupported Polymarket fee mode: maker fees are enabled"
            )
        polymarket_curve = (
            polymarket_price * (Decimal("1") - polymarket_price)
        ) ** self.polymarket_fee_exponent
        polymarket_upper_bound = (
            size * self.polymarket_fee_rate * polymarket_curve
        ).quantize(Decimal("0.00001"), rounding=ROUND_CEILING)
        polymarket_upper_bound += self.polymarket_order_gas_cost

        fee_type = self.kalshi_fee_type.strip().lower()
        if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
            raise EconomicsUnavailableError(
                f"unsupported Kalshi taker fee type: {fee_type or '<empty>'}"
            )
        raw_kalshi = (
            self.kalshi_fee_multiplier
            * Decimal("0.07")
            * size
            * kalshi_price
            * (Decimal("1") - kalshi_price)
        )
        # Kalshi's current fee-rounding contract ceilings each fill's trade fee
        # to one centicent ($0.0001). Balance-precision rounding and the order
        # accumulator are settlement mechanics, not an extra quoted trade fee.
        kalshi_upper_bound = raw_kalshi.quantize(
            Decimal("0.0001"), rounding=ROUND_CEILING
        )
        return polymarket_upper_bound + kalshi_upper_bound

    def net_edge_per_contract(
        self,
        *,
        token: str,
        buy_platform: str,
        buy_price: float,
        sell_price: float,
        size: float,
    ) -> float:
        try:
            buy = Decimal(str(buy_price))
            sell = Decimal(str(sell_price))
            count = Decimal(str(size))
        except InvalidOperation as exc:
            raise EconomicsUnavailableError("opportunity contains invalid decimal data") from exc
        poly_price = buy if buy_platform == "polymarket" else sell
        kalshi_price = buy if buy_platform == "kalshi" else sell
        fees = self.total_taker_cost(
            token=token,
            polymarket_price=poly_price,
            kalshi_price=kalshi_price,
            size=count,
        )
        return float(((sell - buy) * count - fees) / count)


class PolymarketEconomicsClient(Protocol):
    async def get_clob_market_info(self, market_id: str) -> Mapping[str, object]: ...


class KalshiEconomicsClient(Protocol):
    async def get_fee_schedule(self, ticker: str): ...


class AuthoritativeEconomicsProvider:
    """Fetch current public fee metadata for one matched market pair."""

    def __init__(
        self,
        polymarket: PolymarketEconomicsClient,
        kalshi: KalshiEconomicsClient,
    ) -> None:
        self._polymarket = polymarket
        self._kalshi = kalshi

    async def quote_pair(self, pair: MarketPair) -> PairEconomics:
        try:
            market_info, kalshi_schedule = await asyncio.gather(
                self._polymarket.get_clob_market_info(pair.polymarket_execution_id),
                self._kalshi.get_fee_schedule(pair.kalshi_ticker),
            )
            fee_rate, fee_exponent, taker_only = self._parse_polymarket_fee(
                market_info
            )
            fee_type = kalshi_schedule.fee_type
            multiplier = Decimal(str(kalshi_schedule.fee_multiplier))
        except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
            raise EconomicsUnavailableError(
                "authoritative venue fee metadata is invalid"
            ) from exc
        if not math.isfinite(float(multiplier)):
            raise EconomicsUnavailableError("Kalshi fee multiplier is invalid")
        return PairEconomics(
            pair_id=pair.pair_id,
            polymarket_market_id=pair.polymarket_execution_id,
            kalshi_ticker=pair.kalshi_ticker,
            polymarket_fee_rate=fee_rate,
            polymarket_fee_exponent=fee_exponent,
            polymarket_taker_only=taker_only,
            polymarket_order_gas_cost=Decimal("0"),
            polymarket_gas_source="offchain_clob_order",
            kalshi_fee_type=fee_type,
            kalshi_fee_multiplier=multiplier,
            observed_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _parse_polymarket_fee(
        market_info: Mapping[str, object],
    ) -> tuple[Decimal, Decimal, bool]:
        if not isinstance(market_info, Mapping):
            raise EconomicsUnavailableError("Polymarket market metadata is invalid")
        base_fee = market_info.get("tbf")
        details = market_info.get("fd")
        if details is None and base_fee == 0:
            return Decimal("0"), Decimal("1"), True
        if not isinstance(details, Mapping):
            raise EconomicsUnavailableError("Polymarket fee details are unavailable")
        rate_value = details.get("r")
        exponent_value = details.get("e")
        taker_only = details.get("to")
        if (
            isinstance(rate_value, bool)
            or isinstance(exponent_value, bool)
            or not isinstance(taker_only, bool)
        ):
            raise EconomicsUnavailableError("Polymarket fee details are invalid")
        try:
            rate = Decimal(str(rate_value))
            exponent = Decimal(str(exponent_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise EconomicsUnavailableError("Polymarket fee details are invalid") from exc
        if isinstance(base_fee, bool) or not isinstance(base_fee, (int, float, str)):
            raise EconomicsUnavailableError("Polymarket base fee is invalid")
        try:
            basis_rate = Decimal(str(base_fee)) / Decimal("10000")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise EconomicsUnavailableError("Polymarket base fee is invalid") from exc
        if basis_rate != rate:
            raise EconomicsUnavailableError("Polymarket fee metadata is inconsistent")
        return rate, exponent, taker_only
