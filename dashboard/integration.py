"""
Dashboard Integration
======================

Integrates the dashboard with the trading bot components.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from dashboard.server import dashboard_state
from utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class DashboardIntegration:
    """
    Integrates the trading bot with the dashboard.
    
    Updates the dashboard state with live data from the bot.
    """
    
    def __init__(
        self,
        data_feed=None,
        arb_engine=None,
        execution_engine=None,
        risk_manager=None,
        portfolio=None,
        mode: str = "dry_run",
        decision_journal=None,
        paper_trade_store=None,
    ):
        self.data_feed = data_feed
        self.arb_engine = arb_engine
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.mode = mode
        self.decision_journal = decision_journal
        self.paper_trade_store = paper_trade_store
        
        dashboard_state.mode = mode
        dashboard_state.is_running = False
        
        self._update_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self, update_interval: float = 1.0) -> None:
        """Start the dashboard integration."""
        self._running = True
        dashboard_state.is_running = True
        
        self._update_task = asyncio.create_task(
            self._update_loop(update_interval)
        )
        
        logger.info("Dashboard integration started")
    
    async def stop(self) -> None:
        """Stop the dashboard integration."""
        self._running = False
        dashboard_state.is_running = False
        
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Dashboard integration stopped")
    
    async def _update_loop(self, interval: float) -> None:
        """Periodically update the dashboard state."""
        while self._running:
            try:
                await self._update_state()
                await self._broadcast_update()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dashboard update error: {e}")
                await asyncio.sleep(interval)
    
    async def _update_state(self) -> None:
        """Update the dashboard state from bot components."""
        # Update markets
        if self.data_feed:
            markets = {}
            for market_id, state in self.data_feed.get_all_market_states().items():
                ob = state.order_book
                markets[market_id] = {
                    "market_id": market_id,
                    "question": state.market.question[:80] if state.market.question else market_id,
                    "best_bid_yes": ob.best_bid_yes,
                    "best_ask_yes": ob.best_ask_yes,
                    "best_bid_no": ob.best_bid_no,
                    "best_ask_no": ob.best_ask_no,
                    "total_ask": ob.total_ask,
                    "total_bid": ob.total_bid,
                    "spread_yes": ob.yes.spread if ob.yes else None,
                    "spread_no": ob.no.spread if ob.no else None,
                }
            dashboard_state.markets = markets
        
        # Update portfolio
        position_snapshots = []
        recent_trade_snapshots = []
        if self.portfolio:
            summary = self.portfolio.get_summary()
            summary["is_paper"] = self.mode == "dry_run"
            summary["pnl_source"] = "paper" if self.mode == "dry_run" else "live"
            dashboard_state.portfolio = summary
            position_snapshots = self.portfolio.get_position_snapshots()
            recent_trade_snapshots = self.portfolio.get_recent_trade_snapshots(limit=100)
            dashboard_state.positions = position_snapshots
            dashboard_state.trades = recent_trade_snapshots
        
        # Update risk
        if self.risk_manager:
            dashboard_state.risk = self.risk_manager.get_summary()
        
        # Update orders
        open_order_snapshots = []
        order_history_snapshots = []
        if self.execution_engine:
            open_order_snapshots = self.execution_engine.get_open_order_snapshots()
            order_history_snapshots = self.execution_engine.get_order_history_snapshots(limit=100)
            dashboard_state.orders = open_order_snapshots
            dashboard_state.paper_orders = order_history_snapshots
            
            # Update stats
            stats = self.execution_engine.get_stats()
            dashboard_state.stats = {
                "execution_mode": self.mode,
                "is_paper": self.mode == "dry_run",
                "orders_placed": stats.orders_placed,
                "paper_orders": len(order_history_snapshots) if self.mode == "dry_run" else 0,
                "orders_filled": stats.orders_filled,
                "orders_cancelled": stats.orders_cancelled,
                "orders_rejected": stats.orders_rejected,
                "signals_processed": stats.signals_processed,
                "signals_rejected": stats.signals_rejected,
                "slippage_rejections": stats.slippage_rejections,
                "risk_rejections": stats.risk_rejections,
                "expired_before_fill": stats.expired_before_fill,
                "total_open_notional": self.execution_engine.get_total_open_notional(),
            }

        filled_exposure = self.portfolio.get_total_exposure() if self.portfolio else 0.0
        open_order_exposure = sum(order["remaining_notional"] for order in open_order_snapshots)
        strategy_reserved = (
            self.risk_manager.get_summary().get("strategy_exposure", {})
            if self.risk_manager
            else {}
        )
        risk_summary = self.risk_manager.get_summary() if self.risk_manager else {}
        dashboard_state.active_trades = [
            *open_order_snapshots,
            *position_snapshots,
        ]
        dashboard_state.exposure_breakdown = {
            "filled_exposure": filled_exposure,
            "open_order_exposure": open_order_exposure,
            "total_active_exposure": filled_exposure + open_order_exposure,
            "strategy_reserved_exposure": strategy_reserved,
            "open_order_exposure_by_strategy": (
                self.execution_engine.get_open_notional_by_strategy()
                if self.execution_engine
                else {}
            ),
            "global_exposure": risk_summary.get("global_exposure", 0.0),
            "max_global_exposure": risk_summary.get("max_global_exposure", 0.0),
            "global_available": risk_summary.get("max_global_exposure", 0.0) - risk_summary.get("global_exposure", 0.0),
            "markets_with_exposure": risk_summary.get("markets_with_exposure", 0),
        }
        
        # Update arb stats and timing
        if self.arb_engine:
            arb_stats = self.arb_engine.get_stats()
            dashboard_state.stats.update({
                "bundle_opportunities": arb_stats.bundle_opportunities_detected,
                "mm_opportunities": arb_stats.mm_opportunities_detected,
                "signals_generated": arb_stats.signals_generated,
            })
            
            # Update opportunity timing stats
            dashboard_state.timing = self.arb_engine.get_timing_stats()
        
        # Update operational stats
        if self.data_feed:
            markets_with_data = len([m for m in dashboard_state.markets.values() 
                                     if m.get("best_bid_yes") or m.get("best_ask_yes")])
            dashboard_state.operational = {
                "total_markets": len(self.data_feed.market_ids),
                "markets_with_orderbooks": len(dashboard_state.markets),
                "markets_with_prices": markets_with_data,
                "orderbook_updates": self.data_feed.update_count,
                "is_streaming": self.data_feed.is_running,
            }
        
        if self.decision_journal:
            dashboard_state.decisions = self.decision_journal.to_dicts(limit=200)
            dashboard_state.decision_summary = self.decision_journal.summary()
        
        if self.paper_trade_store:
            dashboard_state.paper_history = [
                event.to_dict()
                for event in self.paper_trade_store.recent_events(limit=200)
            ]
        elif not dashboard_state.paper_history:
            dashboard_state.paper_history = []

        dashboard_state.last_update = utc_now()
    
    async def _broadcast_update(self) -> None:
        """Broadcast update to connected clients."""
        await dashboard_state.broadcast({
            "type": "update",
            "data": dashboard_state.to_dict()
        })
    
    def add_opportunity(
        self,
        opportunity_type: str,
        market_id: str,
        edge: float,
        **kwargs
    ) -> None:
        """Add an opportunity to the dashboard."""
        opp = {
            "type": opportunity_type,
            "market_id": market_id,
            "edge": edge,
            **kwargs
        }
        dashboard_state.add_opportunity(opp)
        
        # Broadcast immediately
        asyncio.create_task(dashboard_state.broadcast({
            "type": "opportunity",
            "data": opp
        }))
    
    def add_signal(
        self,
        action: str,
        market_id: str,
        **kwargs
    ) -> None:
        """Add a signal to the dashboard."""
        signal = {
            "action": action,
            "market_id": market_id,
            **kwargs
        }
        dashboard_state.add_signal(signal)
        
        asyncio.create_task(dashboard_state.broadcast({
            "type": "activity",
            "data": signal
        }))
    
    def add_trade(
        self,
        side: str,
        price: float,
        size: float,
        **kwargs
    ) -> None:
        """Add a trade to the dashboard."""
        trade = {
            "side": side,
            "price": price,
            "size": size,
            **kwargs
        }
        dashboard_state.add_trade(trade)
        
        asyncio.create_task(dashboard_state.broadcast({
            "type": "activity",
            "data": trade
        }))

