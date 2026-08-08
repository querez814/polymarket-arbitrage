"""Authoritative scheduled-event ingestion for event-week arbitrage.

Calendar sources normalize official schedules into stable event identities. They
do not select markets, alter trading thresholds, or authorize execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable, Literal, Protocol, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

EventType = Literal[
    "cpi",
    "employment",
    "ppi",
    "pce",
    "gdp",
    "fomc",
    "fed_speech",
    "retail_sales",
    "housing",
    "durable_goods",
    "trade",
    "other_economic",
]
EventStatus = Literal["scheduled", "cancelled"]


class CalendarSourceError(RuntimeError):
    """An official source could not provide a trustworthy schedule."""


class TextFetcher(Protocol):
    async def get_text(self, url: str) -> str: ...


class CalendarSource(Protocol):
    source_id: str
    source_url: str

    async def fetch(
        self, *, start: datetime, end: datetime
    ) -> list["ScheduledEvent"]: ...


class HttpTextFetcher:
    """Bounded HTTP adapter for fixed official calendar URLs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None,
    ):
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("calendar HTTP limits must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = int(max_response_bytes)
        self._transport = transport

    async def get_text(self, url: str) -> str:
        return await asyncio.to_thread(self._get_text_sync, url)

    def _get_text_sync(self, url: str) -> str:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise CalendarSourceError("calendar URL must be credential-free HTTPS")
        with httpx.Client(
            follow_redirects=False,
            timeout=self._timeout_seconds,
            headers={
                "User-Agent": (
                    "Nightwatch/1.0 "
                    "(+https://github.com/querez814/polymarket-arbitrage)"
                )
            },
            transport=self._transport,
        ) as client:
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise CalendarSourceError("calendar source redirect is not allowed")
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        declared_bytes = int(declared_length)
                    except ValueError as exc:
                        raise CalendarSourceError(
                            "calendar response has invalid content length"
                        ) from exc
                    if declared_bytes > self._max_response_bytes:
                        raise CalendarSourceError(
                            "calendar response exceeds configured size limit"
                        )
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise CalendarSourceError(
                            "calendar response exceeds configured size limit"
                        )
                encoding = response.encoding or "utf-8"
        return bytes(content).decode(encoding, errors="strict")


@dataclass(frozen=True)
class ScheduledEvent:
    event_id: str
    source_id: str
    external_id: str
    title: str
    description: str
    event_type: EventType
    scheduled_at: datetime
    reference_period: str
    status: EventStatus
    source_url: str

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.source_id.strip():
            raise ValueError("scheduled event identity must be non-empty")
        if not self.title.strip():
            raise ValueError("scheduled event title must be non-empty")
        if self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled event time must be timezone-aware")
        parts = urlsplit(self.source_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("scheduled event source URL must be absolute HTTP(S)")


@dataclass(frozen=True)
class CalendarSourceHealth:
    source_id: str
    source_url: str
    status: Literal["fresh", "stale", "error"]
    checked_at: datetime
    last_success_at: datetime | None
    event_count: int
    error: str


@dataclass(frozen=True)
class CalendarSnapshot:
    status: Literal["complete", "partial", "error"]
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    events: tuple[ScheduledEvent, ...]
    sources: tuple[CalendarSourceHealth, ...]

    @property
    def fresh_events(self) -> tuple[ScheduledEvent, ...]:
        """Events allowed to control cadence; stale schedules remain visible only."""
        fresh_sources = {
            source.source_id for source in self.sources if source.status == "fresh"
        }
        return tuple(event for event in self.events if event.source_id in fresh_sources)


class OfficialEventCalendar:
    """Refresh multiple official adapters while isolating source failures."""

    def __init__(
        self,
        *,
        sources: Sequence[CalendarSource],
        max_staleness: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] | None = None,
    ):
        if not sources:
            raise ValueError("official event calendar requires at least one source")
        if max_staleness <= timedelta(0):
            raise ValueError("calendar maximum staleness must be positive")
        source_ids = [source.source_id for source in sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("calendar source IDs must be unique")
        self._sources = tuple(sources)
        self._max_staleness = max_staleness
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, tuple[datetime, tuple[ScheduledEvent, ...]]] = {}

    async def refresh(self, *, start: datetime, end: datetime) -> CalendarSnapshot:
        window_start, window_end = _require_window(start, end)
        checked_at = self._now()
        results = await asyncio.gather(
            *(
                source.fetch(start=window_start, end=window_end)
                for source in self._sources
            ),
            return_exceptions=True,
        )
        events_by_id: dict[str, ScheduledEvent] = {}
        health: list[CalendarSourceHealth] = []
        fresh_sources = 0
        for source, result in zip(self._sources, results):
            if isinstance(result, BaseException):
                cached = self._cache.get(source.source_id)
                use_cache = (
                    cached is not None and checked_at - cached[0] <= self._max_staleness
                )
                cached_events = cached[1] if use_cache and cached is not None else ()
                for event in cached_events:
                    if window_start <= event.scheduled_at < window_end:
                        events_by_id[event.event_id] = event
                health.append(
                    CalendarSourceHealth(
                        source_id=source.source_id,
                        source_url=source.source_url,
                        status="stale" if use_cache else "error",
                        checked_at=checked_at,
                        last_success_at=cached[0] if cached is not None else None,
                        event_count=len(cached_events),
                        error=f"{type(result).__name__}: {result}"[:1000],
                    )
                )
                continue
            normalized = tuple(result)
            for event in normalized:
                if event.source_id != source.source_id:
                    raise CalendarSourceError(
                        f"source {source.source_id} returned event for {event.source_id}"
                    )
                prior = events_by_id.get(event.event_id)
                if prior is not None and prior != event:
                    raise CalendarSourceError(
                        f"conflicting scheduled event identity: {event.event_id}"
                    )
                events_by_id[event.event_id] = event
            self._cache[source.source_id] = (checked_at, normalized)
            fresh_sources += 1
            health.append(
                CalendarSourceHealth(
                    source_id=source.source_id,
                    source_url=source.source_url,
                    status="fresh",
                    checked_at=checked_at,
                    last_success_at=checked_at,
                    event_count=len(normalized),
                    error="",
                )
            )
        if fresh_sources == len(self._sources):
            status: Literal["complete", "partial", "error"] = "complete"
        elif fresh_sources or events_by_id:
            status = "partial"
        else:
            status = "error"
        return CalendarSnapshot(
            status=status,
            generated_at=checked_at,
            window_start=window_start,
            window_end=window_end,
            events=tuple(
                sorted(
                    events_by_id.values(),
                    key=lambda item: (item.scheduled_at, item.event_id),
                )
            ),
            sources=tuple(sorted(health, key=lambda item: item.source_id)),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("calendar clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


class BLSCalendarSource:
    """Parse the official BLS iCalendar feed."""

    source_id = "bls"
    source_url = "https://www.bls.gov/schedule/news_release/bls.ics"

    def __init__(self, *, fetcher: TextFetcher):
        self._fetcher = fetcher

    async def fetch(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        window_start, window_end = _require_window(start, end)
        payload = await self._fetcher.get_text(self.source_url)
        if "BEGIN:VCALENDAR" not in payload or "BEGIN:VEVENT" not in payload:
            raise CalendarSourceError("BLS response is not an iCalendar schedule")
        events: list[ScheduledEvent] = []
        for fields in _parse_ics_events(payload):
            start_field = next(
                ((key, value) for key, value in fields if key.startswith("DTSTART")),
                None,
            )
            if start_field is None:
                raise CalendarSourceError("BLS event is missing DTSTART")
            scheduled_at = _parse_ics_datetime(*start_field)
            if not window_start <= scheduled_at < window_end:
                continue
            title = _ics_value(fields, "SUMMARY").strip()
            if not title:
                raise CalendarSourceError("BLS event is missing SUMMARY")
            uid = _ics_value(fields, "UID").strip()
            external_id = uid or _stable_external_id(title, "")
            event_url = _ics_value(fields, "URL").strip() or self.source_url
            status: EventStatus = (
                "cancelled"
                if _ics_value(fields, "STATUS").strip().casefold() == "cancelled"
                else "scheduled"
            )
            events.append(
                ScheduledEvent(
                    event_id=f"{self.source_id}:{external_id}",
                    source_id=self.source_id,
                    external_id=external_id,
                    title=title,
                    description=_ics_value(fields, "DESCRIPTION").strip(),
                    event_type=_classify_event(title),
                    scheduled_at=scheduled_at,
                    reference_period=_reference_period(title),
                    status=status,
                    source_url=event_url,
                )
            )
        return sorted(events, key=lambda item: (item.scheduled_at, item.event_id))


class BEACalendarSource:
    """Parse the official BEA release-schedule table."""

    source_id = "bea"
    source_url = "https://www.bea.gov/news/schedule"

    def __init__(self, *, fetcher: TextFetcher):
        self._fetcher = fetcher

    async def fetch(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        window_start, window_end = _require_window(start, end)
        payload = await self._fetcher.get_text(self.source_url)
        parser = _ScheduleTableParser("release-schedule-table")
        parser.feed(payload)
        year = parser.year
        if year is None or not parser.rows:
            raise CalendarSourceError("BEA response is missing its release table")
        events: list[ScheduledEvent] = []
        for row in parser.rows:
            date_cell = _cell_with_class(row, "scheduled-date")
            title_cell = _cell_with_class(row, "release-title")
            if date_cell is None or title_cell is None:
                continue
            title = title_cell.text.strip()
            date_match = re.search(
                r"([A-Za-z]+\s+\d{1,2})\s+(\d{1,2}:\d{2}\s+[AP]M)",
                date_cell.text,
                flags=re.IGNORECASE,
            )
            if not title or date_match is None:
                raise CalendarSourceError(
                    "BEA release row is missing date, time, or title"
                )
            scheduled_at = _parse_eastern_datetime(
                f"{date_match.group(1)} {year}", date_match.group(2)
            )
            if not window_start <= scheduled_at < window_end:
                continue
            reference_period = _reference_period(title)
            external_id = _stable_external_id(title, reference_period)
            events.append(
                ScheduledEvent(
                    event_id=f"{self.source_id}:{external_id}",
                    source_id=self.source_id,
                    external_id=external_id,
                    title=title,
                    description="BEA scheduled news release",
                    event_type=_classify_event(title),
                    scheduled_at=scheduled_at,
                    reference_period=reference_period,
                    status="scheduled",
                    source_url=self.source_url,
                )
            )
        return sorted(events, key=lambda item: (item.scheduled_at, item.event_id))


class CensusCalendarSource:
    """Parse the official Census economic-indicator calendar."""

    source_id = "census"
    source_url = "https://www.census.gov/economic-indicators/calendar-listview.html"

    def __init__(self, *, fetcher: TextFetcher):
        self._fetcher = fetcher

    async def fetch(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        window_start, window_end = _require_window(start, end)
        payload = await self._fetcher.get_text(self.source_url)
        parser = _ScheduleTableParser("calendar")
        parser.feed(payload)
        if not parser.rows:
            raise CalendarSourceError("Census response is missing its calendar table")
        events: list[ScheduledEvent] = []
        for row in parser.rows:
            if len(row) < 4:
                continue
            title = row[0].text.strip()
            sort_key = row[1].attributes.get("sorttable_customkey", "").strip()
            if not re.fullmatch(r"20\d{10}", sort_key):
                continue
            try:
                local_time = datetime.strptime(sort_key, "%Y%m%d%H%M").replace(
                    tzinfo=ZoneInfo("America/New_York")
                )
            except ValueError as exc:
                raise CalendarSourceError(
                    f"invalid Census calendar sort key: {sort_key!r}"
                ) from exc
            scheduled_at = local_time.astimezone(timezone.utc)
            if not window_start <= scheduled_at < window_end:
                continue
            if not title:
                raise CalendarSourceError("Census release row is missing an indicator")
            reference_period = row[3].text.strip()
            external_id = _stable_external_id(title, reference_period)
            events.append(
                ScheduledEvent(
                    event_id=f"{self.source_id}:{external_id}",
                    source_id=self.source_id,
                    external_id=external_id,
                    title=title,
                    description="Census scheduled economic-indicator release",
                    event_type=_classify_event(title),
                    scheduled_at=scheduled_at,
                    reference_period=reference_period,
                    status="scheduled",
                    source_url=self.source_url,
                )
            )
        return sorted(events, key=lambda item: (item.scheduled_at, item.event_id))


class FREDEconomicCalendarSource:
    """Use the St. Louis Fed calendar when BLS blocks automated access.

    FRED states that these dates are published by the underlying data sources.
    The selected release IDs cover CPI, PPI, and the Employment Situation.
    """

    source_id = "fred_bls"
    source_url = "https://fred.stlouisfed.org/releases/calendar"
    _release_types: dict[str, EventType] = {
        "10": "cpi",
        "46": "ppi",
        "50": "employment",
    }
    _row_pattern = re.compile(
        r"<span[^>]*>\s*(?P<date>[A-Za-z]+\s+[A-Za-z]+\s+\d{1,2},\s+20\d{2})"
        r"\s*</span>.*?</tr>\s*<tr[^>]*>\s*<td[^>]*>\s*"
        r"(?P<time>\d{1,2}:\d{2}\s*[ap]m)\s*</td>\s*<td[^>]*>\s*"
        r"<a[^>]+href=[\"']/release\?rid=(?P<rid>\d+)[\"'][^>]*>"
        r"(?P<title>.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    def __init__(self, *, fetcher: TextFetcher):
        self._fetcher = fetcher

    async def fetch(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        window_start, window_end = _require_window(start, end)
        urls = {
            release_id: (
                f"{self.source_url}?od=asc&rid={release_id}"
                f"&vs={window_start.date().isoformat()}"
                f"&ve={window_end.date().isoformat()}"
            )
            for release_id in self._release_types
        }
        payloads = await asyncio.gather(
            *(self._fetcher.get_text(url) for url in urls.values())
        )
        events: list[ScheduledEvent] = []
        for requested_release_id, payload in zip(urls, payloads):
            if "release-dates-pager" not in payload and not self._row_pattern.search(
                payload
            ):
                raise CalendarSourceError(
                    "FRED response is missing its release calendar"
                )
            for match in self._row_pattern.finditer(payload):
                release_id = match.group("rid")
                if release_id != requested_release_id:
                    continue
                try:
                    local = datetime.strptime(
                        f"{match.group('date')} {match.group('time').upper()}",
                        "%A %B %d, %Y %I:%M %p",
                    ).replace(tzinfo=ZoneInfo("America/Chicago"))
                except ValueError as exc:
                    raise CalendarSourceError(
                        "invalid FRED release date or time"
                    ) from exc
                scheduled_at = local.astimezone(timezone.utc)
                if not window_start <= scheduled_at < window_end:
                    continue
                reference_date = local.replace(day=1) - timedelta(days=1)
                reference_period = reference_date.strftime("%B %Y")
                title = re.sub(
                    r"\s+",
                    " ",
                    html.unescape(re.sub(r"<[^>]+>", "", match.group("title"))),
                ).strip()
                external_id = f"{release_id}:{reference_date.strftime('%Y-%m')}"
                events.append(
                    ScheduledEvent(
                        event_id=f"{self.source_id}:{external_id}",
                        source_id=self.source_id,
                        external_id=external_id,
                        title=f"{title} for {reference_period}",
                        description=(
                            "Release date published by the Federal Reserve Bank of "
                            "St. Louis from the underlying data source"
                        ),
                        event_type=self._release_types[release_id],
                        scheduled_at=scheduled_at,
                        reference_period=reference_period,
                        status="scheduled",
                        source_url=f"{self.source_url}?rid={release_id}",
                    )
                )
        return sorted(events, key=lambda item: (item.scheduled_at, item.event_id))


class FederalReserveCalendarSource:
    """Parse the Federal Reserve Board's official calendar JSON."""

    source_id = "federal_reserve"
    source_url = "https://www.federalreserve.gov/json/calendar.json"
    _included_types = frozenset({"speeches", "testimony", "fomc meetings"})

    def __init__(self, *, fetcher: TextFetcher):
        self._fetcher = fetcher

    async def fetch(self, *, start: datetime, end: datetime) -> list[ScheduledEvent]:
        window_start, window_end = _require_window(start, end)
        payload = await self._fetcher.get_text(self.source_url)
        try:
            document = json.loads(payload.lstrip("\ufeff"))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CalendarSourceError("Federal Reserve response is not JSON") from exc
        rows = document.get("events") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise CalendarSourceError("Federal Reserve calendar is missing events")
        events: list[ScheduledEvent] = []
        for row in rows:
            if not isinstance(row, dict):
                raise CalendarSourceError("Federal Reserve event must be an object")
            kind = str(row.get("type", "")).strip().casefold()
            if kind not in self._included_types:
                continue
            month = str(row.get("month", "")).strip()
            day_values = [
                value.strip()
                for value in str(row.get("days", "")).split(",")
                if value.strip()
            ]
            # FOMC meetings span multiple days, but the decision and statement
            # arrive on the final listed day. That is the tradable event clock.
            if kind == "fomc meetings" and day_values:
                day_values = day_values[-1:]
            time_text = str(row.get("time", "")).strip()
            if (
                not re.fullmatch(r"20\d{2}-\d{2}", month)
                or not day_values
                or not time_text
            ):
                continue
            raw_title = re.sub(
                r"\s+", " ", html.unescape(str(row.get("title", "")))
            ).strip()
            description = re.sub(
                r"\s+", " ", html.unescape(str(row.get("description", "")))
            ).strip()
            location = re.sub(
                r"\s+", " ", html.unescape(str(row.get("location", "")))
            ).strip()
            if not raw_title:
                raise CalendarSourceError("Federal Reserve event is missing a title")
            title = (
                f"{raw_title} — {description}"
                if description and description.casefold() not in raw_title.casefold()
                else raw_title
            )
            external_id = _stable_external_id(
                raw_title,
                description,
                location,
                month,
                "" if kind == "fomc meetings" else ",".join(day_values),
            )
            for day_value in day_values:
                try:
                    local_date = datetime.strptime(
                        f"{month}-{int(day_value):02d}", "%Y-%m-%d"
                    )
                    local_time = _parse_fed_clock(time_text)
                except (ValueError, TypeError) as exc:
                    raise CalendarSourceError(
                        f"invalid Federal Reserve event date/time: {month} {day_value} {time_text}"
                    ) from exc
                scheduled_at = local_date.replace(
                    hour=local_time.hour,
                    minute=local_time.minute,
                    tzinfo=ZoneInfo("America/New_York"),
                ).astimezone(timezone.utc)
                if not window_start <= scheduled_at < window_end:
                    continue
                events.append(
                    ScheduledEvent(
                        event_id=f"{self.source_id}:{external_id}",
                        source_id=self.source_id,
                        external_id=external_id,
                        title=title,
                        description=location,
                        event_type=(
                            "fomc" if kind == "fomc meetings" else "fed_speech"
                        ),
                        scheduled_at=scheduled_at,
                        reference_period=_reference_period(f"{title} {description}"),
                        status="scheduled",
                        source_url=self.source_url,
                    )
                )
        return sorted(events, key=lambda item: (item.scheduled_at, item.event_id))


@dataclass(frozen=True)
class _HtmlCell:
    attributes: dict[str, str]
    text: str


class _ScheduleTableParser(HTMLParser):
    def __init__(self, table_id: str):
        super().__init__(convert_charrefs=True)
        self._table_id = table_id
        self._inside_table = False
        self._table_depth = 0
        self._row: list[_HtmlCell] | None = None
        self._cell_attributes: dict[str, str] | None = None
        self._cell_text: list[str] = []
        self.rows: list[list[_HtmlCell]] = []
        self.year: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table" and attributes.get("id") == self._table_id:
            self._inside_table = True
            self._table_depth = 1
            return
        if not self._inside_table:
            return
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_attributes = attributes
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._inside_table and self._cell_attributes is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_table:
            return
        if tag in {"td", "th"} and self._cell_attributes is not None:
            text = re.sub(r"\s+", " ", html.unescape(" ".join(self._cell_text))).strip()
            self._row.append(_HtmlCell(self._cell_attributes, text))  # type: ignore[union-attr]
            year_match = re.search(r"\b(20\d{2})\b", text)
            if tag == "th" and year_match:
                self.year = int(year_match.group(1))
            self._cell_attributes = None
            self._cell_text = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._inside_table = False


def _cell_with_class(row: Sequence[_HtmlCell], name: str) -> _HtmlCell | None:
    return next(
        (cell for cell in row if name in cell.attributes.get("class", "").split()),
        None,
    )


def _require_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("calendar window start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("calendar window end must be timezone-aware")
    normalized = (start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    if normalized[1] <= normalized[0]:
        raise ValueError("calendar window end must be after start")
    return normalized


def _parse_ics_events(payload: str) -> list[list[tuple[str, str]]]:
    unfolded: list[str] = []
    for raw in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)
    events: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] | None = None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise CalendarSourceError("nested VEVENT is invalid")
            current = []
            continue
        if line == "END:VEVENT":
            if current is None:
                raise CalendarSourceError("unmatched END:VEVENT")
            events.append(current)
            current = None
            continue
        if current is not None and ":" in line:
            key, value = line.split(":", 1)
            current.append((key, _unescape_ics(value)))
    if current is not None:
        raise CalendarSourceError("unterminated VEVENT")
    return events


def _ics_value(fields: Sequence[tuple[str, str]], name: str) -> str:
    for key, value in fields:
        if key.split(";", 1)[0] == name:
            return value
    return ""


def _parse_ics_datetime(key: str, value: str) -> datetime:
    parameters = {
        name.upper(): setting
        for name, setting in (
            part.split("=", 1) for part in key.split(";")[1:] if "=" in part
        )
    }
    raw = value.strip()
    formats = ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d")
    parsed: datetime | None = None
    for candidate in formats:
        try:
            parsed = datetime.strptime(raw.rstrip("Z"), candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        raise CalendarSourceError(f"unsupported ICS datetime: {raw!r}")
    zone: timezone | ZoneInfo
    if raw.endswith("Z"):
        zone = timezone.utc
    else:
        zone_name = parameters.get("TZID", "America/New_York")
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise CalendarSourceError(f"unknown ICS timezone: {zone_name}") from exc
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def _parse_eastern_datetime(date_text: str, time_text: str) -> datetime:
    try:
        parsed = datetime.strptime(
            f"{date_text.strip()} {time_text.strip().upper()}",
            "%B %d %Y %I:%M %p",
        )
    except ValueError as exc:
        raise CalendarSourceError(
            f"unsupported Eastern schedule datetime: {date_text!r} {time_text!r}"
        ) from exc
    return parsed.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _parse_fed_clock(value: str) -> datetime:
    normalized = re.sub(r"\s+", " ", value.replace(".", "")).strip().upper()
    for pattern in ("%I:%M %p", "%I %p"):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    raise ValueError(f"unsupported Federal Reserve clock: {value!r}")


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _classify_event(text: str) -> EventType:
    lowered = text.casefold()
    if "consumer price index" in lowered or re.search(r"\bcore cpi\b|\bcpi\b", lowered):
        return "cpi"
    if "employment situation" in lowered or "nonfarm payroll" in lowered:
        return "employment"
    if "producer price index" in lowered or re.search(r"\bppi\b", lowered):
        return "ppi"
    if "personal income and outlays" in lowered or re.search(r"\bpce\b", lowered):
        return "pce"
    if re.search(r"\bgdp\b|gross domestic product", lowered):
        return "gdp"
    if "fomc" in lowered or "federal open market committee" in lowered:
        return "fomc"
    if any(word in lowered for word in ("speech -", "testimony -", "discussion -")):
        return "fed_speech"
    if "retail" in lowered and ("sales" in lowered or "trade" in lowered):
        return "retail_sales"
    if any(
        word in lowered
        for word in ("housing starts", "residential construction", "home sales")
    ):
        return "housing"
    if "durable goods" in lowered:
        return "durable_goods"
    if "international trade" in lowered:
        return "trade"
    return "other_economic"


def _reference_period(text: str) -> str:
    matches = list(
        re.finditer(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b|\b[1-4](?:st|nd|rd|th) Quarter 20\d{2}\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    return matches[-1].group(0) if matches else ""


def _stable_external_id(*parts: str) -> str:
    normalized = "|".join(
        re.sub(r"\s+", " ", part.strip().casefold()) for part in parts
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
