from datetime import datetime, timezone

from utils.time_utils import to_utc_iso, utc_now, utc_now_iso


def test_utc_now_iso_ends_with_z():
    assert utc_now_iso().endswith("Z")


def test_to_utc_iso_converts_naive_datetime_to_z_suffix():
    naive = datetime(2026, 6, 29, 14, 19, 0, 123456)
    assert to_utc_iso(naive) == "2026-06-29T14:19:00.123456Z"


def test_to_utc_iso_converts_aware_datetime_to_z_suffix():
    aware = datetime(2026, 6, 29, 14, 19, 0, tzinfo=timezone.utc)
    assert to_utc_iso(aware) == "2026-06-29T14:19:00Z"


def test_utc_now_is_timezone_aware():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.tzinfo.utcoffset(now).total_seconds() == 0
