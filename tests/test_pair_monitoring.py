from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.pair_monitoring import PairTierMonitor, discovery_priority_score


def _pair(pair_id: str, confidence: float):
    return SimpleNamespace(
        pair_id=pair_id,
        verification_confidence=confidence,
        auto_approved=confidence >= 0.94,
        discovery_priority=0.0,
    )


def test_monitor_evaluates_hot_pairs_more_frequently_than_cold_pairs():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    monitor = PairTierMonitor(
        hot_limit=1,
        hot_interval=2,
        cold_interval=30,
        clock=lambda: now,
    )
    hot = _pair("hot", 0.98)
    cold = _pair("cold", 0.90)

    first = monitor.due_pairs([cold, hot])
    assert [item.pair.pair_id for item in first] == ["hot", "cold"]
    for item in first:
        monitor.mark_evaluated(item.pair.pair_id, observed_net_edge=0.0)

    now += timedelta(seconds=3)
    assert [item.pair.pair_id for item in monitor.due_pairs([cold, hot])] == ["hot"]


def test_recent_near_threshold_edge_promotes_pair_to_hot_tier():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    monitor = PairTierMonitor(
        hot_limit=1,
        hot_interval=2,
        cold_interval=30,
        clock=lambda: now,
    )
    first = _pair("first", 0.97)
    promoted = _pair("promoted", 0.80)
    monitor.mark_evaluated("promoted", observed_net_edge=0.08, opportunity=True)
    now += timedelta(seconds=3)

    due = monitor.due_pairs([first, promoted])

    assert due[0].pair.pair_id == "promoted"
    assert due[0].tier == "hot"


def test_cold_exploration_priority_uses_preflight_evidence_not_reported_volume():
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    monitor = PairTierMonitor(
        hot_limit=1,
        hot_interval=2,
        cold_interval=30,
        clock=lambda: now,
    )
    lower = _pair("lower", 0.95)
    higher = _pair("higher", 0.95)
    lower.discovery_priority = 0.2
    higher.discovery_priority = 0.9
    lower.volume = 1_000_000
    higher.volume = 0

    due = monitor.due_pairs([lower, higher])

    assert due[0].pair.pair_id == "higher"


def test_discovery_priority_rewards_family_rarity_near_expiry_and_capacity():
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)

    near = discovery_priority_score(
        family_size=1,
        event_date_key="2026-09",
        executable_capacity=20,
        max_capacity=100,
        now=now,
    )
    distant = discovery_priority_score(
        family_size=4,
        event_date_key="2028",
        executable_capacity=5,
        max_capacity=100,
        now=now,
    )

    assert near > distant
