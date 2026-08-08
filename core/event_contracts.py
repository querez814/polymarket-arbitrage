"""Event-first market selection that preserves semantic equivalence gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, Sequence
from zoneinfo import ZoneInfo

from core.cross_platform_arb import MarketPair
from core.event_calendar import ScheduledEvent


class EventPairMatcher(Protocol):
    async def find_matches(
        self, polymarket_markets: list[Any], kalshi_markets: list[Any]
    ) -> list[MarketPair]: ...


class EventContractDiscoveryError(RuntimeError):
    """Event-first discovery returned evidence outside its selected universe."""


CoverageStatus = Literal[
    "cancelled",
    "no_polymarket_contracts",
    "no_kalshi_contracts",
    "no_verified_pairs",
    "verified_pairs",
    "budget_exhausted",
]


@dataclass(frozen=True)
class EventPairLink:
    event_id: str
    event_type: str
    scheduled_at: datetime
    pair: MarketPair

    @property
    def pair_id(self) -> str:
        return self.pair.pair_id


@dataclass(frozen=True)
class EventCoverage:
    event_id: str
    event_type: str
    status: CoverageStatus
    polymarket_candidates: int
    kalshi_candidates: int
    verified_pairs: int


@dataclass(frozen=True)
class EventContractCandidate:
    event_id: str
    event_type: str
    platform: Literal["polymarket", "kalshi"]
    market_id: str
    title: str


@dataclass(frozen=True)
class EventContractDiscoveryResult:
    pairs: tuple[MarketPair, ...]
    links: tuple[EventPairLink, ...]
    coverage: tuple[EventCoverage, ...]
    candidates: tuple[EventContractCandidate, ...] = ()


class EventContractDiscovery:
    """Reserve semantic verification for contracts tied to upcoming events."""

    def __init__(
        self,
        *,
        matcher: EventPairMatcher,
        max_matcher_calls_per_cycle: int | None = None,
    ):
        if max_matcher_calls_per_cycle is not None and max_matcher_calls_per_cycle <= 0:
            raise ValueError("event matcher call budget must be positive")
        self._matcher = matcher
        self._max_matcher_calls_per_cycle = max_matcher_calls_per_cycle

    async def discover(
        self,
        *,
        events: Sequence[ScheduledEvent],
        polymarket_markets: Sequence[Any],
        kalshi_markets: Sequence[Any],
    ) -> EventContractDiscoveryResult:
        pairs_by_id: dict[str, MarketPair] = {}
        links: list[EventPairLink] = []
        coverage: list[EventCoverage] = []
        candidates: list[EventContractCandidate] = []
        matcher_calls = 0
        for event in sorted(
            events, key=lambda item: (item.scheduled_at, item.event_id)
        ):
            if event.status == "cancelled":
                coverage.append(self._coverage(event, "cancelled", 0, 0, 0))
                continue
            selected_poly = [
                market
                for market in polymarket_markets
                if _market_matches_event(event, _polymarket_text(market))
            ]
            selected_kalshi = [
                market
                for market in kalshi_markets
                if _market_matches_event(event, _kalshi_text(market))
            ]
            selected_poly.sort(key=lambda market: str(getattr(market, "market_id", "")))
            selected_kalshi.sort(key=lambda market: str(getattr(market, "ticker", "")))
            candidates.extend(
                EventContractCandidate(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    platform="polymarket",
                    market_id=str(getattr(market, "market_id", "")),
                    title=str(getattr(market, "question", "") or ""),
                )
                for market in selected_poly
            )
            candidates.extend(
                EventContractCandidate(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    platform="kalshi",
                    market_id=str(getattr(market, "ticker", "")),
                    title=str(
                        getattr(market, "matching_text", "")
                        or getattr(market, "title", "")
                        or ""
                    ),
                )
                for market in selected_kalshi
            )
            if not selected_poly:
                coverage.append(
                    self._coverage(
                        event,
                        "no_polymarket_contracts",
                        0,
                        len(selected_kalshi),
                        0,
                    )
                )
                continue
            if not selected_kalshi:
                coverage.append(
                    self._coverage(
                        event,
                        "no_kalshi_contracts",
                        len(selected_poly),
                        0,
                        0,
                    )
                )
                continue
            if (
                self._max_matcher_calls_per_cycle is not None
                and matcher_calls >= self._max_matcher_calls_per_cycle
            ):
                coverage.append(
                    self._coverage(
                        event,
                        "budget_exhausted",
                        len(selected_poly),
                        len(selected_kalshi),
                        0,
                    )
                )
                continue
            matcher_calls += 1
            verified = await self._matcher.find_matches(selected_poly, selected_kalshi)
            allowed_poly = {
                str(getattr(market, "market_id", "")) for market in selected_poly
            }
            allowed_kalshi = {
                str(getattr(market, "ticker", "")) for market in selected_kalshi
            }
            for pair in verified:
                if (
                    pair.polymarket_id not in allowed_poly
                    or pair.kalshi_ticker not in allowed_kalshi
                ):
                    raise EventContractDiscoveryError(
                        f"matcher returned pair outside event universe: {pair.pair_id}"
                    )
                if not pair.auto_approved:
                    continue
                pairs_by_id[pair.pair_id] = pair
                links.append(
                    EventPairLink(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        scheduled_at=event.scheduled_at,
                        pair=pair,
                    )
                )
            coverage.append(
                self._coverage(
                    event,
                    (
                        "verified_pairs"
                        if any(pair.auto_approved for pair in verified)
                        else "no_verified_pairs"
                    ),
                    len(selected_poly),
                    len(selected_kalshi),
                    sum(pair.auto_approved for pair in verified),
                )
            )
        return EventContractDiscoveryResult(
            pairs=tuple(pairs_by_id[key] for key in sorted(pairs_by_id)),
            links=tuple(sorted(links, key=lambda item: (item.event_id, item.pair_id))),
            coverage=tuple(coverage),
            candidates=tuple(
                sorted(
                    candidates,
                    key=lambda item: (item.event_id, item.platform, item.market_id),
                )
            ),
        )

    @staticmethod
    def _coverage(
        event: ScheduledEvent,
        status: CoverageStatus,
        polymarket_candidates: int,
        kalshi_candidates: int,
        verified_pairs: int,
    ) -> EventCoverage:
        return EventCoverage(
            event_id=event.event_id,
            event_type=event.event_type,
            status=status,
            polymarket_candidates=polymarket_candidates,
            kalshi_candidates=kalshi_candidates,
            verified_pairs=verified_pairs,
        )


_EVENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "cpi": (r"\bcpi\b", r"consumer price index"),
    "employment": (
        r"employment situation",
        r"nonfarm payroll",
        r"\bpayrolls?\b",
        r"unemployment rate",
        r"\bjobs report\b",
    ),
    "ppi": (r"\bppi\b", r"producer price index"),
    "pce": (r"\bpce\b", r"personal income and outlays"),
    "gdp": (r"\bgdp\b", r"gross domestic product"),
    "fomc": (
        r"\bfomc\b",
        r"federal reserve",
        r"\bfed\b.*\b(?:cut|hold|hike|rate)",
        r"interest rate decision",
    ),
    "retail_sales": (r"retail sales", r"retail and food services"),
    "housing": (r"housing starts", r"residential (?:construction|sales)"),
    "durable_goods": (r"durable goods",),
    "trade": (r"international trade", r"trade deficit"),
}


def _market_matches_event(event: ScheduledEvent, market_text: str) -> bool:
    normalized = _normalize(market_text)
    if not normalized:
        return False
    if event.event_type == "fed_speech":
        if not re.search(
            r"\b(?:say|speak|speech|mention|remarks?|testimony)\b", normalized
        ):
            return False
        identity_tokens = _fed_identity_tokens(event)
        return bool(identity_tokens) and any(
            re.search(rf"\b{re.escape(token)}\b", normalized)
            for token in identity_tokens
        )
    patterns = _EVENT_PATTERNS.get(event.event_type)
    if not patterns or not any(re.search(pattern, normalized) for pattern in patterns):
        return False
    period_aliases = _event_period_aliases(event)
    return bool(period_aliases) and any(
        period in normalized for period in period_aliases
    )


def _polymarket_text(market: Any) -> str:
    return " ".join(
        str(getattr(market, field, "") or "")
        for field in (
            "question",
            "event_title",
            "description",
            "resolution_source",
        )
    )


def _kalshi_text(market: Any) -> str:
    return " ".join(
        str(getattr(market, field, "") or "")
        for field in (
            "ticker",
            "matching_text",
            "title",
            "subtitle",
            "event_title",
            "rules_primary",
            "rules_secondary",
            "settlement_source",
        )
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _period_aliases(reference_period: str) -> tuple[str, ...]:
    normalized = _normalize(reference_period)
    aliases = {normalized}
    month = re.fullmatch(
        r"(january|february|march|april|may|june|july|august|september|october|november|december) (20\d{2})",
        normalized,
    )
    if month:
        aliases.add(f"{month.group(1)[:3]} {month.group(2)}")
    quarter = re.fullmatch(r"([1-4])(?:st|nd|rd|th)? quarter (20\d{2})", normalized)
    if quarter:
        aliases.update(
            {
                f"q{quarter.group(1)} {quarter.group(2)}",
                f"{quarter.group(2)} q{quarter.group(1)}",
            }
        )
    return tuple(sorted(aliases))


def _event_period_aliases(event: ScheduledEvent) -> tuple[str, ...]:
    if event.reference_period:
        return _period_aliases(event.reference_period)
    if event.event_type == "fomc":
        local = event.scheduled_at.astimezone(ZoneInfo("America/New_York"))
        return _period_aliases(local.strftime("%B %Y"))
    return ()


def _fed_identity_tokens(event: ScheduledEvent) -> tuple[str, ...]:
    normalized = _normalize(f"{event.title} {event.description}")
    stop = {
        "speech",
        "discussion",
        "testimony",
        "chair",
        "chairman",
        "governor",
        "vice",
        "supervision",
        "economic",
        "outlook",
        "federal",
        "reserve",
        "at",
        "the",
    }
    return tuple(
        token
        for token in normalized.split()
        if len(token) >= 4 and token not in stop and not token.isdigit()
    )
