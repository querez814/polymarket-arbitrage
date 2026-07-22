"""
Polymarket Client Module
========================

Provides abstracted access to Polymarket's REST and WebSocket APIs.
"""

from polymarket_client.models import (
    Market,
    OrderBook,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Trade,
    PriceLevel,
)
from polymarket_client.api import PolymarketClient, BasePolymarketClient
from polymarket_client.factory import create_polymarket_client
from polymarket_client.venue_adapter import PolymarketVenueAdapter

__all__ = [
    "PolymarketClient",
    "BasePolymarketClient",
    "create_polymarket_client",
    "PolymarketVenueAdapter",
    "Market",
    "OrderBook",
    "Order",
    "OrderSide",
    "OrderStatus",
    "Position",
    "Trade",
    "PriceLevel",
]
