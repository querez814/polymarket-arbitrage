"""
Execution Engine Module
========================

Handles order placement, cancellation, and management.
Consumes signals from the ArbEngine and interfaces with the API.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from polymarket_client.api import BasePolymarketClient
from polymarket_client.models import (
    Order,
    OrderBook,
    OrderSide,
    OrderStatus,
    Signal,
    TokenType,
    Trade,
)
from core.risk_manager import RiskManager
from core.portfolio import Portfolio
from core.decision_journal import DecisionJournal, DecisionOutcome
from utils.time_utils import to_utc_iso, utc_now

if TYPE_CHECKING:
    from utils.paper_trade_store import PaperTradeStore


logger = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    """Configuration for the execution engine."""
    slippage_tolerance: float = 0.02  # Max allowed price slippage
    order_timeout_seconds: float = 60.0  # Cancel unfilled orders after this time
    strategy_slippage_tolerances: dict[str, float] = field(default_factory=dict)
    strategy_order_timeouts: dict[str, float] = field(default_factory=dict)
    high_edge_slippage_multiplier: float = 1.5
    enable_slippage_check: bool = True
    dry_run: bool = True


@dataclass
class ExecutionStats:
    """Statistics for the execution engine."""
    orders_placed: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    orders_rejected: int = 0
    total_notional: float = 0.0
    signals_processed: int = 0
    signals_rejected: int = 0
    slippage_rejections: int = 0
    risk_rejections: int = 0
    expired_before_fill: int = 0


class ExecutionEngine:
    """
    Order execution engine.
    
    Consumes trading signals and places/manages orders through the
    Polymarket API. Enforces risk limits and handles slippage checks.
    """
    
    def __init__(
        self,
        client: BasePolymarketClient,
        risk_manager: RiskManager,
        portfolio: Portfolio,
        config: ExecutionConfig,
        decision_journal: Optional[DecisionJournal] = None,
        paper_trade_store: Optional["PaperTradeStore"] = None,
    ):
        self.client = client
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.config = config
        self.decision_journal = decision_journal
        self.paper_trade_store = paper_trade_store
        self.stats = ExecutionStats()
        
        # Track open orders
        self._open_orders: dict[str, Order] = {}
        self._order_timestamps: dict[str, datetime] = {}
        self._order_history: list[Order] = []
        self._order_market_questions: dict[str, str] = {}
        
        # Order tracking by market and strategy
        self._orders_by_market: dict[str, list[str]] = {}
        self._orders_by_strategy: dict[str, list[str]] = {}
        
        # Signal queue
        self._signal_queue: asyncio.Queue[Signal] = asyncio.Queue()
        self._processing_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info(f"ExecutionEngine initialized (dry_run={config.dry_run})")
    
    def _record_decision(
        self,
        *,
        outcome: DecisionOutcome,
        reason_code: str,
        explanation: str,
        market_id: str = "",
        evidence: Optional[dict] = None,
        orders: Optional[list[dict]] = None,
        related_id: Optional[str] = None,
    ) -> None:
        """Record execution rationale if a journal is configured."""
        if not self.decision_journal:
            return
        self.decision_journal.add_decision(
            decision_id=f"exec_{uuid.uuid4().hex[:12]}",
            strategy="execution",
            outcome=outcome,
            reason_code=reason_code,
            explanation=explanation,
            market_id=market_id,
            platform="polymarket",
            evidence=evidence or {},
            orders=orders or [],
            related_id=related_id,
        )

    def _enum_value(self, value) -> str:
        return value.value if hasattr(value, "value") else str(value)

    def _record_paper_event(
        self,
        *,
        event_type: str,
        event_at: Optional[datetime] = None,
        order: Optional[Order] = None,
        trade: Optional[Trade] = None,
        signal_id: str = "",
        market_id: str = "",
        market_question: str = "",
        token_type: str = "",
        side: str = "",
        price: Optional[float] = None,
        size: Optional[float] = None,
        notional: Optional[float] = None,
        fee: Optional[float] = None,
        strategy_tag: str = "",
        status: str = "",
        reason_code: str = "",
        reason_detail: str = "",
    ) -> None:
        if not self.paper_trade_store or not self.config.dry_run:
            return

        if order:
            market_id = market_id or order.market_id
            market_question = market_question or self._order_market_questions.get(order.order_id, "")
            token_type = token_type or self._enum_value(order.token_type)
            side = side or self._enum_value(order.side)
            price = price if price is not None else order.price
            size = size if size is not None else order.size
            notional = notional if notional is not None else order.notional
            strategy_tag = strategy_tag or order.strategy_tag
            status = status or self._enum_value(order.status)
            event_at = event_at or order.updated_at or order.created_at

        if trade:
            market_id = market_id or trade.market_id
            market_question = market_question or self._order_market_questions.get(trade.order_id, "")
            token_type = token_type or self._enum_value(trade.token_type)
            side = side or self._enum_value(trade.side)
            price = price if price is not None else trade.price
            size = size if size is not None else trade.size
            notional = notional if notional is not None else trade.notional
            fee = fee if fee is not None else trade.fee
            event_at = event_at or trade.timestamp

        self.paper_trade_store.record_event(
            event_type=event_type,
            event_at=event_at,
            order_id=order.order_id if order else None,
            trade_id=trade.trade_id if trade else None,
            signal_id=signal_id or None,
            market_id=market_id,
            market_question=market_question,
            token_type=token_type,
            side=side,
            price=price,
            size=size,
            notional=notional,
            fee=fee,
            strategy_tag=strategy_tag,
            status=status,
            reason_code=reason_code,
            reason_detail=reason_detail,
            is_simulated=True,
            simulation_label="hypothetical_paper_fill" if event_type == "filled" else "hypothetical_paper",
            pnl_source="hypothetical_paper",
        )
    
    async def start(self) -> None:
        """Start the execution engine."""
        if self._running:
            return

        if not self.config.dry_run:
            await self._require_flat_live_start()
        
        self._running = True
        self._processing_task = asyncio.create_task(
            self._process_signals(),
            name="signal_processor"
        )
        
        # Start order timeout monitor
        asyncio.create_task(self._monitor_order_timeouts(), name="order_timeout_monitor")

        if not self.config.dry_run:
            asyncio.create_task(self._monitor_live_fills(), name="live_fill_monitor")
        
        logger.info("ExecutionEngine started")

    async def _require_flat_live_start(self) -> None:
        """Refuse live execution unless authoritative venue state is flat.

        In-memory order and risk ledgers cannot safely reconstruct strategy
        ownership or already-filled exposure after a process restart. Until a
        durable reconciliation workflow exists, the conservative recovery
        boundary is to require successful venue reads and an empty account.
        This method never cancels or otherwise mutates venue state.
        """
        try:
            open_orders = await self.client.get_open_orders()
            positions = await self.client.get_positions()
        except Exception as exc:
            raise RuntimeError(
                "Live execution startup blocked: venue state could not be "
                "reconciled read-only"
            ) from exc

        nonzero_positions = [
            position
            for token_positions in positions.values()
            for position in token_positions.values()
            if position.size != 0
        ]
        if open_orders or nonzero_positions:
            raise RuntimeError(
                "Live execution startup blocked: venue account is not flat "
                f"({len(open_orders)} open orders, "
                f"{len(nonzero_positions)} nonzero positions); reconcile "
                "externally before restart"
            )
    
    async def stop(self) -> None:
        """Stop the execution engine."""
        self._running = False
        
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all open orders
        await self.cancel_all_orders()
        
        logger.info("ExecutionEngine stopped")
    
    async def submit_signal(self, signal: Signal) -> None:
        """Submit a signal for processing."""
        await self._signal_queue.put(signal)
        logger.debug(f"Signal queued: {signal.signal_id}")
        self._record_decision(
            outcome=DecisionOutcome.WAIT,
            reason_code="signal_queued",
            explanation=f"Signal {signal.signal_id} queued for execution.",
            market_id=signal.market_id,
            evidence={
                "signal_id": signal.signal_id,
                "action": signal.action,
                "priority": signal.priority,
                "order_count": len(signal.orders),
            },
            orders=signal.orders,
            related_id=signal.signal_id,
        )
    
    async def _process_signals(self) -> None:
        """Main signal processing loop."""
        while self._running:
            try:
                # Get next signal with timeout
                try:
                    signal = await asyncio.wait_for(
                        self._signal_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                await self._execute_signal(signal)
                self.stats.signals_processed += 1
                
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Signal processing error: {e}")
    
    async def _execute_signal(self, signal: Signal) -> None:
        """Execute a single trading signal."""
        logger.info(f"Executing signal: {signal.signal_id} ({signal.action})")
        self._record_decision(
            outcome=DecisionOutcome.WAIT,
            reason_code="signal_processing",
            explanation=f"Processing signal {signal.signal_id}.",
            market_id=signal.market_id,
            evidence={"signal_id": signal.signal_id, "action": signal.action},
            orders=signal.orders,
            related_id=signal.signal_id,
        )
        
        if signal.is_place:
            await self._handle_place_orders(signal)
        elif signal.is_cancel:
            await self._handle_cancel_orders(signal)
        else:
            logger.warning(f"Unknown signal action: {signal.action}")
    
    async def _handle_place_orders(self, signal: Signal) -> None:
        """Handle a place_orders signal."""
        for order_spec in signal.orders:
            try:
                # Extract order parameters
                token_type = order_spec["token_type"]
                side = order_spec["side"]
                price = order_spec["price"]
                size = order_spec["size"]
                strategy_tag = order_spec.get("strategy_tag", "")
                
                # Check slippage if enabled
                if self.config.enable_slippage_check and signal.opportunity:
                    if not self._check_slippage(signal.opportunity, order_spec):
                        self.stats.slippage_rejections += 1
                        logger.warning(f"Order rejected due to slippage: {order_spec}")
                        self._record_decision(
                            outcome=DecisionOutcome.REJECT,
                            reason_code="slippage",
                            explanation="Rejected order because intended price exceeded slippage tolerance against the opportunity snapshot.",
                            market_id=signal.market_id,
                            evidence={
                                "signal_id": signal.signal_id,
                                "slippage_tolerance": self.config.slippage_tolerance,
                                "order": order_spec,
                            },
                            orders=[order_spec],
                            related_id=signal.signal_id,
                        )
                        self._record_paper_event(
                            event_type="rejected",
                            signal_id=signal.signal_id,
                            market_id=signal.market_id,
                            market_question=signal.market_question,
                            token_type=self._enum_value(token_type),
                            side=self._enum_value(side),
                            price=price,
                            size=size,
                            notional=price * size,
                            strategy_tag=strategy_tag,
                            reason_code="slippage",
                            reason_detail="Order rejected due to slippage tolerance.",
                        )
                        continue
                
                # Check risk limits
                proposed_order = Order(
                    order_id="temp",
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                if not self.risk_manager.check_order(proposed_order):
                    self.stats.signals_rejected += 1
                    self.stats.risk_rejections += 1
                    logger.warning(f"Order rejected by risk manager: {order_spec}")
                    self._record_decision(
                        outcome=DecisionOutcome.REJECT,
                        reason_code="risk_limit",
                        explanation="Rejected order because it failed configured risk checks.",
                        market_id=signal.market_id,
                        evidence={
                            "signal_id": signal.signal_id,
                            "order_notional": proposed_order.notional,
                            "risk": self.risk_manager.get_summary(),
                            "order": order_spec,
                        },
                        orders=[order_spec],
                        related_id=signal.signal_id,
                    )
                    self._record_paper_event(
                        event_type="rejected",
                        signal_id=signal.signal_id,
                        market_id=signal.market_id,
                        market_question=signal.market_question,
                        token_type=self._enum_value(token_type),
                        side=self._enum_value(side),
                        price=price,
                        size=size,
                        notional=proposed_order.notional,
                        strategy_tag=strategy_tag,
                        reason_code="risk_limit",
                        reason_detail="Order rejected by risk manager.",
                    )
                    continue

                # Charge the attempt before the only client call. A timeout or
                # transport failure may follow venue acceptance, so ambiguous
                # attempts conservatively continue to consume both budgets.
                if not self.risk_manager.admit_order_attempt():
                    self.stats.signals_rejected += 1
                    self.stats.risk_rejections += 1
                    logger.warning(
                        "Order rejected by placement-attempt cap: %s", order_spec
                    )
                    self._record_decision(
                        outcome=DecisionOutcome.REJECT,
                        reason_code="order_attempt_limit",
                        explanation="Rejected order because a local placement-attempt cap was reached.",
                        market_id=signal.market_id,
                        evidence={
                            "signal_id": signal.signal_id,
                            "risk": self.risk_manager.get_summary(),
                            "order": order_spec,
                        },
                        orders=[order_spec],
                        related_id=signal.signal_id,
                    )
                    self._record_paper_event(
                        event_type="rejected",
                        signal_id=signal.signal_id,
                        market_id=signal.market_id,
                        market_question=signal.market_question,
                        token_type=self._enum_value(token_type),
                        side=self._enum_value(side),
                        price=price,
                        size=size,
                        notional=proposed_order.notional,
                        strategy_tag=strategy_tag,
                        reason_code="order_attempt_limit",
                        reason_detail="Local placement-attempt cap reached.",
                    )
                    continue
                
                # Place the order
                order = await self._place_order(
                    market_id=signal.market_id,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    strategy_tag=strategy_tag,
                )
                
                if order:
                    self._remember_order_market_question(order.order_id, signal.market_question)
                    self._track_order(order)
                    self.risk_manager.reserve_strategy_exposure(order.strategy_tag, order.notional)
                    self.stats.orders_placed += 1
                    self.stats.total_notional += order.notional
                    self._record_decision(
                        outcome=DecisionOutcome.TRADE,
                        reason_code="paper_order_placed" if self.config.dry_run else "order_placed",
                        explanation=(
                            "Paper order placed in dry-run mode."
                            if self.config.dry_run else "Order placed through exchange API."
                        ),
                        market_id=signal.market_id,
                        evidence={
                            "signal_id": signal.signal_id,
                            "order_id": order.order_id,
                            "side": order.side.value,
                            "token_type": order.token_type.value,
                            "price": order.price,
                            "size": order.size,
                            "notional": order.notional,
                            "strategy_tag": order.strategy_tag,
                        },
                        orders=[{
                            "order_id": order.order_id,
                            "side": order.side.value,
                            "token_type": order.token_type.value,
                            "price": order.price,
                            "size": order.size,
                            "strategy_tag": order.strategy_tag,
                        }],
                        related_id=signal.signal_id,
                    )
                    self._record_paper_event(
                        event_type="placed",
                        order=order,
                        signal_id=signal.signal_id,
                        market_question=signal.market_question,
                        reason_code="paper_order_placed",
                        reason_detail="Paper order placed against real market data.",
                    )
                else:
                    self.stats.orders_rejected += 1
                    self._record_paper_event(
                        event_type="rejected",
                        signal_id=signal.signal_id,
                        market_id=signal.market_id,
                        market_question=signal.market_question,
                        token_type=self._enum_value(token_type),
                        side=self._enum_value(side),
                        price=price,
                        size=size,
                        notional=price * size,
                        strategy_tag=strategy_tag,
                        reason_code="order_error",
                        reason_detail="Order placement failed without a blind retry.",
                    )
                    
            except Exception as e:
                logger.error(f"Failed to place order: {e}")
                self.stats.orders_rejected += 1
                self._record_decision(
                    outcome=DecisionOutcome.ERROR,
                    reason_code="order_error",
                    explanation=f"Failed to place order: {e}",
                    market_id=signal.market_id,
                    evidence={"signal_id": signal.signal_id, "order": order_spec, "error": str(e)},
                    orders=[order_spec] if "order_spec" in locals() else [],
                    related_id=signal.signal_id,
                )
                self._record_paper_event(
                    event_type="rejected",
                    signal_id=signal.signal_id,
                    market_id=signal.market_id,
                    market_question=signal.market_question,
                    token_type=self._enum_value(order_spec.get("token_type", "")),
                    side=self._enum_value(order_spec.get("side", "")),
                    price=order_spec.get("price"),
                    size=order_spec.get("size"),
                    notional=(order_spec.get("price") or 0) * (order_spec.get("size") or 0),
                    strategy_tag=order_spec.get("strategy_tag", ""),
                    reason_code="order_error",
                    reason_detail=str(e),
                )
    
    async def _handle_cancel_orders(self, signal: Signal) -> None:
        """Handle a cancel_orders signal."""
        for order_id in signal.cancel_order_ids:
            try:
                await self.cancel_order(order_id)
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
    
    def _check_slippage(self, opportunity, order_spec: dict) -> bool:
        """
        Check if current prices have slipped too far from signal generation.
        
        Returns True if within tolerance, False if slippage exceeded.
        """
        # Compare intended price vs opportunity snapshot
        intended_price = order_spec["price"]
        side = order_spec["side"]
        token_type = order_spec["token_type"]
        strategy_tag = order_spec.get("strategy_tag", "")
        tolerance = self.config.strategy_slippage_tolerances.get(
            strategy_tag,
            self.config.slippage_tolerance,
        )
        edge = getattr(opportunity, "edge", None)
        if edge is None:
            edge = getattr(opportunity, "net_edge", 0.0)
        if edge and edge >= tolerance * 2:
            tolerance *= self.config.high_edge_slippage_multiplier
        
        if token_type == TokenType.YES:
            snapshot_bid = opportunity.best_bid_yes
            snapshot_ask = opportunity.best_ask_yes
        else:
            snapshot_bid = opportunity.best_bid_no
            snapshot_ask = opportunity.best_ask_no
        
        if snapshot_bid is None or snapshot_ask is None:
            return True  # Can't check, allow
        
        if side == OrderSide.BUY:
            # For buys, check if ask hasn't moved up too much
            slippage = (intended_price - snapshot_ask) / snapshot_ask if snapshot_ask > 0 else 0
        else:
            # For sells, check if bid hasn't moved down too much
            slippage = (snapshot_bid - intended_price) / snapshot_bid if snapshot_bid > 0 else 0
        
        return abs(slippage) <= tolerance
    
    async def _place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
    ) -> Optional[Order]:
        """Place an order once, failing closed when acceptance is ambiguous.

        A timeout or transport error can occur after an exchange accepted the
        order. Retrying without first reconciling by a stable client/server
        order id can duplicate exposure, so this boundary deliberately makes
        exactly one client call. A later reconciliation implementation may
        safely decide whether a retry is possible.
        """
        try:
            order = await self.client.place_order(
                market_id=market_id,
                token_type=token_type,
                side=side,
                price=price,
                size=size,
                strategy_tag=strategy_tag,
            )

            logger.info(
                f"Order placed: {order.order_id} | "
                f"{side.value} {size:.2f} {token_type.value} @ {price:.4f}"
            )

            return order

        except Exception as error:
            logger.error(
                "Order placement failed; refusing blind retry until exchange "
                "state is reconciled: %s",
                error,
            )
            return None
    
    def _track_order(self, order: Order) -> None:
        """Add order to tracking structures."""
        self.risk_manager.reserve_open_order(
            order.order_id,
            order.market_id,
            order.remaining_size * order.price,
        )
        self._open_orders[order.order_id] = order
        self._order_timestamps[order.order_id] = utc_now()
        self._order_history.append(order)
        if len(self._order_history) > 1000:
            self._order_history = self._order_history[-500:]
        
        # Track by market
        if order.market_id not in self._orders_by_market:
            self._orders_by_market[order.market_id] = []
        self._orders_by_market[order.market_id].append(order.order_id)
        
        # Track by strategy
        if order.strategy_tag:
            if order.strategy_tag not in self._orders_by_strategy:
                self._orders_by_strategy[order.strategy_tag] = []
            self._orders_by_strategy[order.strategy_tag].append(order.order_id)

    def _remember_order_market_question(self, order_id: str, market_question: str) -> None:
        if market_question:
            self._order_market_questions[order_id] = market_question
    
    def _untrack_order(self, order_id: str) -> None:
        """Remove order from tracking structures."""
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            del self._open_orders[order_id]
            self.risk_manager.release_open_order(order_id)
            
            if order_id in self._order_timestamps:
                del self._order_timestamps[order_id]
            
            # Remove from market tracking
            if order.market_id in self._orders_by_market:
                if order_id in self._orders_by_market[order.market_id]:
                    self._orders_by_market[order.market_id].remove(order_id)
            
            # Remove from strategy tracking
            if order.strategy_tag and order.strategy_tag in self._orders_by_strategy:
                if order_id in self._orders_by_strategy[order.strategy_tag]:
                    self._orders_by_strategy[order.strategy_tag].remove(order_id)
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order and release exposure only after terminal state is known.

        In live mode, a successful cancel request is not authoritative evidence
        that the order's full residual was removed. Reconcile the order once,
        apply any fills that raced with cancellation, and retain local tracking
        unless the venue reports a terminal state.
        """
        try:
            await self.client.cancel_order(order_id)
            order = self._open_orders.get(order_id)

            if order and not self.config.dry_run:
                try:
                    remote_order, trades = await self.client.refresh_order(order)
                except Exception as exc:
                    logger.error(
                        "Cancel requested for %s but reconciliation failed; "
                        "retaining residual exposure: %s",
                        order_id,
                        exc,
                    )
                    return False

                for trade in trades:
                    self.handle_fill(trade)

                if order_id not in self._open_orders:
                    logger.info("Order %s became fully filled during cancellation", order_id)
                    return True

                if remote_order.status not in {
                    OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                    OrderStatus.REJECTED,
                }:
                    logger.error(
                        "Cancel requested for %s but venue still reports %s; "
                        "retaining residual exposure",
                        order_id,
                        remote_order.status.value,
                    )
                    return False

                order.status = remote_order.status
                order.updated_at = remote_order.updated_at

            if order:
                self.risk_manager.release_strategy_exposure(order.strategy_tag, order.notional)
                self._record_paper_event(
                    event_type="cancelled",
                    order=order,
                    reason_code="order_cancelled",
                    reason_detail=f"Order {order_id} was cancelled.",
                )
            self._untrack_order(order_id)
            self.stats.orders_cancelled += 1
            logger.info(f"Order cancelled: {order_id}")
            self._record_decision(
                outcome=DecisionOutcome.WAIT,
                reason_code="order_cancelled",
                explanation=f"Order {order_id} was cancelled.",
                market_id=order.market_id if order else "",
                evidence={"order_id": order_id},
                related_id=order_id,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific market."""
        if market_id:
            order_ids = list(self._orders_by_market.get(market_id, []))
        else:
            order_ids = list(self._open_orders.keys())
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        logger.info(f"Cancelled {cancelled} orders")
        return cancelled
    
    async def cancel_orders_by_strategy(self, strategy_tag: str) -> int:
        """Cancel all orders for a specific strategy."""
        order_ids = list(self._orders_by_strategy.get(strategy_tag, []))
        
        cancelled = 0
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        
        return cancelled
    
    async def _monitor_live_fills(self) -> None:
        """Poll exchange order state and apply incremental fills in live mode."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
                if self.config.dry_run or not self._open_orders:
                    continue

                for order_id, local_order in list(self._open_orders.items()):
                    try:
                        remote_order, trades = await self.client.refresh_order(local_order)
                    except Exception as exc:
                        logger.warning("Failed to refresh order %s: %s", order_id, exc)
                        continue

                    for trade in trades:
                        self.handle_fill(trade)

                    if order_id not in self._open_orders:
                        continue

                    if remote_order.status == OrderStatus.CANCELLED:
                        self.risk_manager.release_strategy_exposure(
                            local_order.strategy_tag,
                            local_order.remaining_size * local_order.price,
                        )
                        local_order.status = OrderStatus.CANCELLED
                        local_order.updated_at = utc_now()
                        self._untrack_order(order_id)
                        self.stats.orders_cancelled += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Live fill monitor error: {e}")
    
    async def _monitor_order_timeouts(self) -> None:
        """Monitor and cancel orders that have timed out."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                now = utc_now()
                timed_out = []
                for order_id, timestamp in self._order_timestamps.items():
                    order = self._open_orders.get(order_id)
                    timeout_seconds = self._timeout_for_order(order)
                    if now - timestamp > timedelta(seconds=timeout_seconds):
                        timed_out.append(order_id)
                
                for order_id in timed_out:
                    logger.info(f"Order timed out: {order_id}")
                    self.stats.expired_before_fill += 1
                    order = self._open_orders.get(order_id)
                    self._record_paper_event(
                        event_type="expired",
                        order=order,
                        reason_code="order_timeout",
                        reason_detail=f"Order {order_id} expired before fill.",
                    )
                    self._record_decision(
                        outcome=DecisionOutcome.REJECT,
                        reason_code="order_timeout",
                        explanation=f"Order {order_id} timed out before filling.",
                        market_id=order.market_id if order else "",
                        evidence={
                            "order_id": order_id,
                            "timeout_seconds": self._timeout_for_order(order),
                        },
                        related_id=order_id,
                    )
                    await self.cancel_order(order_id)
                    
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Order timeout monitor error: {e}")
    
    def handle_fill(self, trade: Trade) -> None:
        """Handle a trade fill notification."""
        order_id = trade.order_id
        order = self._open_orders.get(order_id)
        
        if order:
            order.filled_size += trade.size
            order.updated_at = utc_now()
            self.risk_manager.release_open_order(order_id, trade.size * order.price)
            
            if order.remaining_size <= 0:
                order.status = OrderStatus.FILLED
                self.risk_manager.release_strategy_exposure(order.strategy_tag, order.notional)
                self._untrack_order(order_id)
                self.stats.orders_filled += 1
            else:
                order.status = OrderStatus.PARTIALLY_FILLED
        
        # Update portfolio
        self.portfolio.update_from_fill(trade)
        
        # Update risk manager
        self.risk_manager.update_from_fill(trade)
        
        logger.info(
            f"Fill: {trade.trade_id} | "
            f"{trade.side.value} {trade.size:.2f} {trade.token_type.value} @ {trade.price:.4f}"
        )
        self._record_decision(
            outcome=DecisionOutcome.TRADE,
            reason_code="hypothetical_paper_fill" if self.config.dry_run else "order_filled",
            explanation=(
                "Hypothetical paper fill simulated for strategy evaluation; no real money was used."
                if self.config.dry_run else "Order fill received from exchange."
            ),
            market_id=trade.market_id,
            evidence={
                "trade_id": trade.trade_id,
                "order_id": trade.order_id,
                "side": trade.side.value,
                "token_type": trade.token_type.value,
                "price": trade.price,
                "size": trade.size,
                "notional": trade.notional,
                "fee": trade.fee,
                "is_simulated": trade.is_simulated,
                "simulation_label": trade.simulation_label,
            },
            related_id=trade.order_id,
        )
        if self.config.dry_run:
            self._record_paper_event(
                event_type="filled",
                order=order,
                trade=trade,
                reason_code="hypothetical_paper_fill",
                reason_detail="Hypothetical paper fill simulated for strategy evaluation.",
            )

    def _timeout_for_order(self, order: Optional[Order]) -> float:
        if not order:
            return self.config.order_timeout_seconds
        return self.config.strategy_order_timeouts.get(
            order.strategy_tag,
            self.config.order_timeout_seconds,
        )
    
    def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        """Get all open orders, optionally filtered by market."""
        if market_id:
            order_ids = self._orders_by_market.get(market_id, [])
            return [self._open_orders[oid] for oid in order_ids if oid in self._open_orders]
        return list(self._open_orders.values())

    def _order_snapshot(self, order: Order, snapshot_type: str) -> dict:
        """Return dashboard-ready order state with paper/live labeling."""
        created_at = self._order_timestamps.get(order.order_id, order.created_at)
        age_seconds = max(0.0, (utc_now() - created_at).total_seconds())
        timeout_seconds = self._timeout_for_order(order)
        remaining_size = order.remaining_size
        remaining_notional = remaining_size * order.price
        return {
            "type": snapshot_type,
            "order_id": order.order_id,
            "market_id": order.market_id,
            "token_type": order.token_type.value,
            "side": order.side.value,
            "price": order.price,
            "size": order.size,
            "filled_size": order.filled_size,
            "remaining_size": remaining_size,
            "notional": order.notional,
            "remaining_notional": remaining_notional,
            "strategy_tag": order.strategy_tag,
            "status": order.status.value,
            "age_seconds": age_seconds,
            "timeout_seconds": timeout_seconds,
            "expires_in_seconds": max(0.0, timeout_seconds - age_seconds),
            "created_at": created_at.isoformat(),
            "updated_at": order.updated_at.isoformat(),
            "execution_mode": "paper" if self.config.dry_run else "live",
            "is_paper": self.config.dry_run,
            "pnl_source": "paper" if self.config.dry_run else "live",
        }

    def get_open_order_snapshots(self, market_id: Optional[str] = None) -> list[dict]:
        """Return dashboard-ready snapshots for currently open orders."""
        return [
            self._order_snapshot(order, "open_order")
            for order in self.get_open_orders(market_id)
        ]

    def get_order_history_snapshots(self, limit: int = 100) -> list[dict]:
        """Return recent order attempts, including paper orders from real data."""
        snapshot_type = "paper_order" if self.config.dry_run else "order"
        return [
            self._order_snapshot(order, snapshot_type)
            for order in self._order_history[-limit:]
        ]

    def get_open_notional_by_strategy(self) -> dict[str, float]:
        """Aggregate remaining open-order notional by strategy tag."""
        totals: dict[str, float] = {}
        for order in self._open_orders.values():
            strategy = order.strategy_tag or "unclassified"
            totals[strategy] = totals.get(strategy, 0.0) + order.remaining_size * order.price
        return totals

    def get_total_open_notional(self) -> float:
        """Get total remaining notional across open orders."""
        return sum(order.remaining_size * order.price for order in self._open_orders.values())
    
    def get_stats(self) -> ExecutionStats:
        """Get execution statistics."""
        return self.stats
    
    @property
    def open_order_count(self) -> int:
        """Get number of open orders."""
        return len(self._open_orders)
