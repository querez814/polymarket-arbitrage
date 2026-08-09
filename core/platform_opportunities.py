"""Platform-first catalog and isolated shadow strategy lanes.

The module intentionally has no exchange client or order API.  It discovers
what exists on each venue, assigns bounded monitoring, and records immutable
research intents whose outcomes are measured only from later observed books.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence, cast
from collections.abc import Mapping

from kalshi_client.models import KalshiMarket, KalshiMilestone
from polymarket_client.models import Market, OrderBook, PriceLevel, TokenType
from utils.platform_opportunity_store import PlatformOpportunityStore


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlatformContract:
    contract_id: str
    venue: str
    native_id: str
    title: str
    event_id: str
    event_title: str
    category: str
    active: bool
    close_time: datetime | None
    liquidity: float
    volume: float
    rules: str
    settlement_source: str
    occurrence_at: datetime | None
    occurrence_evidence: str
    occurrence_sources: tuple[str, ...]
    catalyst_at: datetime | None
    catalyst_evidence: str
    catalyst_sources: tuple[str, ...]
    catalyst_conflict_seconds: float | None
    revision_hash: str


@dataclass(frozen=True)
class CatalystReference:
    """External timing evidence joined only after platform discovery."""

    reference_id: str
    title: str
    scheduled_at: datetime
    source: str
    authoritative: bool

    def __post_init__(self) -> None:
        if (
            not self.reference_id.strip()
            or not self.title.strip()
            or not self.source.strip()
        ):
            raise ValueError(
                "catalyst reference identity, title, and source are required"
            )


def _contract(payload: dict) -> PlatformContract:
    revision_payload = dict(payload)
    revision_payload.pop("contract_id", None)
    return PlatformContract(**payload, revision_hash=_fingerprint(revision_payload))


def _stored_contract(payload: Mapping[str, object]) -> PlatformContract:
    """Rehydrate a revision retained solely by an active political lock."""
    values = dict(payload)
    for field_name in ("close_time", "occurrence_at", "catalyst_at"):
        value = values.get(field_name)
        if isinstance(value, str):
            values[field_name] = _aware(datetime.fromisoformat(value))
    for field_name in ("occurrence_sources", "catalyst_sources"):
        values[field_name] = tuple(values.get(field_name) or ())
    return PlatformContract(**values)  # type: ignore[arg-type]


def normalize_polymarket(market: Market) -> PlatformContract:
    close = _aware(market.end_date)
    return _contract(
        {
            "contract_id": f"polymarket:{market.market_id}",
            "venue": "polymarket",
            "native_id": market.market_id,
            "title": market.question.strip(),
            "event_id": market.event_id.strip() or market.condition_id,
            "event_title": market.event_title.strip(),
            "category": market.category.strip(),
            "active": bool(market.active and not market.closed and not market.resolved),
            "close_time": close,
            "liquidity": max(0.0, float(market.liquidity)),
            "volume": max(0.0, float(market.volume_24h)),
            "rules": market.description.strip(),
            "settlement_source": (
                market.resolution_source.strip() or market.oracle.strip()
            ),
            "occurrence_at": close,
            "occurrence_evidence": "exact_venue_metadata" if close else "unknown",
            "occurrence_sources": ("polymarket.end_date",) if close else (),
            "catalyst_at": close,
            "catalyst_evidence": "exact_venue_metadata" if close else "unknown",
            "catalyst_sources": ("polymarket.end_date",) if close else (),
            "catalyst_conflict_seconds": None,
        }
    )


def normalize_kalshi(market: KalshiMarket) -> PlatformContract:
    close = _aware(market.close_time or market.expiration_time)
    catalyst = _aware(market.expiration_time or market.close_time)
    rules = "\n".join(
        part.strip()
        for part in (market.rules_primary, market.rules_secondary)
        if part.strip()
    )
    return _contract(
        {
            "contract_id": f"kalshi:{market.ticker}",
            "venue": "kalshi",
            "native_id": market.ticker,
            "title": market.title.strip(),
            "event_id": market.event_ticker.strip() or market.ticker,
            "event_title": market.event_title.strip(),
            "category": market.category.strip(),
            "active": market.is_active,
            "close_time": close,
            "liquidity": max(0.0, float(market.open_interest)),
            "volume": max(0.0, float(market.volume)),
            "rules": rules,
            "settlement_source": market.settlement_source.strip(),
            # Kalshi expiration is often a settlement/postponement deadline,
            # not an occurrence. Only a related venue milestone may set this.
            "occurrence_at": None,
            "occurrence_evidence": "unknown",
            "occurrence_sources": (),
            "catalyst_at": catalyst,
            "catalyst_evidence": "exact_venue_metadata" if catalyst else "unknown",
            "catalyst_sources": (
                ("kalshi.expiration_time",)
                if market.expiration_time is not None
                else (("kalshi.close_time",) if market.close_time is not None else ())
            ),
            "catalyst_conflict_seconds": None,
        }
    )


@dataclass(frozen=True)
class MonitoringPolicy:
    lookahead: timedelta = timedelta(days=7)
    max_hot_contracts: int = 100
    min_liquidity: float = 100.0
    min_volume: float = 100.0

    def __post_init__(self) -> None:
        if self.lookahead <= timedelta(0):
            raise ValueError("monitoring lookahead must be positive")
        if self.max_hot_contracts <= 0:
            raise ValueError("max_hot_contracts must be positive")
        if self.min_liquidity < 0 or self.min_volume < 0:
            raise ValueError("liquidity and volume floors cannot be negative")


@dataclass(frozen=True)
class PoliticalWatchPolicy:
    """Bounded, persistent selection policy for political-event research."""

    max_events: int = 4
    max_contracts_per_event: int = 6
    lookahead: timedelta = timedelta(days=7)
    warm_before: timedelta = timedelta(hours=24)
    hot_before: timedelta = timedelta(hours=1)
    warm_poll_seconds: float = 60.0
    hot_poll_seconds: float = 2.0
    event_poll_seconds: float = 2.0
    cooldown_poll_seconds: float = 10.0
    cooldown_after: timedelta = timedelta(hours=2)
    reviewed_pinned_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_events <= 0 or self.max_contracts_per_event <= 0:
            raise ValueError("political watchlist caps must be positive")
        if self.lookahead <= timedelta(0):
            raise ValueError("political watchlist lookahead must be positive")
        if not timedelta(0) < self.hot_before <= self.warm_before <= self.lookahead:
            raise ValueError(
                "political watch windows must satisfy lookahead >= warm >= hot > 0"
            )
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                self.warm_poll_seconds,
                self.hot_poll_seconds,
                self.event_poll_seconds,
                self.cooldown_poll_seconds,
            )
        ):
            raise ValueError("political watch cadences must be finite and positive")
        if self.cooldown_after < timedelta(0):
            raise ValueError("political watchlist cooldown cannot be negative")
        if any(not event_id.strip() for event_id in self.reviewed_pinned_event_ids):
            raise ValueError("reviewed pinned event IDs must be non-empty")
        if len(set(self.reviewed_pinned_event_ids)) != len(
            self.reviewed_pinned_event_ids
        ):
            raise ValueError("reviewed pinned event IDs must be unique")
        if len(self.reviewed_pinned_event_ids) > self.max_events:
            raise ValueError("reviewed pinned event IDs cannot exceed max_events")


@dataclass(frozen=True)
class PoliticalEventLock:
    event_id: str
    event_title: str
    occurrence_at: datetime
    locked_until: datetime
    selected_at: datetime
    contract_ids: tuple[str, ...]


def _event_pin_id(contract: PlatformContract) -> str:
    """Return the stable, config-facing identity of an event candidate."""
    return f"{contract.venue}:{contract.event_id}"


_POLITICAL_TERMS = frozenset(
    {
        "approval",
        "congress",
        "election",
        "electoral",
        "governor",
        "mayor",
        "parliament",
        "political",
        "politics",
        "polling",
        "president",
        "presidential",
        "prime minister",
        "senate",
        "trump",
        "vote",
        "voting",
        "white house",
    }
)


def _is_political_contract(contract: PlatformContract) -> bool:
    """Use explicit political language; never infer politics from generic dates."""
    text = " ".join((contract.title, contract.event_title, contract.category)).casefold()
    return any(term in text for term in _POLITICAL_TERMS) or (
        "approval" in text
        and any(term in text for term in ("president", "trump", "white house"))
    )


def _is_combo_contract(contract: PlatformContract) -> bool:
    """MVE/combo bundles are not single political-event contracts."""
    text = " ".join(
        (contract.native_id, contract.title, contract.event_title)
    ).casefold()
    return any(
        marker in text for marker in ("mve", "multivariate", "combo", "parlay")
    )


@dataclass(frozen=True)
class MonitoringAssignment:
    contract_id: str
    venue: str
    native_id: str
    catalyst_at: datetime | None
    reason: str
    priority_score: float
    cadence: str
    interval_seconds: float


@dataclass(frozen=True)
class MonitoringPlan:
    hot: tuple[MonitoringAssignment, ...]
    warm: tuple[MonitoringAssignment, ...]
    budget_excluded: tuple[MonitoringAssignment, ...]


@dataclass(frozen=True)
class CatalogRefresh:
    catalog_contracts: int
    revisions_written: int
    monitoring: MonitoringPlan


@dataclass(frozen=True)
class StructuralRelation:
    relation_id: str
    relation_type: str
    contract_ids: tuple[str, ...]
    invariant: str
    residual_basis_risk: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class VenueFeeSchedule:
    """Authoritative fee curve captured before a shadow intent is emitted."""

    venue: str
    fee_type: str
    rate: float
    exponent: float
    multiplier: float
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.fee_type not in {"none", "polymarket_curve", "kalshi_quadratic"}:
            raise ValueError("unsupported shadow fee schedule")
        for value in (self.rate, self.exponent, self.multiplier):
            if not math.isfinite(value) or value < 0:
                raise ValueError("fee schedule values must be finite and non-negative")
        if not self.source.strip():
            raise ValueError("fee schedule source is required")

    def fee_per_contract(self, price: float) -> float:
        return self.fee_cost(price, 1.0)

    def fee_cost(self, price: float, contracts: float) -> float:
        if not 0 < price < 1:
            raise ValueError("fee price must be inside contract bounds")
        if not math.isfinite(contracts) or contracts <= 0:
            raise ValueError("fee contracts must be finite and positive")
        if self.fee_type == "none":
            return 0.0
        if self.fee_type == "polymarket_curve":
            raw = contracts * self.rate * (price * (1 - price)) ** self.exponent
            return math.ceil(raw * 100_000 - 1e-12) / 100_000
        raw = contracts * self.multiplier * 0.07 * price * (1 - price)
        return math.ceil(raw * 10_000 - 1e-12) / 10_000


def polymarket_fee_schedule_from_market_info(
    market_info: Mapping[str, object], *, observed_at: datetime
) -> VenueFeeSchedule:
    """Parse current public CLOB fee metadata without guessing missing fields."""
    base_fee = market_info.get("tbf")
    details = market_info.get("fd")
    if details is None and base_fee == 0:
        return VenueFeeSchedule(
            "polymarket", "none", 0.0, 1.0, 0.0, observed_at, "clob_market_info"
        )
    if not isinstance(details, Mapping) or details.get("to") is not True:
        raise ValueError("Polymarket taker-only fee curve is unavailable")
    rate = float(details["r"])
    exponent = float(details["e"])
    return VenueFeeSchedule(
        "polymarket",
        "polymarket_curve",
        rate,
        exponent,
        0.0,
        observed_at,
        "clob_market_info",
    )


def kalshi_fee_schedule_from_metadata(
    *, fee_type: str, multiplier: float, observed_at: datetime, source: str
) -> VenueFeeSchedule:
    normalized = fee_type.strip().lower()
    if normalized not in {"quadratic", "quadratic_with_maker_fees"}:
        raise ValueError("unsupported Kalshi shadow taker fee type")
    return VenueFeeSchedule(
        "kalshi",
        "kalshi_quadratic",
        0.0,
        1.0,
        float(multiplier),
        observed_at,
        f"kalshi_{source}",
    )


@dataclass(frozen=True)
class ShadowIntent:
    """Immutable hypothetical action recorded before its outcome is known."""

    intent_id: str
    lane: str
    event_cluster_id: str
    market_family: str
    contract_id: str
    relation_id: str | None
    direction: str
    created_at: datetime
    expires_at: datetime
    entry_price: float
    entry_levels: tuple[tuple[float, float], ...]
    entry_leg_levels: tuple[tuple[tuple[float, float], ...], ...]
    fee_schedules: tuple[VenueFeeSchedule, ...]
    signal_strength: float
    model_version: str
    cohort_id: str
    feature_snapshot: dict[str, float]


@dataclass(frozen=True)
class ShadowMark:
    intent_id: str
    horizon_seconds: int
    observed_at: datetime
    capacity_fraction: float
    max_notional: float
    net_return: float | None
    capacity_pnl: float | None
    reason: str
    unit_pnl: float | None


@dataclass(frozen=True)
class ObservationResult:
    intents: tuple[ShadowIntent, ...] = ()
    marks: tuple[ShadowMark, ...] = ()


@dataclass(frozen=True)
class AcceptancePolicy:
    min_event_clusters: int = 50
    min_intents: int = 200
    max_drawdown: float = 50.0
    max_profit_concentration: float = 0.20
    bootstrap_samples: int = 2_000

    def __post_init__(self) -> None:
        if self.min_event_clusters <= 0 or self.min_intents <= 0:
            raise ValueError("acceptance sample floors must be positive")
        if self.max_drawdown <= 0:
            raise ValueError("acceptance drawdown budget must be positive")
        if not 0 < self.max_profit_concentration <= 1:
            raise ValueError("profit concentration must be in (0, 1]")
        if self.bootstrap_samples < 100:
            raise ValueError("bootstrap_samples must be at least 100")


@dataclass(frozen=True)
class AcceptanceReport:
    lane: str
    passed: bool
    authority: str
    event_clusters: int
    intents: int
    scored_intents: int
    net_capacity_pnl: float
    lower_95_bound: float | None
    max_drawdown: float
    max_profit_concentration: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _BookFeatures:
    observed_at: datetime
    mid: float
    spread: float
    imbalance: float
    bid_depth: float
    ask_depth: float
    trade_flow: float = 0.0
    lead_lag: float = 0.0
    volatility: float = 0.0
    seconds_to_catalyst: float = 0.0


class PlatformOpportunitySystem:
    """Coordinates catalog persistence and bounded monitoring allocation."""

    def __init__(
        self,
        *,
        store: PlatformOpportunityStore,
        monitoring_policy: MonitoringPolicy | None = None,
        political_watch_policy: PoliticalWatchPolicy | None = None,
        acceptance_policy: AcceptancePolicy | None = None,
        additional_fee_buffer_per_contract: float = 0.0,
        slippage_per_contract: float = 0.002,
        max_shadow_notional: float = 100.0,
        experiment_id: str = "platform-first-v1",
    ):
        self.store = store
        self.monitoring_policy = monitoring_policy or MonitoringPolicy()
        self.political_watch_policy = political_watch_policy
        self.acceptance_policy = acceptance_policy or AcceptancePolicy()
        self.additional_fee_buffer_per_contract = max(
            0.0, float(additional_fee_buffer_per_contract)
        )
        self.slippage_per_contract = max(0.0, float(slippage_per_contract))
        self.max_shadow_notional = float(max_shadow_notional)
        if not math.isfinite(self.max_shadow_notional) or self.max_shadow_notional <= 0:
            raise ValueError("max_shadow_notional must be finite and positive")
        if not experiment_id.strip():
            raise ValueError("experiment_id must be non-empty")
        self.experiment_id = experiment_id.strip()
        self._contracts: dict[str, PlatformContract] = {}
        self._snapshot_complete = True
        self._features: dict[str, deque[_BookFeatures]] = defaultdict(
            lambda: deque(maxlen=64)
        )
        self._intents: dict[str, ShadowIntent] = {}
        self._relations: dict[str, StructuralRelation] = {}
        self._sampled_contract_ids: set[str] = set()
        self._latest_books: dict[str, tuple[OrderBook, datetime]] = {}
        self._relation_residuals: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=64)
        )
        self._last_intent_at: dict[tuple[str, str], datetime] = {}
        self._marked: set[tuple[str, int, float]] = set()
        self._fee_schedules: dict[str, VenueFeeSchedule] = {}
        self._political_locks: dict[str, PoliticalEventLock] = {}
        self.cohort_id = (
            "cohort:"
            + _fingerprint(
                {
                    "directional_model": "microstructure-baseline-v1",
                    "relative_model": "structural-residual-baseline-v1",
                    "scoring_schema": "later-book-capacity-v1",
                    "experiment_id": self.experiment_id,
                    "fee_model": "authoritative-venue-fees-v1",
                    "slippage": self.slippage_per_contract,
                    "additional_fee_buffer": self.additional_fee_buffer_per_contract,
                    "max_shadow_notional": self.max_shadow_notional,
                    "acceptance": asdict(self.acceptance_policy),
                }
            )[:24]
        )
        self._restore_state()

    @property
    def contracts(self) -> tuple[PlatformContract, ...]:
        return tuple(self._contracts.values())

    def set_fee_schedule(self, contract_id: str, schedule: VenueFeeSchedule) -> None:
        if schedule.venue != contract_id.split(":", 1)[0]:
            raise ValueError("fee schedule venue does not match contract")
        self._fee_schedules[contract_id] = schedule

    def _restore_state(self) -> None:
        """Resume open intents, marks, and cooldowns from the current cohort."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for payload in self.store.intent_rows(cohort_id=self.cohort_id):
            created_at = datetime.fromisoformat(str(payload["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue
            expires_at = datetime.fromisoformat(str(payload["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            schedules = tuple(
                VenueFeeSchedule(
                    venue=str(item["venue"]),
                    fee_type=str(item["fee_type"]),
                    rate=float(item["rate"]),
                    exponent=float(item["exponent"]),
                    multiplier=float(item["multiplier"]),
                    observed_at=datetime.fromisoformat(str(item["observed_at"])),
                    source=str(item["source"]),
                )
                for item in payload.get("fee_schedules", [])
            )
            intent = ShadowIntent(
                intent_id=str(payload["intent_id"]),
                lane=str(payload["lane"]),
                event_cluster_id=str(payload["event_cluster_id"]),
                market_family=str(payload.get("market_family") or "unclassified"),
                contract_id=str(payload["contract_id"]),
                relation_id=(
                    str(payload["relation_id"])
                    if payload.get("relation_id") is not None
                    else None
                ),
                direction=str(payload["direction"]),
                created_at=created_at,
                expires_at=expires_at,
                entry_price=float(payload["entry_price"]),
                entry_levels=tuple(
                    (float(price), float(size))
                    for price, size in payload["entry_levels"]
                ),
                entry_leg_levels=tuple(
                    tuple((float(price), float(size)) for price, size in leg)
                    for leg in payload.get("entry_leg_levels", [])
                ),
                fee_schedules=schedules,
                signal_strength=float(payload["signal_strength"]),
                model_version=str(payload["model_version"]),
                cohort_id=str(payload["cohort_id"]),
                feature_snapshot={
                    str(key): float(value)
                    for key, value in payload.get("feature_snapshot", {}).items()
                },
            )
            self._intents[intent.intent_id] = intent
            cooldown_key = (
                (intent.relation_id, "relative_value")
                if intent.relation_id is not None
                else (intent.contract_id, intent.direction)
            )
            if cooldown_key[0] is not None:
                self._last_intent_at[(cooldown_key[0], cooldown_key[1])] = created_at
        self._marked = self.store.mark_keys(cohort_id=self.cohort_id)

    def _refresh_political_locks(self, now: datetime) -> None:
        """Keep selected events locked until cooldown, regardless of later volume."""
        policy = self.political_watch_policy
        if policy is None:
            self._political_locks = {}
            return
        retained: dict[str, PoliticalEventLock] = {}
        for payload in self.store.active_political_event_locks(now=now):
            lock = PoliticalEventLock(
                event_id=str(payload["event_id"]),
                event_title=str(payload["event_title"]),
                occurrence_at=(
                    _aware(datetime.fromisoformat(str(payload["occurrence_at"])))
                    or now
                ),
                locked_until=(
                    _aware(datetime.fromisoformat(str(payload["locked_until"])))
                    or now
                ),
                selected_at=(
                    _aware(datetime.fromisoformat(str(payload["selected_at"])))
                    or now
                ),
                contract_ids=tuple(str(item) for item in payload["contract_ids"]),
            )
            current = [
                self._contracts[contract_id]
                for contract_id in lock.contract_ids
                if contract_id in self._contracts
            ]
            if current or now >= lock.occurrence_at:
                retained[lock.event_id] = lock

        candidates: dict[str, list[PlatformContract]] = defaultdict(list)
        for contract in self._contracts.values():
            if (
                contract.active
                and (contract.occurrence_at or contract.catalyst_at) is not None
                and (contract.occurrence_at or contract.catalyst_at) >= now
                and (contract.occurrence_at or contract.catalyst_at)
                <= now + policy.lookahead
                and _is_political_contract(contract)
                and not _is_combo_contract(contract)
                and (contract.venue != "kalshi" or contract.occurrence_at is not None)
            ):
                candidates[contract.event_id].append(contract)
        ranked = sorted(
            candidates.items(),
            key=lambda item: (
                -max(contract.volume + contract.liquidity for contract in item[1]),
                item[0],
            ),
        )
        # Pins are reviewed event identities, not a separate eligibility path:
        # they must first pass the same active/political/exact-milestone/window
        # gates above.  Once eligible they consume a normal watchlist slot,
        # deterministically ahead of automatic volume ranking.
        pinned = {
            _event_pin_id(contracts[0]): (event_id, contracts)
            for event_id, contracts in candidates.items()
        }
        selected = [
            pinned[event_id]
            for event_id in policy.reviewed_pinned_event_ids
            if event_id in pinned
        ]
        selected.extend(
            item
            for item in ranked
            if _event_pin_id(item[1][0]) not in policy.reviewed_pinned_event_ids
        )
        for event_id, contracts in selected:
            if len(retained) >= policy.max_events:
                break
            if event_id in retained:
                continue
            contracts.sort(
                key=lambda contract: (
                    -(contract.volume + contract.liquidity),
                    contract.contract_id,
                )
            )
            occurrence_at = min(
                contract.occurrence_at or contract.catalyst_at
                for contract in contracts
                if (contract.occurrence_at or contract.catalyst_at) is not None
            )
            lock = PoliticalEventLock(
                event_id=event_id,
                event_title=contracts[0].event_title or contracts[0].title,
                occurrence_at=occurrence_at,
                locked_until=occurrence_at + policy.cooldown_after,
                selected_at=now,
                contract_ids=tuple(contract.contract_id for contract in contracts[: policy.max_contracts_per_event]),
            )
            retained[event_id] = lock
            self.store.upsert_political_event_lock(lock)
        self._political_locks = retained

    def refresh_catalog(
        self,
        *,
        polymarket_markets: Sequence[Market],
        kalshi_markets: Sequence[KalshiMarket],
        kalshi_milestones: Sequence[KalshiMilestone] = (),
        catalyst_references: Sequence[CatalystReference] = (),
        snapshot_complete: bool = True,
        observed_at: datetime,
    ) -> CatalogRefresh:
        observed_at = _aware(observed_at) or observed_at
        contracts = [
            *(normalize_polymarket(market) for market in polymarket_markets),
            *(normalize_kalshi(market) for market in kalshi_markets),
        ]
        contracts = self._apply_kalshi_milestones(contracts, kalshi_milestones)
        contracts = self._enrich_catalysts(contracts, catalyst_references)
        discovered = {contract.contract_id: contract for contract in contracts}
        # A bounded/partial response is a new eligibility cohort, not a delta.
        # Retaining every previously seen row turned repeated truncated pulls
        # into a 556k-row stale catalog.  The only permitted carry-over is an
        # already selected political lock, whose full observation window is a
        # deliberate research commitment rather than ordinary eligibility.
        retained_lock_ids = {
            contract_id
            for lock in self._political_locks.values()
            if lock.locked_until >= observed_at
            for contract_id in lock.contract_ids
        }
        # A restarted worker has not populated ``_political_locks`` yet. The
        # store is authoritative for active locks, so reconstruct their
        # contract ids before replacing this bounded eligibility cohort.
        retained_lock_ids.update(
            contract_id
            for lock in self.store.active_political_event_locks(now=observed_at)
            for contract_id in lock.get("contract_ids", ())
        )
        if retained_lock_ids:
            previous = self.store.latest_contract_payloads(
                retained_lock_ids - set(discovered)
            )
            discovered.update(
                {
                    contract_id: _stored_contract(payload)
                    for contract_id, payload in previous.items()
                }
            )
        self._contracts = discovered
        self._snapshot_complete = snapshot_complete
        revisions = self.store.upsert_contracts(
            contracts,
            observed_at=observed_at,
            # ``current`` mirrors this response's bounded cohort. Revisions
            # retain all prior evidence, including lock-only contracts.
            retire_absent=True,
        )
        self._refresh_political_locks(observed_at)
        # Structural eligibility is a property of the catalog metadata, not
        # of this refresh's bounded polling assignments.  Persist it before
        # planning so a capacity decision can say why one leg is not sampled.
        self.discover_structural_relations(observed_at=observed_at)
        monitoring = self.plan_monitoring(observed_at)
        # Relative-value observations need contemporaneous books for every leg.
        # Keep the structural universe aligned with the assignments the runtime
        # actually polls; ordinary warm inventory is intentionally not sampled.
        self._sampled_contract_ids = {
            assignment.contract_id for assignment in monitoring.hot
        }
        self._sampled_contract_ids.update(
            assignment.contract_id
            for assignment in monitoring.warm
            if assignment.reason == "political_event_lock"
        )
        return CatalogRefresh(
            catalog_contracts=len(self._contracts),
            revisions_written=revisions,
            monitoring=monitoring,
        )

    @staticmethod
    def _apply_kalshi_milestones(
        contracts: Sequence[PlatformContract],
        milestones: Sequence[KalshiMilestone],
    ) -> list[PlatformContract]:
        """Apply only exact Kalshi event-ticker occurrence evidence.

        Title/category matching is deliberately absent. Direct primary-event
        links win over merely related links, but conflicting primary windows
        are not evidence of a single occurrence. Duplicate records for the
        same start/end window are safe to deduplicate by milestone id.
        """
        linked: dict[str, list[KalshiMilestone]] = defaultdict(list)
        for milestone in milestones:
            for event_ticker in milestone.related_event_tickers:
                linked[event_ticker].append(milestone)
        enriched: list[PlatformContract] = []
        for contract in contracts:
            if contract.venue != "kalshi":
                enriched.append(contract)
                continue
            candidates = linked.get(contract.event_id, [])
            if not candidates:
                enriched.append(contract)
                continue
            primary_candidates = [
                item
                for item in candidates
                if contract.event_id in item.primary_event_tickers
            ]
            if primary_candidates:
                primary_windows = {
                    (item.start_time, item.end_time) for item in primary_candidates
                }
                if len(primary_windows) != 1:
                    enriched.append(contract)
                    continue
                candidates = primary_candidates
            milestone = min(
                candidates,
                key=lambda item: (
                    item.start_time,
                    item.milestone_id,
                ),
            )
            source = f"kalshi.milestone:{milestone.milestone_id}:start_date"
            enriched.append(
                replace(
                    contract,
                    occurrence_at=milestone.start_time,
                    occurrence_evidence="exact_venue_milestone",
                    occurrence_sources=(source,),
                    catalyst_at=milestone.start_time,
                    catalyst_evidence="exact_venue_milestone",
                    catalyst_sources=(source,),
                    catalyst_conflict_seconds=(
                        abs((contract.catalyst_at - milestone.start_time).total_seconds())
                        if contract.catalyst_at is not None
                        else None
                    ),
                )
            )
        return enriched

    @staticmethod
    def _title_tokens(value: str) -> set[str]:
        stop = {"will", "the", "be", "is", "a", "an", "of", "for", "to"}
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if len(token) > 2 and token not in stop
        }

    def _enrich_catalysts(
        self,
        contracts: Sequence[PlatformContract],
        references: Sequence[CatalystReference],
    ) -> list[PlatformContract]:
        """Leave timing untouched until it has a contract-specific proof.

        Calendar titles are deliberately not a join key: common words such as
        "price" and a year previously caused unrelated crypto contracts to be
        scheduled as PPI.  A future milestone/link ingestion path must provide
        a venue-native contract identifier before it can alter ``catalyst_at``.
        ``CatalystReference`` currently carries only title-level evidence, so
        it is retained for callers but cannot automatically schedule contracts.
        """
        del references
        return list(contracts)

    def plan_monitoring(self, now: datetime) -> MonitoringPlan:
        now = _aware(now) or now
        policy = self.monitoring_policy
        political_policy = self.political_watch_policy
        eligible: list[MonitoringAssignment] = []
        warm: list[MonitoringAssignment] = []
        lock_by_contract_id = {
            contract_id: lock
            for lock in self._political_locks.values()
            for contract_id in lock.contract_ids
        }
        for contract in self._contracts.values():
            lock = lock_by_contract_id.get(contract.contract_id)
            if lock is not None:
                # A lock owns the complete research window.  It deliberately
                # bypasses ordinary volume/lookahead ranking so refreshes
                # cannot silently stop pre-event baselines or cooldown marks.
                if now < lock.occurrence_at - political_policy.hot_before:
                    warm.append(
                        MonitoringAssignment(
                            contract.contract_id,
                            contract.venue,
                            contract.native_id,
                            lock.occurrence_at,
                            "political_event_lock",
                            1_000_000_000_000.0,
                            "warm",
                            political_policy.warm_poll_seconds,
                        )
                    )
                elif now < lock.occurrence_at:
                    eligible.append(
                        MonitoringAssignment(
                            contract.contract_id,
                            contract.venue,
                            contract.native_id,
                            lock.occurrence_at,
                            "political_event_lock",
                            1_000_000_000_000.0,
                            "hot",
                            political_policy.hot_poll_seconds,
                        )
                    )
                else:
                    eligible.append(
                        MonitoringAssignment(
                            contract.contract_id,
                            contract.venue,
                            contract.native_id,
                            lock.occurrence_at,
                            "political_event_lock",
                            1_000_000_000_000.0,
                            "cooldown",
                            political_policy.cooldown_poll_seconds,
                        )
                    )
                continue
            catalyst_at = contract.catalyst_at
            reason = ""
            if (
                self.political_watch_policy is not None
                and contract.contract_id not in lock_by_contract_id
            ):
                reason = "political_watchlist_not_selected"
            if not contract.active:
                reason = "inactive"
            elif contract.liquidity < policy.min_liquidity:
                reason = "below_liquidity_floor"
            elif contract.volume < policy.min_volume:
                reason = "below_volume_floor"
            elif catalyst_at is None:
                reason = "catalyst_time_unknown"
            elif catalyst_at < now:
                reason = "catalyst_elapsed"
            elif catalyst_at - now > policy.lookahead:
                reason = "outside_hot_lookahead"
            if reason:
                warm.append(
                    MonitoringAssignment(
                        contract.contract_id,
                        contract.venue,
                        contract.native_id,
                        catalyst_at,
                        reason,
                        0.0,
                        "scheduled",
                        300.0,
                    )
                )
                continue
            assert catalyst_at is not None
            seconds = max(1.0, (catalyst_at - now).total_seconds())
            if contract.catalyst_evidence == "model_inferred":
                warm.append(
                    MonitoringAssignment(
                        contract.contract_id,
                        contract.venue,
                        contract.native_id,
                        catalyst_at,
                        "model_inferred_not_hot",
                        0.0,
                        "warm",
                        60.0,
                    )
                )
                continue
            if contract.catalyst_evidence == "corroborated" and seconds <= 5 * 60:
                cadence = "burst"
                interval_seconds = 1.0
            elif seconds <= 60 * 60:
                cadence = "hot"
                interval_seconds = 2.0
            else:
                warm.append(
                    MonitoringAssignment(
                        contract.contract_id,
                        contract.venue,
                        contract.native_id,
                        catalyst_at,
                        "scheduled_until_hot_window",
                        0.0,
                        "warm" if seconds <= 24 * 60 * 60 else "scheduled",
                        60.0 if seconds <= 24 * 60 * 60 else 300.0,
                    )
                )
                continue
            evidence_weight = {
                "corroborated": 3.0,
                "exact_venue_metadata": 2.0,
                "model_inferred": 0.5,
            }.get(contract.catalyst_evidence, 0.0)
            score = (
                evidence_weight * 1_000_000
                + 100_000 / seconds
                + contract.liquidity
                + contract.volume
            )
            eligible.append(
                MonitoringAssignment(
                    contract.contract_id,
                    contract.venue,
                    contract.native_id,
                    catalyst_at,
                    "hot_lane_eligible",
                    score,
                    cadence,
                    interval_seconds,
                )
            )
        eligible.sort(key=lambda item: (-item.priority_score, item.contract_id))
        # Locked events retain their sampling slot even when unrelated volume
        # surges.  The political watch cap bounds this deliberate exception.
        locked_eligible = [
            item for item in eligible if item.reason == "political_event_lock"
        ]
        ordinary_eligible = [
            item for item in eligible if item.reason != "political_event_lock"
        ]
        hot = [*locked_eligible, *ordinary_eligible[: policy.max_hot_contracts]]
        hot_contract_ids = {item.contract_id for item in hot}
        relation_contract_ids = {
            contract_id
            for relation in self._relations.values()
            if not set(relation.contract_ids).issubset(hot_contract_ids)
            for contract_id in relation.contract_ids
        }
        excluded = [
            MonitoringAssignment(
                item.contract_id,
                item.venue,
                item.native_id,
                item.catalyst_at,
                (
                    "structural_relation_sampling_capacity"
                    if item.contract_id in relation_contract_ids
                    else "hot_lane_capacity"
                ),
                item.priority_score,
                item.cadence,
                item.interval_seconds,
            )
            for item in ordinary_eligible[policy.max_hot_contracts :]
        ]
        return MonitoringPlan(tuple(hot), tuple(warm), tuple(excluded))

    _THRESHOLD = re.compile(
        r"\b(above|over|greater than|at least|below|under|less than|at most)\s*"
        r"(?:\$)?(-?\d+(?:\.\d+)?)\s*(%|percent|points?|bps?)?",
        re.IGNORECASE,
    )

    def discover_structural_relations(
        self, *, observed_at: datetime
    ) -> tuple[StructuralRelation, ...]:
        """Admit only relations whose invariant can be derived from metadata."""
        parsed: dict[
            tuple[str, str, str], list[tuple[float, PlatformContract, str]]
        ] = defaultdict(list)
        for contract in self._contracts.values():
            match = self._THRESHOLD.search(contract.title)
            if not match:
                continue
            comparator = match.group(1).casefold()
            direction = (
                "above"
                if comparator
                in {
                    "above",
                    "over",
                    "greater than",
                    "at least",
                }
                else "below"
            )
            value = float(match.group(2))
            unit = (match.group(3) or "").casefold()
            context = self._THRESHOLD.sub("<threshold>", contract.title.casefold())
            context = " ".join(re.findall(r"[a-z]+|<threshold>", context))
            parsed[(contract.event_id, context, direction)].append(
                (value, contract, unit)
            )
        relations: list[StructuralRelation] = []
        for (_, _, direction), entries in parsed.items():
            entries.sort(key=lambda item: item[0])
            for lower, upper in zip(entries, entries[1:]):
                low_value, low_contract, low_unit = lower
                high_value, high_contract, high_unit = upper
                if low_unit != high_unit or low_value == high_value:
                    continue
                if direction == "above":
                    contract_ids = (high_contract.contract_id, low_contract.contract_id)
                    invariant = f"P(above {high_value:g}) <= P(above {low_value:g})"
                else:
                    contract_ids = (low_contract.contract_id, high_contract.contract_id)
                    invariant = f"P(below {low_value:g}) <= P(below {high_value:g})"
                relation_id = (
                    "structure:"
                    + _fingerprint(
                        {"type": "ordered_thresholds", "contracts": contract_ids}
                    )[:24]
                )
                relations.append(
                    StructuralRelation(
                        relation_id=relation_id,
                        relation_type="ordered_thresholds",
                        contract_ids=contract_ids,
                        invariant=invariant,
                        residual_basis_risk=(
                            "Venue rules, rounding, revision policy, and settlement "
                            "source must still be checked before any live pilot."
                        ),
                        evidence=(
                            low_contract.title,
                            high_contract.title,
                            "shared event_id and normalized threshold context",
                        ),
                    )
                )
        unique = {relation.relation_id: relation for relation in relations}
        result = tuple(unique.values())
        self._relations = dict(unique)
        self.store.record_relations(
            result,
            observed_at=observed_at,
            retire_absent=self._snapshot_complete,
        )
        return result

    @staticmethod
    def declare_structural_relation(
        *,
        relation_type: str,
        contract_ids: Sequence[str],
        invariant: str,
        residual_basis_risk: str,
        evidence: Sequence[str],
    ) -> StructuralRelation:
        """Create a reviewed machine-checkable relation, never from similarity alone."""
        allowed = {
            "ordered_thresholds",
            "nested_deadlines",
            "exclusive_exhaustive_buckets",
            "cross_venue_basis",
        }
        if relation_type not in allowed:
            raise ValueError("unsupported structural relation type")
        if len(set(contract_ids)) < 2:
            raise ValueError("a structural relation needs at least two contracts")
        if not invariant.strip() or not residual_basis_risk.strip() or not evidence:
            raise ValueError(
                "invariant, residual basis risk, and evidence are required"
            )
        relation_id = (
            "structure:"
            + _fingerprint(
                {
                    "type": relation_type,
                    "contracts": sorted(contract_ids),
                    "invariant": invariant,
                }
            )[:24]
        )
        return StructuralRelation(
            relation_id,
            relation_type,
            tuple(contract_ids),
            invariant.strip(),
            residual_basis_risk.strip(),
            tuple(evidence),
        )

    @staticmethod
    def _book_features(book: OrderBook, observed_at: datetime) -> _BookFeatures | None:
        bid = book.yes.best_bid
        ask = book.yes.best_ask
        if bid is None or ask is None or ask <= bid:
            return None
        bid_depth = book.yes.bids.total_size(5)
        ask_depth = book.yes.asks.total_size(5)
        total_depth = bid_depth + ask_depth
        if total_depth <= 0:
            return None
        return _BookFeatures(
            observed_at=observed_at,
            mid=(bid + ask) / 2,
            spread=ask - bid,
            imbalance=(bid_depth - ask_depth) / total_depth,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
        )

    @staticmethod
    def _levels(
        book: OrderBook, direction: str, *, entry: bool
    ) -> tuple[tuple[float, float], ...]:
        token = book.yes if direction == "yes" else book.no
        levels = token.asks.levels if entry else token.bids.levels
        return tuple((float(level.price), float(level.size)) for level in levels)

    def observe_book(
        self,
        contract_id: str,
        book: OrderBook,
        *,
        observed_at: datetime,
        trade_flow: float = 0.0,
        lead_price: float | None = None,
    ) -> ObservationResult:
        """Observe one book and return quickly; persistence can run off-thread."""
        observed_at = _aware(observed_at) or observed_at
        features = self._book_features(book, observed_at)
        if features is not None:
            recent_mids = [item.mid for item in self._features[contract_id]]
            volatility = (
                statistics.pstdev([*recent_mids[-15:], features.mid])
                if recent_mids
                else 0.0
            )
            contract = self._contracts.get(contract_id)
            seconds_to_catalyst = (
                (contract.catalyst_at - observed_at).total_seconds()
                if contract is not None and contract.catalyst_at is not None
                else 0.0
            )
            features = replace(
                features,
                trade_flow=max(-1.0, min(1.0, float(trade_flow))),
                lead_lag=(
                    float(lead_price) - features.mid if lead_price is not None else 0.0
                ),
                volatility=volatility,
                seconds_to_catalyst=seconds_to_catalyst,
            )
        marks = self._score_open_intents(
            contract_id, book, observed_at, current_features=features
        )
        self._latest_books[contract_id] = (book, observed_at)
        relation_result = self._observe_structural_relations(
            contract_id, observed_at=observed_at
        )
        marks = (*marks, *relation_result.marks)
        if features is None or contract_id not in self._contracts:
            return ObservationResult(relation_result.intents, tuple(marks))
        history = self._features[contract_id]
        prior = history[-1] if history else None
        history.append(features)
        if prior is None:
            return ObservationResult(relation_result.intents, tuple(marks))
        momentum = features.mid - prior.mid
        # This lane is a directional *reaction*: without a price movement, an
        # order-book imbalance alone cannot choose a coherent direction.  In
        # particular, do not let the old yes-if-positive-else-no fallback turn
        # zero momentum into a zero-strength NO intent.
        if not math.isfinite(momentum) or abs(momentum) <= 1e-9:
            return ObservationResult(relation_result.intents, tuple(marks))
        composite = (
            momentum
            + 0.015 * features.imbalance
            + 0.005 * features.trade_flow
            + 0.25 * features.lead_lag
        )
        aligned = (composite >= 0.012 and features.imbalance >= 0.20) or (
            composite <= -0.012 and features.imbalance <= -0.20
        )
        if not aligned or features.spread > 0.08:
            return ObservationResult(relation_result.intents, tuple(marks))
        # The same signed composite that cleared the entry threshold chooses
        # direction.  Momentum is deliberately only one input: deriving the
        # side from it alone could emit NO after strongly positive imbalance,
        # flow, or lead-lag evidence had qualified a YES reaction.
        direction = "yes" if composite > 0 else "no"
        last = self._last_intent_at.get((contract_id, direction))
        if last is not None and observed_at - last < timedelta(minutes=10):
            return ObservationResult(relation_result.intents, tuple(marks))
        entry_levels = self._levels(book, direction, entry=True)
        if not entry_levels:
            return ObservationResult(relation_result.intents, tuple(marks))
        fee_schedule = self._fee_schedules.get(contract_id)
        if fee_schedule is None:
            return ObservationResult(relation_result.intents, tuple(marks))
        contract = self._contracts[contract_id]
        expiry = min(
            observed_at + timedelta(minutes=10),
            (
                (contract.catalyst_at + timedelta(minutes=30))
                if contract.catalyst_at
                else observed_at + timedelta(minutes=10)
            ),
        )
        intent_id = (
            "intent:"
            + _fingerprint(
                {
                    "lane": "directional_reaction",
                    "contract": contract_id,
                    "created": observed_at.isoformat(),
                    "direction": direction,
                }
            )[:24]
        )
        intent = ShadowIntent(
            intent_id=intent_id,
            lane="directional_reaction",
            event_cluster_id=contract.event_id or contract.contract_id,
            market_family=(contract.category or contract.event_title or "unclassified"),
            contract_id=contract_id,
            relation_id=None,
            direction=direction,
            created_at=observed_at,
            expires_at=expiry,
            entry_price=entry_levels[0][0],
            entry_levels=entry_levels,
            entry_leg_levels=(entry_levels,),
            fee_schedules=(fee_schedule,),
            signal_strength=abs(momentum) * (1 + abs(features.imbalance)),
            model_version="microstructure-baseline-v1",
            cohort_id=self.cohort_id,
            feature_snapshot={
                "mid": features.mid,
                "momentum": momentum,
                "imbalance": features.imbalance,
                "spread": features.spread,
                "bid_depth": features.bid_depth,
                "ask_depth": features.ask_depth,
                "trade_flow": features.trade_flow,
                "cross_venue_lead_lag": features.lead_lag,
                "volatility": features.volatility,
                "seconds_to_catalyst": features.seconds_to_catalyst,
            },
        )
        self._intents[intent.intent_id] = intent
        self._last_intent_at[(contract_id, direction)] = observed_at
        self.store.record_intent(intent)
        return ObservationResult((*relation_result.intents, intent), tuple(marks))

    @staticmethod
    def _synthetic_levels(
        first: Sequence[PriceLevel], second: Sequence[PriceLevel]
    ) -> tuple[tuple[float, float], ...]:
        """Build conservative paired levels without assuming depth can be reused."""
        left = [[float(level.price), float(level.size)] for level in first]
        right = [[float(level.price), float(level.size)] for level in second]
        result: list[tuple[float, float]] = []
        left_index = right_index = 0
        while left_index < len(left) and right_index < len(right):
            size = min(left[left_index][1], right[right_index][1])
            if size <= 0:
                break
            result.append((left[left_index][0] + right[right_index][0], size))
            left[left_index][1] -= size
            right[right_index][1] -= size
            if left[left_index][1] <= 1e-9:
                left_index += 1
            if right[right_index][1] <= 1e-9:
                right_index += 1
        return tuple(result)

    def _observe_structural_relations(
        self, contract_id: str, *, observed_at: datetime
    ) -> ObservationResult:
        intents: list[ShadowIntent] = []
        marks: list[ShadowMark] = []
        for relation in self._relations.values():
            if relation.relation_type != "ordered_thresholds":
                continue
            restrictive_id, broad_id = relation.contract_ids
            # Evaluate once per paired state, on the restrictive contract update.
            if contract_id != restrictive_id:
                continue
            restrictive = self._latest_books.get(restrictive_id)
            broad = self._latest_books.get(broad_id)
            if restrictive is None or broad is None:
                continue
            restrictive_book, restrictive_at = restrictive
            broad_book, broad_at = broad
            if abs((restrictive_at - broad_at).total_seconds()) > 5:
                continue
            marks.extend(
                self._score_relation_intents(
                    relation, restrictive_book, broad_book, observed_at
                )
            )
            restrictive_mid = restrictive_book.yes.mid_price
            broad_mid = broad_book.yes.mid_price
            if restrictive_mid is None or broad_mid is None:
                continue
            residual = broad_mid - restrictive_mid
            history = self._relation_residuals[relation.relation_id]
            baseline = sum(history) / len(history) if len(history) >= 2 else None
            history.append(residual)
            if baseline is None or abs(residual - baseline) < 0.03:
                continue
            last = self._last_intent_at.get((relation.relation_id, "relative_value"))
            if last is not None and observed_at - last < timedelta(minutes=10):
                continue
            if residual < baseline:
                direction = "long_restrictive_yes_long_broad_no"
                entry_levels = self._synthetic_levels(
                    restrictive_book.yes.asks.levels,
                    broad_book.no.asks.levels,
                )
                entry_leg_levels = (
                    self._levels(restrictive_book, "yes", entry=True),
                    self._levels(broad_book, "no", entry=True),
                )
            else:
                direction = "long_broad_yes_long_restrictive_no"
                entry_levels = self._synthetic_levels(
                    broad_book.yes.asks.levels,
                    restrictive_book.no.asks.levels,
                )
                entry_leg_levels = (
                    self._levels(broad_book, "yes", entry=True),
                    self._levels(restrictive_book, "no", entry=True),
                )
            if not entry_levels:
                continue
            fee_schedules_by_contract = (
                self._fee_schedules.get(restrictive_id),
                self._fee_schedules.get(broad_id),
            )
            if any(schedule is None for schedule in fee_schedules_by_contract):
                continue
            if direction == "long_restrictive_yes_long_broad_no":
                fee_schedules = (
                    fee_schedules_by_contract[0],
                    fee_schedules_by_contract[1],
                )
            else:
                fee_schedules = (
                    fee_schedules_by_contract[1],
                    fee_schedules_by_contract[0],
                )
            restrictive_contract = self._contracts[restrictive_id]
            broad_contract = self._contracts[broad_id]
            intent_id = (
                "intent:"
                + _fingerprint(
                    {
                        "lane": "relative_value",
                        "relation": relation.relation_id,
                        "created": observed_at.isoformat(),
                        "direction": direction,
                    }
                )[:24]
            )
            intent = ShadowIntent(
                intent_id=intent_id,
                lane="relative_value",
                event_cluster_id=(
                    restrictive_contract.event_id
                    or broad_contract.event_id
                    or relation.relation_id
                ),
                market_family=(
                    restrictive_contract.category
                    or broad_contract.category
                    or restrictive_contract.event_title
                    or broad_contract.event_title
                    or "unclassified"
                ),
                contract_id=f"relation:{relation.relation_id}",
                relation_id=relation.relation_id,
                direction=direction,
                created_at=observed_at,
                expires_at=observed_at + timedelta(minutes=10),
                entry_price=entry_levels[0][0],
                entry_levels=entry_levels,
                entry_leg_levels=entry_leg_levels,
                fee_schedules=tuple(
                    schedule for schedule in fee_schedules if schedule is not None
                ),
                signal_strength=abs(residual - baseline),
                model_version="structural-residual-baseline-v1",
                cohort_id=self.cohort_id,
                feature_snapshot={
                    "residual": residual,
                    "baseline_residual": baseline,
                    "deviation": residual - baseline,
                    "paired_age_seconds": abs(
                        (restrictive_at - broad_at).total_seconds()
                    ),
                },
            )
            self._intents[intent.intent_id] = intent
            self._last_intent_at[(relation.relation_id, "relative_value")] = observed_at
            self.store.record_intent(intent)
            intents.append(intent)
        if marks:
            self.store.record_marks(marks)
        return ObservationResult(tuple(intents), tuple(marks))

    def _score_relation_intents(
        self,
        relation: StructuralRelation,
        restrictive_book: OrderBook,
        broad_book: OrderBook,
        observed_at: datetime,
    ) -> tuple[ShadowMark, ...]:
        marks: list[ShadowMark] = []
        for intent in self._intents.values():
            if intent.relation_id != relation.relation_id:
                continue
            elapsed = (observed_at - intent.created_at).total_seconds()
            if elapsed <= 0:
                continue
            if intent.direction == "long_restrictive_yes_long_broad_no":
                exit_levels = self._synthetic_levels(
                    restrictive_book.yes.bids.levels,
                    broad_book.no.bids.levels,
                )
                exit_leg_levels = (
                    self._levels(restrictive_book, "yes", entry=False),
                    self._levels(broad_book, "no", entry=False),
                )
            else:
                exit_levels = self._synthetic_levels(
                    broad_book.yes.bids.levels,
                    restrictive_book.no.bids.levels,
                )
                exit_leg_levels = (
                    self._levels(broad_book, "yes", entry=False),
                    self._levels(restrictive_book, "no", entry=False),
                )
            available = min(
                *(
                    sum(size for _, size in levels)
                    for levels in (*intent.entry_leg_levels, *exit_leg_levels)
                ),
            )
            strategy_exit_reason = None
            if observed_at >= intent.expires_at:
                strategy_exit_reason = "10m_max_hold"
            current_restrictive_mid = restrictive_book.yes.mid_price
            current_broad_mid = broad_book.yes.mid_price
            baseline = intent.feature_snapshot["baseline_residual"]
            if current_restrictive_mid is not None and current_broad_mid is not None:
                current_residual = current_broad_mid - current_restrictive_mid
                if (
                    intent.direction == "long_restrictive_yes_long_broad_no"
                    and current_residual >= baseline
                ) or (
                    intent.direction == "long_broad_yes_long_restrictive_no"
                    and current_residual <= baseline
                ):
                    strategy_exit_reason = "signal_reversal"
            top_exit = exit_levels[0][0] if exit_levels else None
            if top_exit is not None:
                top_entry_prices = tuple(
                    levels[0][0] for levels in intent.entry_leg_levels
                )
                top_exit_prices = tuple(levels[0][0] for levels in exit_leg_levels)
                top_fees = sum(
                    schedule.fee_per_contract(entry_price)
                    + schedule.fee_per_contract(exit_price)
                    for schedule, entry_price, exit_price in zip(
                        intent.fee_schedules,
                        top_entry_prices,
                        top_exit_prices,
                    )
                )
                top_return = (
                    top_exit
                    - intent.entry_price
                    - top_fees
                    - 2 * self.additional_fee_buffer_per_contract
                    - 4 * self.slippage_per_contract
                ) / intent.entry_price
                if top_return <= -0.05:
                    strategy_exit_reason = "hard_stop"
            horizons = [30, 120, 600, 1800]
            if strategy_exit_reason is not None:
                horizons.append(-1)
            for horizon in horizons:
                if horizon == -1 and strategy_exit_reason is None:
                    continue
                if elapsed < horizon:
                    continue
                for fraction in (1.0, 0.05, 0.1, 0.2):
                    key = (intent.intent_id, horizon, fraction)
                    if key in self._marked:
                        continue
                    contracts = 1.0 if fraction == 1.0 else available * fraction
                    contracts = min(
                        contracts,
                        self.max_shadow_notional / max(intent.entry_price, 0.01),
                    )
                    entry_prices = tuple(
                        self._walk(levels, contracts)
                        for levels in intent.entry_leg_levels
                    )
                    exit_prices = tuple(
                        self._walk(levels, contracts) for levels in exit_leg_levels
                    )
                    if (
                        any(price is None for price in (*entry_prices, *exit_prices))
                        or contracts <= 0
                    ):
                        unit_pnl = net_return = capacity_pnl = None
                        max_notional = 0.0
                        reason = "insufficient_later_executable_depth"
                    else:
                        resolved_entry = tuple(
                            cast(float, price) for price in entry_prices
                        )
                        resolved_exit = tuple(
                            cast(float, price) for price in exit_prices
                        )
                        entry_price = sum(resolved_entry)
                        exit_price = sum(resolved_exit)
                        fee_cost = sum(
                            schedule.fee_cost(entry_leg_price, contracts)
                            + schedule.fee_cost(exit_leg_price, contracts)
                            for schedule, entry_leg_price, exit_leg_price in zip(
                                intent.fee_schedules,
                                resolved_entry,
                                resolved_exit,
                            )
                        )
                        unit_pnl = (
                            exit_price
                            - entry_price
                            - fee_cost / contracts
                            - 2 * self.additional_fee_buffer_per_contract
                            - 4 * self.slippage_per_contract
                        )
                        net_return = unit_pnl / entry_price
                        max_notional = contracts * entry_price
                        capacity_pnl = unit_pnl * contracts
                        reason = (
                            strategy_exit_reason
                            if horizon == -1 and strategy_exit_reason is not None
                            else "later_book_executable_mark"
                        )
                    marks.append(
                        ShadowMark(
                            intent.intent_id,
                            horizon,
                            observed_at,
                            fraction,
                            max_notional,
                            net_return,
                            capacity_pnl,
                            reason,
                            unit_pnl,
                        )
                    )
                    self._marked.add(key)
        return tuple(marks)

    @staticmethod
    def _walk(levels: Sequence[tuple[float, float]], contracts: float) -> float | None:
        if not math.isfinite(contracts) or contracts <= 0:
            return None
        remaining = contracts
        value = 0.0
        for price, size in levels:
            fill = min(remaining, size)
            value += fill * price
            remaining -= fill
            if remaining <= 1e-9:
                return value / contracts
        return None

    def _score_open_intents(
        self,
        contract_id: str,
        book: OrderBook,
        observed_at: datetime,
        *,
        current_features: _BookFeatures | None,
    ) -> tuple[ShadowMark, ...]:
        marks: list[ShadowMark] = []
        fractions = (1.0, 0.05, 0.1, 0.2)
        horizons = (30, 120, 600, 1800)
        for intent in tuple(self._intents.values()):
            if intent.contract_id != contract_id or observed_at <= intent.created_at:
                continue
            elapsed = (observed_at - intent.created_at).total_seconds()
            due = [horizon for horizon in horizons if elapsed >= horizon]
            strategy_exit_reason = None
            if observed_at >= intent.expires_at:
                strategy_exit_reason = (
                    "catalyst_cooldown"
                    if intent.expires_at < intent.created_at + timedelta(minutes=10)
                    else "10m_max_hold"
                )
            exit_levels = self._levels(book, intent.direction, entry=False)
            top_exit = exit_levels[0][0] if exit_levels else None
            if top_exit is not None:
                schedule = intent.fee_schedules[0]
                top_fees = schedule.fee_per_contract(
                    intent.entry_price
                ) + schedule.fee_per_contract(top_exit)
                top_unit_return = (
                    top_exit
                    - intent.entry_price
                    - top_fees
                    - self.additional_fee_buffer_per_contract
                    - 2 * self.slippage_per_contract
                ) / intent.entry_price
                if top_unit_return <= -0.05:
                    strategy_exit_reason = "hard_stop"
            if current_features is not None:
                reversal = (
                    intent.direction == "yes" and current_features.imbalance <= -0.25
                ) or (intent.direction == "no" and current_features.imbalance >= 0.25)
                if reversal:
                    strategy_exit_reason = "signal_reversal"
            if strategy_exit_reason is not None:
                due.append(-1)
            if not due:
                continue
            entry_capacity = sum(size for _, size in intent.entry_levels)
            exit_capacity = sum(size for _, size in exit_levels)
            available = min(entry_capacity, exit_capacity)
            for horizon in due:
                for fraction in fractions:
                    key = (intent.intent_id, horizon, fraction)
                    if key in self._marked:
                        continue
                    contracts = 1.0 if fraction == 1.0 else available * fraction
                    contracts = min(
                        contracts,
                        self.max_shadow_notional / max(intent.entry_price, 0.01),
                    )
                    entry_price = self._walk(intent.entry_levels, contracts)
                    exit_price = self._walk(exit_levels, contracts)
                    if entry_price is None or exit_price is None or contracts <= 0:
                        unit_pnl = net_return = capacity_pnl = None
                        max_notional = 0.0
                        reason = "insufficient_later_executable_depth"
                    else:
                        schedule = intent.fee_schedules[0]
                        fee_cost = schedule.fee_cost(
                            entry_price, contracts
                        ) + schedule.fee_cost(exit_price, contracts)
                        unit_pnl = (
                            exit_price
                            - entry_price
                            - fee_cost / contracts
                            - self.additional_fee_buffer_per_contract
                            - 2 * self.slippage_per_contract
                        )
                        net_return = unit_pnl / entry_price
                        max_notional = contracts * entry_price
                        capacity_pnl = unit_pnl * contracts
                        reason = (
                            strategy_exit_reason
                            if horizon == -1 and strategy_exit_reason is not None
                            else "later_book_executable_mark"
                        )
                    mark = ShadowMark(
                        intent.intent_id,
                        horizon,
                        observed_at,
                        fraction,
                        max_notional,
                        net_return,
                        capacity_pnl,
                        reason,
                        unit_pnl,
                    )
                    marks.append(mark)
                    self._marked.add(key)
        self.store.record_marks(marks)
        return tuple(marks)

    def acceptance_report(self, lane: str) -> AcceptanceReport:
        """Compute pre-registered lane proof from later 10-minute 10% marks."""
        intents = self.store.intent_rows(lane=lane, cohort_id=self.cohort_id)
        marks = self.store.mark_rows(
            lane=lane,
            horizon_seconds=-1,
            cohort_id=self.cohort_id,
        )
        clusters = {row["event_cluster_id"] for row in intents}
        reasons: list[str] = []
        policy = self.acceptance_policy
        if len(clusters) < policy.min_event_clusters:
            reasons.append(f"fewer_than_{policy.min_event_clusters}_event_clusters")
        if len(intents) < policy.min_intents:
            reasons.append(f"fewer_than_{policy.min_intents}_intents")
        valid = [row for row in marks if row.get("capacity_pnl") is not None]
        scored_intents = {str(row["intent_id"]) for row in valid}
        scored_clusters = {str(row["event_cluster_id"]) for row in valid}
        if len(scored_intents) < policy.min_intents:
            reasons.append(f"fewer_than_{policy.min_intents}_scored_intents")
        if len(scored_clusters) < policy.min_event_clusters:
            reasons.append(
                f"fewer_than_{policy.min_event_clusters}_scored_event_clusters"
            )
        pnl = [float(row["capacity_pnl"]) for row in valid]
        total = sum(pnl)
        if not pnl or total <= 0:
            reasons.append("non_positive_net_capacity_pnl")
        running = peak = 0.0
        max_drawdown = 0.0
        for value in pnl:
            running += value
            peak = max(peak, running)
            max_drawdown = max(max_drawdown, peak - running)
        if max_drawdown > policy.max_drawdown:
            reasons.append("drawdown_budget_exceeded")
        cluster_profit: dict[str, float] = defaultdict(float)
        family_profit: dict[str, float] = defaultdict(float)
        time_profit: dict[str, float] = defaultdict(float)
        for row in valid:
            value = float(row["capacity_pnl"])
            cluster_profit[str(row["event_cluster_id"])] += value
            family_profit[str(row.get("market_family") or "unclassified")] += value
            created_at = datetime.fromisoformat(str(row["created_at"]))
            iso_year, iso_week, _ = created_at.isocalendar()
            time_profit[f"{iso_year}-W{iso_week:02d}"] += value
        positive = [value for value in cluster_profit.values() if value > 0]
        concentration = max(positive) / sum(positive) if positive else None
        if concentration is None or concentration > policy.max_profit_concentration:
            reasons.append("single_event_profit_concentration_exceeded")
        if len(family_profit) < 2:
            reasons.append("fewer_than_2_market_families")
        elif any(value <= 0 for value in family_profit.values()):
            reasons.append("non_positive_market_family")
        if len(time_profit) < 2:
            reasons.append("fewer_than_2_time_buckets")
        elif any(value <= 0 for value in time_profit.values()):
            reasons.append("non_positive_time_bucket")
        lower = self._clustered_bootstrap_lower(valid, policy.bootstrap_samples)
        if lower is None or lower <= 0:
            reasons.append("lower_95_clustered_bootstrap_not_positive")
        return AcceptanceReport(
            lane=lane,
            passed=not reasons,
            authority="bounded_live_pilot_eligible" if not reasons else "shadow_only",
            event_clusters=len(scored_clusters),
            intents=len(intents),
            scored_intents=len(scored_intents),
            net_capacity_pnl=total,
            lower_95_bound=lower,
            max_drawdown=max_drawdown,
            max_profit_concentration=concentration,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _clustered_bootstrap_lower(marks: Sequence[dict], samples: int) -> float | None:
        grouped: dict[str, list[float]] = defaultdict(list)
        for mark in marks:
            cluster = str(mark.get("event_cluster_id") or "")
            if cluster:
                grouped[cluster].append(float(mark["capacity_pnl"]))
        cluster_pnl = [sum(values) for values in grouped.values()]
        if len(cluster_pnl) < 2:
            return None
        rng = random.Random(843)
        totals = sorted(
            sum(rng.choice(cluster_pnl) for _ in cluster_pnl) for _ in range(samples)
        )
        return totals[max(0, int(samples * 0.025) - 1)]

    def dashboard_summary(self) -> dict:
        counts = self.store.summary(cohort_id=self.cohort_id)
        now = datetime.now(timezone.utc)
        locks = []
        for lock in sorted(
            self._political_locks.values(),
            key=lambda item: (item.occurrence_at, item.event_id),
        ):
            state = (
                "warm"
                if now < lock.occurrence_at - timedelta(hours=1)
                else "hot"
                if now < lock.occurrence_at
                else "cooldown"
                if now <= lock.locked_until
                else "expired"
            )
            locks.append(
                {
                    "event_id": lock.event_id,
                    "event_title": lock.event_title,
                    "occurrence_at": lock.occurrence_at.isoformat(),
                    "locked_until": lock.locked_until.isoformat(),
                    "state": state,
                    "contract_ids": list(lock.contract_ids),
                    "sampled_contract_ids": [
                        contract_id
                        for contract_id in lock.contract_ids
                        if contract_id in self._sampled_contract_ids
                    ],
                }
            )
        return {
            "enabled": True,
            "mode": "shadow_only",
            "experiment_id": self.experiment_id,
            "cohort_id": self.cohort_id,
            "fee_model": "authoritative_venue_metadata",
            "fee_covered_contracts": len(self._fee_schedules),
            "catalog": {
                "contracts": counts["current"],
                "revisions": counts["revisions"],
            },
            "relations": counts["relations"],
            "intents": counts["intents"],
            "marks": counts["marks"],
            "political_event_locks": locks,
            "sampled_contract_ids": sorted(self._sampled_contract_ids),
            "research_pnl": self.store.research_mark_summary(cohort_id=self.cohort_id),
        }
