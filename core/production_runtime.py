"""Owned production lifecycle for locked cross-venue arbitrage execution."""

from __future__ import annotations

import asyncio
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Awaitable, Callable, Mapping, Protocol

from core.cross_platform_arb import CrossPlatformOpportunity, MarketPair
from core.execution_economics import EconomicsUnavailableError, PairEconomics
from core.execution_journal import ExecutionJournal
from core.execution_recovery import (
    AuthoritativeVenueReader,
    ExecutionStartupGate,
    RecoveryBlockedError,
)
from core.operations import (
    AdmissionLimitError,
    PersistentOperatorControls,
    TradingHaltedError,
)
from core.two_leg_execution import (
    ExecutionPhase,
    LegIntent,
    LegSide,
    TwoLegExecution,
)
from core.venue_execution import LockedArbitrageExecutor, VenueExecutionAdapter


class RuntimeNotReadyError(RuntimeError):
    """A required production gate is unavailable or closed."""


class PrivateLifecycleStream(Protocol):
    async def run(
        self,
        *,
        on_event: Callable[[Any], Awaitable[None]],
        reconcile_rest: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        market_tickers: tuple[str, ...] = (),
        max_reconnect_delay: float = 30.0,
    ) -> None: ...


class EconomicsProvider(Protocol):
    async def quote_pair(self, pair: MarketPair) -> PairEconomics: ...


class OpportunityDetector(Protocol):
    def check_arbitrage(
        self,
        pair: MarketPair,
        polymarket_book: Any,
        kalshi_book: Any,
        *,
        economics: PairEconomics,
    ) -> CrossPlatformOpportunity | None: ...


@dataclass(frozen=True)
class RuntimeStatus:
    started: bool
    recovery_ready: bool
    private_stream_ready: bool
    halted: bool
    ready: bool
    last_error: str


@dataclass(frozen=True)
class RuntimeEvaluation:
    economics: PairEconomics
    opportunity: CrossPlatformOpportunity | None
    execution: TwoLegExecution | None


class ProductionArbitrageRuntime:
    """Own recovery, stream callbacks, plan admission, and execution.

    Every new plan performs a fresh account-wide authoritative reconciliation.
    A private lifecycle stream must complete its subscribe-before-REST handshake
    before production admission opens. Any residual, ambiguous state, or
    unexpected lifecycle failure durably trips operator controls.
    """

    REQUIRED_VENUES = frozenset({"polymarket", "kalshi"})

    def __init__(
        self,
        *,
        journal: ExecutionJournal,
        adapters: Mapping[str, VenueExecutionAdapter],
        controls: PersistentOperatorControls,
        min_net_edge: float,
        max_order_notional: float,
        economics_max_age: timedelta,
        max_order_attempts_per_minute: int = 10,
        max_daily_order_attempts: int = 100,
        max_strategy_exposure: float = 1e18,
        max_global_exposure: float = 1e18,
        max_position_per_market: float = 1e18,
        max_open_positions: int = 1_000_000,
        market_whitelist: tuple[str, ...] = (),
        market_blacklist: tuple[str, ...] = (),
        opportunity_max_age: timedelta = timedelta(seconds=5),
        require_collateral: bool = False,
        economics_provider: EconomicsProvider | None = None,
        detector: OpportunityDetector | None = None,
        private_stream: PrivateLifecycleStream | None = None,
        private_stream_markets: tuple[str, ...] = (),
        require_private_stream: bool = True,
    ) -> None:
        normalized = {name.strip().lower(): adapter for name, adapter in adapters.items()}
        if set(normalized) != set(self.REQUIRED_VENUES):
            raise ValueError("production runtime requires exactly Polymarket and Kalshi")
        if not math.isfinite(min_net_edge) or min_net_edge < 0:
            raise ValueError("min_net_edge must be finite and non-negative")
        if not math.isfinite(max_order_notional) or max_order_notional <= 0:
            raise ValueError("max_order_notional must be finite and positive")
        if economics_max_age <= timedelta(0):
            raise ValueError("economics_max_age must be positive")
        for name, value in (
            ("max_strategy_exposure", max_strategy_exposure),
            ("max_global_exposure", max_global_exposure),
            ("max_position_per_market", max_position_per_market),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if (
            isinstance(max_open_positions, bool)
            or not isinstance(max_open_positions, int)
            or max_open_positions <= 0
        ):
            raise ValueError("max_open_positions must be a positive integer")
        if opportunity_max_age <= timedelta(0):
            raise ValueError("opportunity_max_age must be positive")
        for name, value in (
            ("max_order_attempts_per_minute", max_order_attempts_per_minute),
            ("max_daily_order_attempts", max_daily_order_attempts),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 2:
                raise ValueError(f"{name} must be an integer of at least 2")
        if require_private_stream and private_stream is None:
            raise ValueError("production runtime requires a private lifecycle stream")
        self._journal = journal
        self._adapters = normalized
        self._controls = controls
        self._min_net_edge = min_net_edge
        self._max_order_notional = max_order_notional
        self._economics_max_age = economics_max_age
        self._max_order_attempts_per_minute = max_order_attempts_per_minute
        self._max_daily_order_attempts = max_daily_order_attempts
        self._max_strategy_exposure = max_strategy_exposure
        self._max_global_exposure = max_global_exposure
        self._max_position_per_market = max_position_per_market
        self._max_open_positions = max_open_positions
        self._market_whitelist = frozenset(market_whitelist)
        self._market_blacklist = frozenset(market_blacklist)
        self._opportunity_max_age = opportunity_max_age
        self._require_collateral = require_collateral
        if require_collateral and any(
            not callable(getattr(adapter, "available_collateral", None))
            for adapter in normalized.values()
        ):
            raise ValueError("production adapters must provide authoritative collateral")
        self._economics_provider = economics_provider
        self._detector = detector
        self._private_stream = private_stream
        self._private_stream_markets = private_stream_markets
        self._require_private_stream = require_private_stream
        self._executor = LockedArbitrageExecutor(journal, normalized)
        self._lock = asyncio.Lock()
        self._stream_task: asyncio.Task[None] | None = None
        self._started = False
        self._recovery_ready = False
        self._stream_ready = not require_private_stream
        self._last_error = ""

    async def start(self) -> None:
        if self._started:
            return
        await self._controls.startup_halt()
        try:
            await self._refresh_recovery()
        except Exception as exc:
            self._last_error = type(exc).__name__
            await self._controls.trip(
                reason="startup authoritative recovery failed",
                source="production_runtime",
            )
            raise RuntimeNotReadyError("startup authoritative recovery failed") from exc
        self._started = True
        if self._private_stream is not None:
            self._stream_task = asyncio.create_task(
                self._run_private_stream(), name="kalshi-private-lifecycle"
            )

    async def stop(self) -> None:
        self._started = False
        self._recovery_ready = False
        self._stream_ready = not self._require_private_stream
        task = self._stream_task
        self._stream_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def status(self) -> RuntimeStatus:
        operator = self._controls.status()
        ready = (
            self._started
            and self._recovery_ready
            and self._stream_ready
            and not operator.halted
            and not self._last_error
        )
        return RuntimeStatus(
            started=self._started,
            recovery_ready=self._recovery_ready,
            private_stream_ready=self._stream_ready,
            halted=operator.halted,
            ready=ready,
            last_error=self._last_error,
        )

    def operator_status(self, token: str):
        return self._controls.authenticated_status(token)

    async def panic(self, token: str, *, reason: str):
        return await self._controls.panic(token, reason=reason)

    async def resume(self, token: str, *, reason: str):
        async with self._lock:
            if not self._started:
                raise RuntimeNotReadyError("production runtime has not started")
            if self._require_private_stream and not self._stream_ready:
                raise RuntimeNotReadyError("private lifecycle stream is not reconciled")
            if self._last_error:
                raise RuntimeNotReadyError(
                    f"runtime requires operator reconciliation: {self._last_error}"
                )
            await self._refresh_recovery()
            return await self._controls.resume(token, reason=reason)

    async def execute_opportunity(
        self,
        opportunity: CrossPlatformOpportunity,
        economics: PairEconomics,
    ) -> TwoLegExecution:
        async with self._lock:
            self._require_ready()
            try:
                economics.require_pair(opportunity.market_pair)
                economics.require_fresh(max_age=self._economics_max_age)
            except EconomicsUnavailableError as exc:
                raise RuntimeNotReadyError(str(exc)) from exc

            opportunity = _normalize_opportunity(opportunity)
            self._require_opportunity_fresh(opportunity)
            self._require_market_authorized(opportunity.market_pair)
            size = opportunity.suggested_size
            if not math.isfinite(size) or size <= 0:
                raise RuntimeNotReadyError("opportunity size is invalid")
            for price in (opportunity.buy_price, opportunity.sell_price):
                notional = price * size
                if not math.isfinite(notional) or notional > self._max_order_notional:
                    raise RuntimeNotReadyError("opportunity exceeds per-order notional limit")
            net_edge = economics.net_edge_per_contract(
                token=opportunity.token,
                buy_platform=opportunity.buy_platform,
                buy_price=opportunity.buy_price,
                sell_price=opportunity.sell_price,
                size=size,
            )
            if net_edge < self._min_net_edge:
                raise RuntimeNotReadyError("authoritative after-cost edge is below threshold")

            execution, first_leg_id = _build_execution(opportunity)
            self._require_exposure_capacity(execution)
            try:
                # Reserve both possible IOC submissions conservatively before a
                # plan or venue read exists. Failed admissions/reconciliations
                # intentionally consume capacity; the durable budget survives
                # process restarts and never understates possible attempts.
                self._controls.reserve_order_attempts(
                    2,
                    max_per_minute=self._max_order_attempts_per_minute,
                    max_per_day=self._max_daily_order_attempts,
                )
            except AdmissionLimitError as exc:
                raise RuntimeNotReadyError(str(exc)) from exc
            try:
                gate = await self._refresh_recovery()
            except Exception as exc:
                self._recovery_ready = False
                self._last_error = type(exc).__name__
                await self._controls.trip(
                    reason="authoritative recovery failed before locked execution",
                    source="production_runtime",
                )
                raise
            try:
                economics.require_fresh(max_age=self._economics_max_age)
            except EconomicsUnavailableError as exc:
                raise RuntimeNotReadyError(str(exc)) from exc
            self._require_opportunity_fresh(opportunity)
            if self._require_collateral:
                await self._require_collateral_capacity(execution)
            try:
                gate.persist_execution_plan(execution)
                result = await self._executor.execute(
                    execution.execution_id, first_leg_id=first_leg_id
                )
            except Exception as exc:
                self._recovery_ready = False
                self._last_error = type(exc).__name__
                await self._controls.trip(
                    reason="locked execution failed and requires reconciliation",
                    source="production_runtime",
                )
                raise

            if result.phase in {
                ExecutionPhase.RECOVERY_REQUIRED,
                ExecutionPhase.RESIDUAL_EXPOSURE,
            }:
                self._recovery_ready = False
                self._last_error = result.phase.value
                await self._controls.trip(
                    reason=f"execution ended in {result.phase.value}",
                    source="production_runtime",
                )
            else:
                # A completed plan consumed the current recovery proof. The next
                # plan must establish a new proof before journal admission.
                self._recovery_ready = False
            return result

    async def evaluate_pair(
        self,
        pair: MarketPair,
        polymarket_book: Any,
        kalshi_book: Any,
    ) -> RuntimeEvaluation:
        """Fetch current costs, detect one locked edge, and execute it if present."""
        self._require_ready()
        if self._economics_provider is None or self._detector is None:
            raise RuntimeNotReadyError(
                "production runtime does not own economics and detection"
            )
        try:
            economics = await self._economics_provider.quote_pair(pair)
            economics.require_pair(pair)
            economics.require_fresh(max_age=self._economics_max_age)
        except EconomicsUnavailableError as exc:
            raise RuntimeNotReadyError(str(exc)) from exc
        except Exception as exc:
            raise RuntimeNotReadyError(
                "authoritative venue economics are unavailable"
            ) from exc

        opportunity = self._detector.check_arbitrage(
            pair,
            polymarket_book,
            kalshi_book,
            economics=economics,
        )
        if opportunity is None:
            return RuntimeEvaluation(economics, None, None)
        opportunity = _normalize_opportunity(opportunity)
        execution = await self.execute_opportunity(opportunity, economics)
        return RuntimeEvaluation(economics, opportunity, execution)

    def _require_ready(self) -> None:
        if not self._started:
            raise RuntimeNotReadyError("production runtime has not started")
        if self._require_private_stream and not self._stream_ready:
            raise RuntimeNotReadyError("private lifecycle stream is not reconciled")
        try:
            self._controls.require_armed()
        except TradingHaltedError as exc:
            raise RuntimeNotReadyError(str(exc)) from exc
        if self._last_error:
            raise RuntimeNotReadyError(
                f"runtime requires operator reconciliation: {self._last_error}"
            )

    def _require_opportunity_fresh(self, opportunity: CrossPlatformOpportunity) -> None:
        observed = opportunity.detected_at
        if observed.tzinfo is None or observed.utcoffset() is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
        if age < timedelta(0) or age > self._opportunity_max_age:
            raise RuntimeNotReadyError("opportunity became stale before mutation")

    def _require_market_authorized(self, pair: MarketPair) -> None:
        identities = {pair.polymarket_execution_id, pair.kalshi_ticker}
        if identities & self._market_blacklist:
            raise RuntimeNotReadyError("matched pair contains a blacklisted market")
        if self._market_whitelist and not identities <= self._market_whitelist:
            raise RuntimeNotReadyError("matched pair is not fully whitelisted")

    def _require_exposure_capacity(self, planned: TwoLegExecution) -> None:
        positions: dict[tuple[str, str], float] = {}
        for execution in self._journal.load_all():
            for leg in execution.legs.values():
                key = (leg.intent.venue.strip().lower(), leg.intent.market_id)
                positions[key] = positions.get(key, 0.0) + (
                    leg.intent.side.exposure_sign * leg.filled_size
                )
        for leg in planned.legs.values():
            key = (leg.intent.venue.strip().lower(), leg.intent.market_id)
            projected = positions.get(key, 0.0) + (
                leg.intent.side.exposure_sign * leg.intent.size
            )
            if abs(projected) > self._max_position_per_market:
                raise RuntimeNotReadyError("per-market exposure limit would be exceeded")
            positions[key] = projected
        active = [value for value in positions.values() if not math.isclose(value, 0.0)]
        if len(active) > self._max_open_positions:
            raise RuntimeNotReadyError("open-position limit would be exceeded")
        gross = sum(abs(value) for value in active)
        if gross > self._max_strategy_exposure:
            raise RuntimeNotReadyError("cross-platform strategy exposure limit would be exceeded")
        if gross > self._max_global_exposure:
            raise RuntimeNotReadyError("global exposure limit would be exceeded")

    async def _require_collateral_capacity(self, execution: TwoLegExecution) -> None:
        for leg in execution.legs.values():
            adapter = self._adapters[leg.intent.venue.strip().lower()]
            available = await adapter.available_collateral()  # type: ignore[attr-defined]
            required = leg.intent.size * (
                leg.intent.limit_price
                if leg.intent.side is LegSide.BUY
                else 1.0 - leg.intent.limit_price
            )
            if (
                available is None
                or not math.isfinite(available)
                or available < required
            ):
                raise RuntimeNotReadyError(
                    f"authoritative {leg.intent.venue} collateral is insufficient"
                )

    async def _refresh_recovery(self) -> ExecutionStartupGate:
        gate = await ExecutionStartupGate.establish(
            self._journal,
            self._adapters,  # adapters implement the authoritative reader contract
            required_venues=self.REQUIRED_VENUES,
        )
        self._recovery_ready = True
        self._last_error = ""
        return gate

    async def _run_private_stream(self) -> None:
        assert self._private_stream is not None
        try:
            await self._private_stream.run(
                on_event=self._on_private_event,
                reconcile_rest=self._on_stream_reconcile,
                on_disconnect=self._on_stream_disconnect,
                market_tickers=self._private_stream_markets,
            )
            raise RuntimeError("private lifecycle stream exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stream_ready = False
            self._recovery_ready = False
            self._last_error = type(exc).__name__
            await self._controls.trip(
                reason="private lifecycle stream failed",
                source="production_runtime",
            )

    async def _on_stream_reconcile(self) -> None:
        async with self._lock:
            await self._refresh_recovery()
            self._stream_ready = True

    async def _on_stream_disconnect(self) -> None:
        async with self._lock:
            self._stream_ready = False
            self._recovery_ready = False
            self._last_error = "private_stream_disconnected"
            await self._controls.trip(
                reason="private lifecycle stream disconnected",
                source="production_runtime",
            )

    async def _on_private_event(self, _event: Any) -> None:
        async with self._lock:
            try:
                await self._refresh_recovery()
            except RecoveryBlockedError:
                self._stream_ready = False
                self._recovery_ready = False
                self._last_error = "private_event_reconciliation"
                await self._controls.trip(
                    reason="private lifecycle event could not be reconciled",
                    source="production_runtime",
                )


def _build_execution(
    opportunity: CrossPlatformOpportunity,
) -> tuple[TwoLegExecution, str]:
    token = opportunity.token.strip().upper()
    if token not in {"YES", "NO"}:
        raise RuntimeNotReadyError("opportunity token is unsupported")

    if token == "YES":
        buy_side, sell_side = LegSide.BUY, LegSide.SELL
        buy_limit, sell_limit = opportunity.buy_price, opportunity.sell_price
    else:
        # The durable aggregate is normalized to YES exposure. Buying NO is
        # equivalent to selling YES, and selling NO is equivalent to buying YES.
        buy_side, sell_side = LegSide.SELL, LegSide.BUY
        buy_limit, sell_limit = 1.0 - opportunity.buy_price, 1.0 - opportunity.sell_price

    market_ids = {
        "polymarket": opportunity.market_pair.polymarket_execution_id,
        "kalshi": opportunity.market_pair.kalshi_ticker,
    }
    buy = LegIntent(
        "buy",
        opportunity.buy_platform,
        market_ids[opportunity.buy_platform],
        buy_side,
        buy_limit,
        opportunity.suggested_size,
    )
    sell = LegIntent(
        "sell",
        opportunity.sell_platform,
        market_ids[opportunity.sell_platform],
        sell_side,
        sell_limit,
        opportunity.suggested_size,
    )
    execution_id = f"locked-{uuid.uuid4()}"
    execution = TwoLegExecution(execution_id, buy, sell)
    first_leg_id = (
        "buy"
        if opportunity.buy_liquidity <= opportunity.sell_liquidity
        else "sell"
    )
    return execution, first_leg_id


def _normalize_opportunity(
    opportunity: CrossPlatformOpportunity,
) -> CrossPlatformOpportunity:
    """Round down to the common Kalshi/Polymarket contract increment."""
    try:
        size = Decimal(str(opportunity.suggested_size)).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
    except Exception as exc:
        raise RuntimeNotReadyError("opportunity size is invalid") from exc
    if not size.is_finite() or size <= 0:
        raise RuntimeNotReadyError("opportunity size is below venue precision")
    normalized = float(size)
    if normalized == opportunity.suggested_size:
        return opportunity
    return replace(opportunity, suggested_size=normalized)
