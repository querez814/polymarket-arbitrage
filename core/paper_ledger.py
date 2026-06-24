"""
Paper Trading Ledger
====================

Records paper-only detections, simulated orders, fills, misses, and exposure.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from polymarket_client.models import Order, OrderSide, Signal, Trade


@dataclass
class PaperLedgerEvent:
    """A paper trading event suitable for logs and dashboard display."""
    event_id: str
    timestamp: datetime
    status: str
    market_id: str
    strategy: str
    signal_id: Optional[str] = None
    order_id: Optional[str] = None
    opportunity_type: Optional[str] = None
    rejection_reason: Optional[str] = None
    expected_pnl: float = 0.0
    notional: float = 0.0
    orders: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    depth_walked: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize for dashboard/API output."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "market_id": self.market_id,
            "strategy": self.strategy,
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "opportunity_type": self.opportunity_type,
            "rejection_reason": self.rejection_reason,
            "expected_pnl": round(self.expected_pnl, 4),
            "notional": round(self.notional, 4),
            "orders": self.orders,
            "fills": self.fills,
            "depth_walked": self.depth_walked,
        }


class PaperLedger:
    """In-memory ledger for simulated trading activity."""

    def __init__(self, one_sided_exposure_cap: float = 250.0, max_events: int = 1000):
        self.one_sided_exposure_cap = one_sided_exposure_cap
        self.max_events = max_events
        self._events: list[PaperLedgerEvent] = []
        self._exposure: dict[str, dict[str, float]] = {}
        self._order_to_strategy: dict[str, str] = {}

    def record_signal(self, signal: Signal) -> PaperLedgerEvent:
        """Record a detected signal and its expected paper PnL."""
        opportunity = signal.opportunity
        strategy = self._strategy_from_signal(signal)
        expected_pnl = (opportunity.edge * opportunity.suggested_size) if opportunity else 0.0
        depth_walked = opportunity.metadata.get("depth_walked", {}) if opportunity else {}
        orders = [self._order_spec_to_dict(o) for o in signal.orders]
        event = PaperLedgerEvent(
            event_id=f"paper_detected_{len(self._events) + 1}",
            timestamp=datetime.utcnow(),
            status="detected",
            market_id=signal.market_id,
            strategy=strategy,
            signal_id=signal.signal_id,
            opportunity_type=opportunity.opportunity_type.value if opportunity else None,
            expected_pnl=expected_pnl,
            notional=sum(float(o.get("price", 0)) * float(o.get("size", 0)) for o in signal.orders),
            orders=orders,
            depth_walked=depth_walked,
        )
        return self._append(event)

    def record_rejection(self, signal: Signal, reason: str) -> PaperLedgerEvent:
        """Record a rejected paper signal/order."""
        opportunity = signal.opportunity
        event = PaperLedgerEvent(
            event_id=f"paper_rejected_{len(self._events) + 1}",
            timestamp=datetime.utcnow(),
            status="rejected",
            market_id=signal.market_id,
            strategy=self._strategy_from_signal(signal),
            signal_id=signal.signal_id,
            opportunity_type=opportunity.opportunity_type.value if opportunity else None,
            rejection_reason=reason,
            depth_walked=opportunity.metadata.get("depth_walked", {}) if opportunity else {},
        )
        return self._append(event)

    def record_order(self, order: Order) -> PaperLedgerEvent:
        """Record a simulated order placement."""
        strategy = order.strategy_tag or "unknown"
        self._order_to_strategy[order.order_id] = strategy
        event = PaperLedgerEvent(
            event_id=f"paper_order_{len(self._events) + 1}",
            timestamp=datetime.utcnow(),
            status="submitted",
            market_id=order.market_id,
            strategy=strategy,
            order_id=order.order_id,
            notional=order.notional,
            orders=[self._order_to_dict(order)],
        )
        return self._append(event)

    def record_fill(self, trade: Trade) -> PaperLedgerEvent:
        """Record a simulated fill and update open exposure."""
        strategy = self._order_to_strategy.get(trade.order_id, "unknown")
        signed_size = trade.size if trade.side == OrderSide.BUY else -trade.size
        market_exposure = self._exposure.setdefault(trade.market_id, {})
        market_exposure[strategy] = market_exposure.get(strategy, 0.0) + signed_size * trade.price

        event = PaperLedgerEvent(
            event_id=f"paper_fill_{len(self._events) + 1}",
            timestamp=datetime.utcnow(),
            status="filled",
            market_id=trade.market_id,
            strategy=strategy,
            order_id=trade.order_id,
            notional=trade.notional,
            fills=[self._trade_to_dict(trade)],
        )
        return self._append(event)

    def record_miss(self, order: Order, reason: str) -> PaperLedgerEvent:
        """Record a simulated order miss/cancel/timeout."""
        event = PaperLedgerEvent(
            event_id=f"paper_missed_{len(self._events) + 1}",
            timestamp=datetime.utcnow(),
            status="missed",
            market_id=order.market_id,
            strategy=order.strategy_tag or "unknown",
            order_id=order.order_id,
            rejection_reason=reason,
            notional=order.notional,
            orders=[self._order_to_dict(order)],
        )
        return self._append(event)

    def would_exceed_one_sided_cap(self, signal: Signal) -> bool:
        """Return True if any one-sided leg would push exposure over the paper cap."""
        projected = self._projected_market_exposure(signal)
        return any(abs(value) > self.one_sided_exposure_cap for value in projected.values())

    def recent(self, limit: int = 100) -> list[dict]:
        """Return recent ledger events."""
        return [event.to_dict() for event in self._events[-limit:]]

    def summary(self) -> dict:
        """Return aggregate paper ledger stats."""
        fills = [e for e in self._events if e.status == "filled"]
        rejections = [e for e in self._events if e.status == "rejected"]
        submitted = [e for e in self._events if e.status == "submitted"]
        return {
            "events": len(self._events),
            "orders_submitted": len(submitted),
            "fills": len(fills),
            "rejections": len(rejections),
            "expected_pnl": round(sum(e.expected_pnl for e in self._events), 4),
            "open_exposure": self.open_exposure(),
            "empty_message": "No paper fills yet" if not fills else "",
        }

    def open_exposure(self) -> dict:
        """Return open exposure by market and strategy."""
        return {
            market_id: {
                strategy: round(value, 4)
                for strategy, value in strategies.items()
                if abs(value) > 1e-9
            }
            for market_id, strategies in self._exposure.items()
            if any(abs(value) > 1e-9 for value in strategies.values())
        }

    def _projected_market_exposure(self, signal: Signal) -> dict[str, float]:
        market_exposure = self._exposure.get(signal.market_id, {}).copy()
        for spec in signal.orders:
            strategy = str(spec.get("strategy_tag") or "unknown")
            side = spec.get("side")
            signed = 1 if side == OrderSide.BUY else -1
            notional = float(spec.get("price", 0)) * float(spec.get("size", 0)) * signed
            market_exposure[strategy] = market_exposure.get(strategy, 0.0) + notional
        return market_exposure

    def _append(self, event: PaperLedgerEvent) -> PaperLedgerEvent:
        self._events.append(event)
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events // 2:]
        return event

    def _strategy_from_signal(self, signal: Signal) -> str:
        if signal.orders:
            return str(signal.orders[0].get("strategy_tag") or "unknown")
        return "unknown"

    def _order_spec_to_dict(self, spec: dict) -> dict:
        return {
            "token_type": getattr(spec.get("token_type"), "value", spec.get("token_type")),
            "side": getattr(spec.get("side"), "value", spec.get("side")),
            "price": float(spec.get("price", 0)),
            "size": float(spec.get("size", 0)),
            "strategy_tag": spec.get("strategy_tag", ""),
        }

    def _order_to_dict(self, order: Order) -> dict:
        return {
            "order_id": order.order_id,
            "token_type": order.token_type.value,
            "side": order.side.value,
            "price": order.price,
            "size": order.size,
            "filled_size": order.filled_size,
            "status": order.status.value,
            "strategy_tag": order.strategy_tag,
        }

    def _trade_to_dict(self, trade: Trade) -> dict:
        return {
            "trade_id": trade.trade_id,
            "order_id": trade.order_id,
            "token_type": trade.token_type.value,
            "side": trade.side.value,
            "price": trade.price,
            "size": trade.size,
            "fee": trade.fee,
        }
