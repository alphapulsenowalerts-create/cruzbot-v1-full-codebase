"""Macro economic calendar guard — pause trading around major news."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Protocol, Sequence

from trading_bot.models import utcnow

logger = logging.getLogger(__name__)

# Default major-event keywords (case-insensitive match on title/name)
DEFAULT_MAJOR_KEYWORDS = (
    "NFP",
    "NONFARM",
    "NON-FARM",
    "CPI",
    "FOMC",
    "INTEREST RATE",
    "FED RATE",
    "FEDERAL RESERVE",
    "PPI",
    "GDP",
    "UNEMPLOYMENT",
    "JOLTS",
    "PCE",
)


class MacroEvent:
    """Simple calendar event."""

    __slots__ = ("title", "timestamp", "importance")

    def __init__(
        self,
        title: str,
        timestamp: datetime,
        importance: str = "high",
    ) -> None:
        self.title = title
        self.timestamp = (
            timestamp
            if timestamp.tzinfo
            else timestamp.replace(tzinfo=timezone.utc)
        )
        self.importance = importance


class CalendarAdapter(Protocol):
    """Pluggable economic-calendar source."""

    async def fetch_events(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> List[MacroEvent]: ...


class MockCalendarAdapter:
    """In-memory adapter for tests / offline dry-run."""

    def __init__(self, events: Optional[Sequence[MacroEvent]] = None) -> None:
        self._events: List[MacroEvent] = list(events or [])

    def set_events(self, events: Sequence[MacroEvent]) -> None:
        self._events = list(events)

    async def fetch_events(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> List[MacroEvent]:
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        return [e for e in self._events if start <= e.timestamp <= end]



class FileCalendarAdapter:
    """Load events from a local JSON file (path or file:// URL)."""

    def __init__(self, path: str) -> None:
        from pathlib import Path as _P
        p = path[7:] if path.startswith("file://") else path
        self.path = _P(p)

    async def fetch_events(
        self,
        *,
        start: "datetime",
        end: "datetime",
    ) -> "List[MacroEvent]":
        import json
        from datetime import timezone as _tz
        if not self.path.exists():
            logger.warning("Macro calendar file missing: %s", self.path)
            return []
        try:
            data = json.loads(self.path.read_text())
        except Exception as exc:
            logger.warning("Macro calendar file read failed: %s", exc)
            return []
        raw_list = data if isinstance(data, list) else data.get("events", [])
        out: List[MacroEvent] = []
        start = start if start.tzinfo else start.replace(tzinfo=_tz.utc)
        end = end if end.tzinfo else end.replace(tzinfo=_tz.utc)
        for item in raw_list or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or item.get("event") or "")
            ts_raw = item.get("time") or item.get("timestamp") or item.get("date")
            if not title or not ts_raw:
                continue
            try:
                if isinstance(ts_raw, (int, float)):
                    ts = datetime.fromtimestamp(float(ts_raw), tz=_tz.utc)
                else:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz.utc)
            except (TypeError, ValueError):
                continue
            if start <= ts <= end:
                out.append(
                    MacroEvent(
                        title=title,
                        timestamp=ts,
                        importance=str(item.get("importance") or item.get("impact") or "high"),
                    )
                )
        return out


class HttpCalendarAdapter:
    """
    Fetch events from a JSON calendar URL.

    Expected JSON shapes (best-effort):
      [{"title": "...", "time": "ISO8601", "importance": "high"}, ...]
      or {"events": [...]}
    Auth via optional API key header when MACRO_CALENDAR_API_KEY is set.
    """

    def __init__(
        self,
        url: str,
        api_key: str = "",
        *,
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    async def fetch_events(
        self,
        *,
        start: datetime,
        end: datetime,
    ) -> List[MacroEvent]:
        if not self.url:
            return []
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed — HttpCalendarAdapter no-op")
            return []

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-KEY"] = self.api_key

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(self.url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Macro calendar fetch failed: %s", exc)
            return []

        raw_list = data if isinstance(data, list) else data.get("events", [])
        out: List[MacroEvent] = []
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        for item in raw_list or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or item.get("event") or "")
            ts_raw = item.get("time") or item.get("timestamp") or item.get("date")
            if not title or not ts_raw:
                continue
            try:
                if isinstance(ts_raw, (int, float)):
                    ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if start <= ts <= end:
                out.append(
                    MacroEvent(
                        title=title,
                        timestamp=ts,
                        importance=str(item.get("importance") or item.get("impact") or "high"),
                    )
                )
        return out


def is_major_event(title: str, keywords: Sequence[str] = DEFAULT_MAJOR_KEYWORDS) -> bool:
    upper = title.upper()
    return any(k.upper() in upper for k in keywords)


def in_pause_window(
    now: datetime,
    event_ts: datetime,
    pause_minutes: int,
) -> bool:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    event_ts = event_ts if event_ts.tzinfo else event_ts.replace(tzinfo=timezone.utc)
    delta = abs((now - event_ts).total_seconds())
    return delta <= pause_minutes * 60


class MacroGuard:
    """
    Checks economic calendar and reports whether trading should pause.

    Config: MACRO_PAUSE_ENABLED, MACRO_PAUSE_MINUTES, MACRO_CALENDAR_URL / API key.
    """

    def __init__(
        self,
        adapter: CalendarAdapter,
        *,
        enabled: bool = True,
        pause_minutes: int = 15,
        keywords: Sequence[str] = DEFAULT_MAJOR_KEYWORDS,
        lookback_hours: float = 6.0,
        lookahead_hours: float = 6.0,
    ) -> None:
        self.adapter = adapter
        self.enabled = enabled
        self.pause_minutes = pause_minutes
        self.keywords = tuple(keywords)
        self.lookback_hours = lookback_hours
        self.lookahead_hours = lookahead_hours
        self._last_pause_event: Optional[MacroEvent] = None
        self._notified_keys: set[str] = set()

    @property
    def last_pause_event(self) -> Optional[MacroEvent]:
        return self._last_pause_event

    async def check_pause(
        self,
        now: Optional[datetime] = None,
    ) -> tuple[bool, Optional[MacroEvent]]:
        """
        Return (paused, event). When disabled → (False, None).
        """
        if not self.enabled:
            self._last_pause_event = None
            return False, None

        now = now or utcnow()
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        window_start = now - timedelta(hours=self.lookback_hours)
        window_end = now + timedelta(hours=self.lookahead_hours)

        try:
            events = await self.adapter.fetch_events(start=window_start, end=window_end)
        except Exception as exc:
            logger.warning("MacroGuard fetch error: %s", exc)
            return False, None

        for ev in events:
            if not is_major_event(ev.title, self.keywords):
                continue
            if in_pause_window(now, ev.timestamp, self.pause_minutes):
                self._last_pause_event = ev
                logger.warning(
                    "MACRO_PAUSE active around '%s' at %s (±%s min)",
                    ev.title,
                    ev.timestamp.isoformat(),
                    self.pause_minutes,
                )
                return True, ev

        self._last_pause_event = None
        return False, None

    def pause_notify_key(self, event: MacroEvent) -> str:
        return f"{event.title}|{event.timestamp.isoformat()}"

    def should_notify(self, event: MacroEvent) -> bool:
        key = self.pause_notify_key(event)
        if key in self._notified_keys:
            return False
        self._notified_keys.add(key)
        return True


def build_macro_guard(
    *,
    enabled: bool,
    pause_minutes: int,
    calendar_url: str = "",
    api_key: str = "",
    adapter: Optional[CalendarAdapter] = None,
) -> MacroGuard:
    if adapter is not None:
        return MacroGuard(adapter, enabled=enabled, pause_minutes=pause_minutes)
    if calendar_url:
        if calendar_url.startswith("file://") or calendar_url.endswith(".json"):
            adapter_impl: CalendarAdapter = FileCalendarAdapter(calendar_url)
        else:
            adapter_impl = HttpCalendarAdapter(calendar_url, api_key=api_key)
        return MacroGuard(
            adapter_impl,
            enabled=enabled,
            pause_minutes=pause_minutes,
        )
    return MacroGuard(
        MockCalendarAdapter(),
        enabled=enabled,
        pause_minutes=pause_minutes,
    )
