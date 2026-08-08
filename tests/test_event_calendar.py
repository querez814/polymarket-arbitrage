from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.event_calendar import (
    BEACalendarSource,
    BLSCalendarSource,
    CalendarSnapshot,
    CalendarSourceError,
    CalendarSourceHealth,
    CensusCalendarSource,
    FederalReserveCalendarSource,
    FREDEconomicCalendarSource,
    HttpTextFetcher,
    OfficialEventCalendar,
    ScheduledEvent,
)
from utils.paper_trade_store import PaperTradeStore


class StaticTextFetcher:
    def __init__(self, payload: str):
        self.payload = payload
        self.requests: list[str] = []

    async def get_text(self, url: str) -> str:
        self.requests.append(url)
        return self.payload


@pytest.mark.asyncio
async def test_bls_calendar_normalizes_release_time_and_stable_identity():
    payload = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:bls-cpi-2026-07
DTSTART;TZID=America/New_York:20260812T083000
SUMMARY:Consumer Price Index for July 2026
DESCRIPTION:Scheduled national CPI release.
URL:https://www.bls.gov/news.release/cpi.nr0.htm
END:VEVENT
END:VCALENDAR
"""
    fetcher = StaticTextFetcher(payload)
    source = BLSCalendarSource(fetcher=fetcher)

    events = await source.fetch(
        start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        end=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert fetcher.requests == ["https://www.bls.gov/schedule/news_release/bls.ics"]
    assert len(events) == 1
    event = events[0]
    assert event.event_id == "bls:bls-cpi-2026-07"
    assert event.source_id == "bls"
    assert event.event_type == "cpi"
    assert event.scheduled_at == datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    assert event.reference_period == "July 2026"
    assert event.status == "scheduled"


@pytest.mark.asyncio
async def test_bea_calendar_extracts_release_rows_from_official_schedule_html():
    payload = """
    <table id="release-schedule-table">
      <thead><tr><th>Year 2026</th><th></th><th>Release</th></tr></thead>
      <tbody>
        <tr class="scheduled-releases-type-press">
          <td class="scheduled-date"><div class="release-date">August 26</div><small>8:30 AM</small></td>
          <td>News</td>
          <td class="release-title">Personal Income and Outlays, July 2026</td>
        </tr>
      </tbody>
    </table>
    """
    source = BEACalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        end=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "pce"
    assert event.scheduled_at == datetime(2026, 8, 26, 12, 30, tzinfo=timezone.utc)
    assert event.reference_period == "July 2026"
    assert event.event_id.startswith("bea:")


@pytest.mark.asyncio
async def test_census_calendar_uses_machine_sort_key_and_period_covered():
    payload = """
    <table id="calendar">
      <tr><th>Indicator</th><th>Release Date</th><th>Time</th><th>Period Covered</th></tr>
      <tr>
        <td>Advance Monthly Sales for Retail and Food Services</td>
        <td sorttable_customkey="202608140830">August 14, 2026</td>
        <td>8:30 AM</td>
        <td>July 2026</td>
        <td class="hiden">A202608140830</td>
      </tr>
    </table>
    """
    source = CensusCalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        end=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "retail_sales"
    assert event.scheduled_at == datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc)
    assert event.reference_period == "July 2026"
    assert event.event_id.startswith("census:")


@pytest.mark.asyncio
async def test_federal_reserve_calendar_normalizes_speech_json():
    payload = """{
      "events": [{
        "description": "Economic Outlook",
        "location": "At an economic luncheon",
        "title": "Speech - Governor Lisa D. Cook",
        "time": "4:05 p.m.",
        "month": "2026-08",
        "days": "5",
        "type": "Speeches"
      }],
      "announcement": []
    }"""
    source = FederalReserveCalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "fed_speech"
    assert event.title == "Speech - Governor Lisa D. Cook — Economic Outlook"
    assert event.scheduled_at == datetime(2026, 8, 5, 20, 5, tzinfo=timezone.utc)
    assert event.event_id.startswith("federal_reserve:")


@pytest.mark.asyncio
async def test_federal_reserve_calendar_uses_fomc_decision_day_for_multiday_meeting():
    payload = """{
      "events": [{
        "description": "Federal Open Market Committee Meeting",
        "location": "Washington, D.C.",
        "title": "FOMC Meeting",
        "time": "2:00 p.m.",
        "month": "2026-09",
        "days": "15,16",
        "type": "FOMC Meetings"
      }]
    }"""
    source = FederalReserveCalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert len(events) == 1
    assert events[0].event_type == "fomc"
    assert events[0].scheduled_at == datetime(2026, 9, 16, 18, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_federal_reserve_recurring_meetings_have_distinct_stable_identities():
    payload = """{
      "events": [
        {"description":"FOMC meeting","location":"Washington, D.C.","title":"FOMC Meeting","time":"2:00 p.m.","month":"2026-09","days":"15,16","type":"FOMC Meetings"},
        {"description":"FOMC meeting","location":"Washington, D.C.","title":"FOMC Meeting","time":"2:00 p.m.","month":"2026-12","days":"8,9","type":"FOMC Meetings"}
      ]
    }"""
    source = FederalReserveCalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )

    assert len(events) == 2
    assert len({event.event_id for event in events}) == 2


@pytest.mark.asyncio
async def test_calendar_http_fetcher_streams_and_rejects_oversized_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 17, request=request)

    fetcher = HttpTextFetcher(
        max_response_bytes=16,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CalendarSourceError, match="exceeds configured size"):
        await fetcher.get_text("https://www.bls.gov/calendar")


@pytest.mark.asyncio
async def test_calendar_http_fetcher_does_not_follow_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/internal"},
            request=request,
        )

    fetcher = HttpTextFetcher(transport=httpx.MockTransport(handler))

    with pytest.raises(CalendarSourceError, match="redirect"):
        await fetcher.get_text("https://www.bls.gov/calendar")


@pytest.mark.asyncio
async def test_fred_calendar_provides_working_official_bls_release_fallback():
    payload = """
    <table><tbody>
      <tr class="odd"><td colspan="2"><span style="font-weight: bold;">Wednesday August 12, 2026</span></td></tr>
      <tr><td>7:30 am</td><td><a href="/release?rid=10">Consumer Price Index</a></td></tr>
      <tr class="odd"><td colspan="2"><span style="font-weight: bold;">Thursday August 13, 2026</span></td></tr>
      <tr><td>7:30 am</td><td><a href="/release?rid=46">Producer Price Index</a></td></tr>
      <tr class="odd"><td colspan="2"><span style="font-weight: bold;">Friday September 4, 2026</span></td></tr>
      <tr><td>7:30 am</td><td><a href="/release?rid=50">Employment Situation</a></td></tr>
    </tbody></table>
    """
    source = FREDEconomicCalendarSource(fetcher=StaticTextFetcher(payload))

    events = await source.fetch(
        start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        end=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )

    assert [event.event_type for event in events] == ["cpi", "ppi", "employment"]
    assert events[0].scheduled_at == datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    assert events[0].reference_period == "July 2026"
    assert events[2].reference_period == "August 2026"


@pytest.mark.asyncio
async def test_official_calendar_isolates_one_failed_source_without_hiding_it():
    event = ScheduledEvent(
        event_id="bea:pce-july-2026",
        source_id="bea",
        external_id="pce-july-2026",
        title="Personal Income and Outlays, July 2026",
        description="",
        event_type="pce",
        scheduled_at=datetime(2026, 8, 26, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bea.gov/news/schedule",
    )

    class Source:
        def __init__(self, source_id, result=None, error=None):
            self.source_id = source_id
            self.source_url = f"https://example.com/{source_id}"
            self.result = result
            self.error = error

        async def fetch(self, **kwargs):
            if self.error:
                raise self.error
            return list(self.result or [])

    calendar = OfficialEventCalendar(
        sources=[
            Source("bea", [event]),
            Source("bls", error=RuntimeError("access denied")),
        ],
        clock=lambda: datetime(2026, 8, 8, 15, tzinfo=timezone.utc),
    )

    snapshot = await calendar.refresh(
        start=datetime(2026, 8, 8, tzinfo=timezone.utc),
        end=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert snapshot.status == "partial"
    assert snapshot.events == (event,)
    assert [(source.source_id, source.status) for source in snapshot.sources] == [
        ("bea", "fresh"),
        ("bls", "error"),
    ]
    assert snapshot.sources[1].error == "RuntimeError: access denied"


@pytest.mark.asyncio
async def test_stale_cached_event_remains_visible_but_cannot_control_cadence():
    now = datetime(2026, 8, 8, 15, tzinfo=timezone.utc)
    event = ScheduledEvent(
        event_id="bls:cpi-july-2026",
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="",
        event_type="cpi",
        scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/calendar",
    )

    class Source:
        source_id = "bls"
        source_url = event.source_url

        def __init__(self):
            self.fail = False

        async def fetch(self, **_):
            if self.fail:
                raise RuntimeError("blocked")
            return [event]

    source = Source()
    calendar = OfficialEventCalendar(sources=[source], clock=lambda: now)
    window = {
        "start": now,
        "end": now + timedelta(days=7),
    }
    fresh = await calendar.refresh(**window)
    source.fail = True
    stale = await calendar.refresh(**window)

    assert fresh.fresh_events == (event,)
    assert stale.events == (event,)
    assert stale.sources[0].status == "stale"
    assert stale.fresh_events == ()


def test_store_updates_same_event_and_appends_revision_on_reschedule(tmp_path):
    checked_at = datetime(2026, 8, 8, 15, tzinfo=timezone.utc)
    event = ScheduledEvent(
        event_id="bls:cpi-july-2026",
        source_id="bls",
        external_id="cpi-july-2026",
        title="Consumer Price Index for July 2026",
        description="",
        event_type="cpi",
        scheduled_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
        reference_period="July 2026",
        status="scheduled",
        source_url="https://www.bls.gov/schedule/news_release/bls.ics",
    )
    health = CalendarSourceHealth(
        source_id="bls",
        source_url=event.source_url,
        status="fresh",
        checked_at=checked_at,
        last_success_at=checked_at,
        event_count=1,
        error="",
    )
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    try:
        first = CalendarSnapshot(
            status="complete",
            generated_at=checked_at,
            window_start=checked_at,
            window_end=checked_at + timedelta(days=7),
            events=(event,),
            sources=(health,),
        )
        store.record_event_calendar_snapshot(first)

        moved = replace(
            event,
            scheduled_at=datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc),
        )
        second = replace(
            first,
            generated_at=checked_at + timedelta(hours=6),
            events=(moved,),
            sources=(
                replace(
                    health,
                    checked_at=checked_at + timedelta(hours=6),
                    last_success_at=checked_at + timedelta(hours=6),
                ),
            ),
        )
        store.record_event_calendar_snapshot(second)

        upcoming = store.upcoming_scheduled_events(
            start=checked_at,
            end=checked_at + timedelta(days=7),
        )
        revisions = store.scheduled_event_revisions(event.event_id)
    finally:
        store.close()

    assert len(upcoming) == 1
    assert upcoming[0]["event_id"] == event.event_id
    assert upcoming[0]["scheduled_at_utc"] == "2026-08-13T12:30:00Z"
    assert upcoming[0]["revision_count"] == 1
    assert [row["revision_number"] for row in revisions] == [0, 1]
    assert revisions[0]["scheduled_at_utc"] == "2026-08-12T12:30:00Z"
    assert revisions[1]["scheduled_at_utc"] == "2026-08-13T12:30:00Z"
