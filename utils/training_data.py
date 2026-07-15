"""
Utilities for collecting ML-ready cross-platform training snapshots.

The live collector stores pair-level rows, because the model should learn from
the full cross-platform state at one point in time instead of isolated venue
records.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from polymarket_client.models import OrderBook, TokenOrderBook


def utc_now_iso() -> str:
    """Return current UTC timestamp in the project's JSONL timestamp format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def quote_from_token_book(book: TokenOrderBook) -> dict:
    """Extract top-of-book quote fields from a token order book."""
    bid = book.best_bid
    ask = book.best_ask
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": book.best_bid_size,
        "ask_size": book.best_ask_size,
        "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "spread": ask - bid if bid is not None and ask is not None else None,
    }


def quotes_from_orderbook(orderbook: OrderBook) -> dict:
    """Convert a unified order book into compact YES/NO quote features."""
    return {
        "market_id": orderbook.market_id,
        "timestamp": orderbook.timestamp.isoformat().replace("+00:00", "Z"),
        "yes": quote_from_token_book(orderbook.yes),
        "no": quote_from_token_book(orderbook.no),
    }


def edge_direction(
    *,
    token: str,
    buy_platform: str,
    sell_platform: str,
    buy_price: Optional[float],
    sell_price: Optional[float],
    buy_size: Optional[float],
    sell_size: Optional[float],
    polymarket_taker_fee: float,
    kalshi_taker_fee: float,
    gas_cost: float,
) -> Optional[dict]:
    """Compute one directional cross-platform edge if both prices exist."""
    if buy_price is None or sell_price is None:
        return None

    buy_fee_rate = polymarket_taker_fee if buy_platform == "polymarket" else kalshi_taker_fee
    sell_fee_rate = polymarket_taker_fee if sell_platform == "polymarket" else kalshi_taker_fee
    gross_edge = sell_price - buy_price
    estimated_cost = buy_price * buy_fee_rate + sell_price * sell_fee_rate + gas_cost * 2
    net_edge = gross_edge - estimated_cost

    return {
        "token": token,
        "buy_platform": buy_platform,
        "sell_platform": sell_platform,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "gross_edge": gross_edge,
        "estimated_cost": estimated_cost,
        "net_edge": net_edge,
        "edge_pct": net_edge / buy_price if buy_price > 0 else None,
        "max_size": min(buy_size or 0.0, sell_size or 0.0),
    }


def edge_directions(
    *,
    polymarket_quotes: dict,
    kalshi_quotes: dict,
    polymarket_taker_fee: float,
    kalshi_taker_fee: float,
    gas_cost: float,
) -> list[dict]:
    """Compute all four YES/NO cross-platform edge directions."""
    poly_yes = polymarket_quotes["yes"]
    poly_no = polymarket_quotes["no"]
    kalshi_yes = kalshi_quotes["yes"]
    kalshi_no = kalshi_quotes["no"]

    candidates = [
        edge_direction(
            token="YES",
            buy_platform="polymarket",
            sell_platform="kalshi",
            buy_price=poly_yes["ask"],
            sell_price=kalshi_yes["bid"],
            buy_size=poly_yes["ask_size"],
            sell_size=kalshi_yes["bid_size"],
            polymarket_taker_fee=polymarket_taker_fee,
            kalshi_taker_fee=kalshi_taker_fee,
            gas_cost=gas_cost,
        ),
        edge_direction(
            token="YES",
            buy_platform="kalshi",
            sell_platform="polymarket",
            buy_price=kalshi_yes["ask"],
            sell_price=poly_yes["bid"],
            buy_size=kalshi_yes["ask_size"],
            sell_size=poly_yes["bid_size"],
            polymarket_taker_fee=polymarket_taker_fee,
            kalshi_taker_fee=kalshi_taker_fee,
            gas_cost=gas_cost,
        ),
        edge_direction(
            token="NO",
            buy_platform="polymarket",
            sell_platform="kalshi",
            buy_price=poly_no["ask"],
            sell_price=kalshi_no["bid"],
            buy_size=poly_no["ask_size"],
            sell_size=kalshi_no["bid_size"],
            polymarket_taker_fee=polymarket_taker_fee,
            kalshi_taker_fee=kalshi_taker_fee,
            gas_cost=gas_cost,
        ),
        edge_direction(
            token="NO",
            buy_platform="kalshi",
            sell_platform="polymarket",
            buy_price=kalshi_no["ask"],
            sell_price=poly_no["bid"],
            buy_size=kalshi_no["ask_size"],
            sell_size=poly_no["bid_size"],
            polymarket_taker_fee=polymarket_taker_fee,
            kalshi_taker_fee=kalshi_taker_fee,
            gas_cost=gas_cost,
        ),
    ]
    return [candidate for candidate in candidates if candidate is not None]


def build_training_snapshot(
    *,
    timestamp: str,
    polymarket_id: str,
    kalshi_ticker: str,
    polymarket_orderbook: OrderBook,
    kalshi_orderbook: OrderBook,
    polymarket_question: str = "",
    kalshi_title: str = "",
    polymarket_taker_fee: float = 0.015,
    kalshi_taker_fee: float = 0.01,
    gas_cost: float = 0.02,
) -> dict:
    """Create one ML-ready row for a matched cross-platform market pair."""
    polymarket_quotes = quotes_from_orderbook(polymarket_orderbook)
    kalshi_quotes = quotes_from_orderbook(kalshi_orderbook)
    directions = edge_directions(
        polymarket_quotes=polymarket_quotes,
        kalshi_quotes=kalshi_quotes,
        polymarket_taker_fee=polymarket_taker_fee,
        kalshi_taker_fee=kalshi_taker_fee,
        gas_cost=gas_cost,
    )
    best_direction = max(directions, key=lambda item: item["net_edge"], default=None)
    best_gross_direction = max(directions, key=lambda item: item["gross_edge"], default=None)

    return {
        "timestamp": timestamp,
        "source": "live-orderbook",
        "pair": {
            "polymarket_id": str(polymarket_id),
            "kalshi_ticker": str(kalshi_ticker),
            "polymarket_question": polymarket_question,
            "kalshi_title": kalshi_title,
        },
        "polymarket": polymarket_quotes,
        "kalshi": kalshi_quotes,
        "directions": directions,
        "best_net_edge": best_direction["net_edge"] if best_direction else None,
        "best_gross_edge": best_gross_direction["gross_edge"] if best_gross_direction else None,
        "best_direction": best_direction,
        "cost_model": {
            "polymarket_taker_fee": polymarket_taker_fee,
            "kalshi_taker_fee": kalshi_taker_fee,
            "gas_cost": gas_cost,
        },
    }
