#!/usr/bin/env python3
"""
Apple Calendar MCP server.

A local, self-contained MCP server that gives Claude full CRUD access to the
macOS Calendar via EventKit — including real recurring-event operations
(modify the recurrence rule, delete a single occurrence vs. the whole series,
detach a standalone copy off a recurring series).

Runs over stdio for Claude Desktop.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

import EventKit
from Foundation import (
    NSURL,
    NSDate,
    NSRunLoop,
    NSDefaultRunLoopMode,
)
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("apple-calendar")

# ---------------------------------------------------------------------------
# EventKit store + access
# ---------------------------------------------------------------------------

_store = EventKit.EKEventStore.alloc().init()

EK_ENTITY_EVENT = EventKit.EKEntityTypeEvent
EK_SPAN_THIS = EventKit.EKSpanThisEvent
EK_SPAN_FUTURE = EventKit.EKSpanFutureEvents

# EKAuthorizationStatus values
_ST_NOT_DETERMINED = 0
_ST_RESTRICTED = 1
_ST_DENIED = 2
_ST_FULL = 3          # authorized / full access
_ST_WRITE_ONLY = 4


def _request_full_access() -> bool:
    """Trigger the TCC prompt and block (spinning the run loop) until it resolves."""
    result: dict[str, Any] = {"done": False, "granted": False}

    def handler(granted, error):  # noqa: ANN001
        result["granted"] = bool(granted)
        result["done"] = True

    if hasattr(_store, "requestFullAccessToEventsWithCompletion_"):
        _store.requestFullAccessToEventsWithCompletion_(handler)
    else:  # pragma: no cover - very old macOS
        _store.requestAccessToEntityType_completion_(EK_ENTITY_EVENT, handler)

    runloop = NSRunLoop.currentRunLoop()
    deadline = time.time() + 60
    while not result["done"] and time.time() < deadline:
        runloop.runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1)
        )
    return result["granted"]


def _ensure_access() -> None:
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EK_ENTITY_EVENT)
    if status == _ST_FULL:
        return
    if status in (_ST_RESTRICTED, _ST_DENIED):
        raise RuntimeError(
            "Calendar access is denied. Grant it in "
            "System Settings > Privacy & Security > Calendars, enable the app that "
            "launched this server (Claude), then restart it."
        )
    # not determined or write-only -> request full access
    if not _request_full_access():
        raise RuntimeError(
            "Calendar access was not granted. Approve the Calendar prompt, or enable "
            "it in System Settings > Privacy & Security > Calendars."
        )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601-ish local datetime. Accepts 'YYYY-MM-DD',
    'YYYY-MM-DDTHH:MM', 'YYYY-MM-DD HH:MM[:SS]'. Naive => local time."""
    s = value.strip().replace("Z", "")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    if "T" not in s:  # date only
        s = s + "T00:00:00"
    return datetime.fromisoformat(s)


def _to_nsdate(value: str) -> NSDate:
    return NSDate.dateWithTimeIntervalSince1970_(_parse_dt(value).timestamp())


def _from_nsdate(nsdate) -> Optional[str]:  # noqa: ANN001
    if nsdate is None:
        return None
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970()).isoformat()


# ---------------------------------------------------------------------------
# Recurrence helpers
# ---------------------------------------------------------------------------

_FREQ_BY_NAME = {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}
_FREQ_BY_VALUE = {v: k for k, v in _FREQ_BY_NAME.items()}

# EKWeekday: Sunday=1 .. Saturday=7
_WEEKDAY_BY_NAME = {
    "su": 1, "mo": 2, "tu": 3, "we": 4, "th": 5, "fr": 6, "sa": 7,
}
_WEEKDAY_NAME = {1: "SU", 2: "MO", 3: "TU", 4: "WE", 5: "TH", 6: "FR", 7: "SA"}


def _build_rule(spec: dict) -> Any:
    """Build an EKRecurrenceRule from a spec dict.

    Keys: frequency (daily|weekly|monthly|yearly), interval (int, default 1),
    days_of_week (list like ['MO','WE','FR']), days_of_month (list of ints,
    negative counts from end), count (int), end_date (ISO string).
    """
    freq_name = str(spec["frequency"]).lower()
    if freq_name not in _FREQ_BY_NAME:
        raise ValueError(f"frequency must be one of {list(_FREQ_BY_NAME)}")
    freq = _FREQ_BY_NAME[freq_name]
    interval = int(spec.get("interval", 1) or 1)

    days_of_week = None
    if spec.get("days_of_week"):
        days_of_week = []
        for d in spec["days_of_week"]:
            key = str(d).lower()[:2]
            if key not in _WEEKDAY_BY_NAME:
                raise ValueError(f"bad weekday: {d!r}")
            days_of_week.append(
                EventKit.EKRecurrenceDayOfWeek.dayOfWeek_(_WEEKDAY_BY_NAME[key])
            )

    days_of_month = None
    if spec.get("days_of_month"):
        days_of_month = [int(x) for x in spec["days_of_month"]]

    end = None
    if spec.get("count"):
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(
            int(spec["count"])
        )
    elif spec.get("end_date"):
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithEndDate_(
            _to_nsdate(spec["end_date"])
        )

    return (
        EventKit.EKRecurrenceRule.alloc()
        .initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
            freq, interval, days_of_week, days_of_month, None, None, None, None, end
        )
    )


def _summarize_rules(rules) -> Optional[list]:  # noqa: ANN001
    if not rules:
        return None
    out = []
    for r in rules:
        d: dict[str, Any] = {
            "frequency": _FREQ_BY_VALUE.get(r.frequency()),
            "interval": r.interval(),
        }
        if r.daysOfTheWeek():
            d["days_of_week"] = [_WEEKDAY_NAME.get(x.dayOfTheWeek()) for x in r.daysOfTheWeek()]
        if r.daysOfTheMonth():
            d["days_of_month"] = [int(x) for x in r.daysOfTheMonth()]
        end = r.recurrenceEnd()
        if end is not None:
            if end.occurrenceCount():
                d["count"] = end.occurrenceCount()
            elif end.endDate():
                d["end_date"] = _from_nsdate(end.endDate())
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Event lookup + serialization
# ---------------------------------------------------------------------------

def _resolve_calendar(calendar_id: Optional[str]):
    if not calendar_id:
        cal = _store.defaultCalendarForNewEvents()
        if cal is None:
            raise RuntimeError("No default calendar available; pass calendar_id.")
        return cal
    for c in _store.calendarsForEntityType_(EK_ENTITY_EVENT):
        if c.calendarIdentifier() == calendar_id or c.title() == calendar_id:
            return c
    raise ValueError(f"Calendar not found: {calendar_id!r}")


def _find_occurrence(event_id: str, occurrence_start: str):
    target = _parse_dt(occurrence_start).timestamp()
    lo = NSDate.dateWithTimeIntervalSince1970_(target - 86400)
    hi = NSDate.dateWithTimeIntervalSince1970_(target + 86400)
    pred = _store.predicateForEventsWithStartDate_endDate_calendars_(lo, hi, None)
    best, best_diff = None, 1e18
    for e in _store.eventsMatchingPredicate_(pred):
        if e.eventIdentifier() == event_id:
            diff = abs(e.startDate().timeIntervalSince1970() - target)
            if diff < best_diff:
                best, best_diff = e, diff
    if best is None or best_diff > 120:
        raise ValueError(
            f"No occurrence of {event_id} found near {occurrence_start}. "
            "Pass an occurrence_start that matches a listed occurrence."
        )
    return best


def _get_event(event_id: str, occurrence_start: Optional[str]):
    if occurrence_start:
        return _find_occurrence(event_id, occurrence_start)
    ev = _store.eventWithIdentifier_(event_id)
    if ev is None:
        raise ValueError(f"Event not found: {event_id!r}")
    return ev


def _resolve_span(event, span: str, occurrence_start: Optional[str]):  # noqa: ANN001
    """Map a friendly span onto (EKEvent, EKSpan)."""
    if not event.hasRecurrenceRules() and not event.isDetached():
        return event, EK_SPAN_THIS
    span = (span or "this").lower()
    if span == "all":
        master = _store.eventWithIdentifier_(event.eventIdentifier())
        return (master or event), EK_SPAN_FUTURE
    if span == "future":
        return event, EK_SPAN_FUTURE
    return event, EK_SPAN_THIS


def _serialize(e) -> dict:  # noqa: ANN001
    url = e.URL()
    return {
        "event_id": e.eventIdentifier(),
        "title": e.title(),
        "calendar": e.calendar().title() if e.calendar() else None,
        "calendar_id": e.calendar().calendarIdentifier() if e.calendar() else None,
        "start": _from_nsdate(e.startDate()),
        "end": _from_nsdate(e.endDate()),
        "all_day": bool(e.isAllDay()),
        "location": e.location(),
        "notes": e.notes(),
        "url": url.absoluteString() if url else None,
        "is_recurring": bool(e.hasRecurrenceRules()),
        "is_detached": bool(e.isDetached()),
        "recurrence": _summarize_rules(e.recurrenceRules()),
    }


def _save(event, span) -> None:  # noqa: ANN001
    ok, err = _store.saveEvent_span_error_(event, span, None)
    if not ok:
        raise RuntimeError(
            f"Save failed: {err.localizedDescription() if err else 'unknown error'}"
        )


def _remove(event, span) -> None:  # noqa: ANN001
    ok, err = _store.removeEvent_span_error_(event, span, None)
    if not ok:
        raise RuntimeError(
            f"Delete failed: {err.localizedDescription() if err else 'unknown error'}"
        )


def _apply_fields(e, title, start, end, all_day, location, notes, url):  # noqa: ANN001
    if title is not None:
        e.setTitle_(title)
    if all_day is not None:
        e.setAllDay_(bool(all_day))
    if start is not None:
        e.setStartDate_(_to_nsdate(start))
    if end is not None:
        e.setEndDate_(_to_nsdate(end))
    if location is not None:
        e.setLocation_(location or None)
    if notes is not None:
        e.setNotes_(notes or None)
    if url is not None:
        e.setURL_(NSURL.URLWithString_(url) if url else None)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_calendars() -> list:
    """List all calendars, with their id, title, source, and whether they're writable."""
    _ensure_access()
    default = _store.defaultCalendarForNewEvents()
    default_id = default.calendarIdentifier() if default else None
    out = []
    for c in _store.calendarsForEntityType_(EK_ENTITY_EVENT):
        out.append({
            "calendar_id": c.calendarIdentifier(),
            "title": c.title(),
            "source": c.source().title() if c.source() else None,
            "writable": bool(c.allowsContentModifications()),
            "is_default": c.calendarIdentifier() == default_id,
        })
    return out


@mcp.tool()
def list_events(
    start: str,
    end: str,
    calendar_id: Optional[str] = None,
    query: Optional[str] = None,
) -> list:
    """List events between start and end (recurring events are expanded into
    individual occurrences). Each occurrence includes its event_id and its own
    start — pass both back to update/delete a specific occurrence.

    Args:
        start: ISO datetime, e.g. '2026-07-24' or '2026-07-24T09:00'.
        end: ISO datetime (exclusive upper bound of the window).
        calendar_id: Restrict to one calendar (id or title). Default: all.
        query: Optional case-insensitive substring filter on title/location/notes.
    """
    _ensure_access()
    cals = [_resolve_calendar(calendar_id)] if calendar_id else None
    pred = _store.predicateForEventsWithStartDate_endDate_calendars_(
        _to_nsdate(start), _to_nsdate(end), cals
    )
    events = list(_store.eventsMatchingPredicate_(pred) or [])
    events.sort(key=lambda e: e.startDate().timeIntervalSince1970())
    out = [_serialize(e) for e in events]
    if query:
        q = query.lower()
        out = [
            e for e in out
            if q in " ".join(
                str(e.get(k) or "") for k in ("title", "location", "notes")
            ).lower()
        ]
    return out


@mcp.tool()
def get_event(event_id: str, occurrence_start: Optional[str] = None) -> dict:
    """Fetch full detail for a single event (or a specific occurrence if
    occurrence_start is given)."""
    _ensure_access()
    return _serialize(_get_event(event_id, occurrence_start))


@mcp.tool()
def create_event(
    title: str,
    start: str,
    end: str,
    calendar_id: Optional[str] = None,
    all_day: bool = False,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    recurrence: Optional[dict] = None,
) -> dict:
    """Create a new event and return it.

    Args:
        title: Event title.
        start: ISO start datetime.
        end: ISO end datetime.
        calendar_id: Target calendar id or title. Default: default calendar.
        all_day: True for an all-day event.
        location: Optional location string.
        notes: Optional notes/description.
        url: Optional URL to attach.
        recurrence: Optional recurrence spec. Keys:
            frequency (daily|weekly|monthly|yearly), interval (int),
            days_of_week (e.g. ['MO','WE','FR']), days_of_month (list of ints),
            count (int, total occurrences) OR end_date (ISO date).
    """
    _ensure_access()
    e = EventKit.EKEvent.eventWithEventStore_(_store)
    e.setCalendar_(_resolve_calendar(calendar_id))
    _apply_fields(e, title, start, end, all_day, location, notes, url)
    if recurrence:
        e.addRecurrenceRule_(_build_rule(recurrence))
    _save(e, EK_SPAN_THIS)
    return _serialize(e)


@mcp.tool()
def update_event(
    event_id: str,
    occurrence_start: Optional[str] = None,
    span: str = "this",
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    all_day: Optional[bool] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    url: Optional[str] = None,
    calendar_id: Optional[str] = None,
    recurrence: Optional[dict] = None,
    clear_recurrence: bool = False,
) -> dict:
    """Update fields on an event. Only the fields you pass are changed.

    For recurring events, target a specific occurrence by passing occurrence_start
    (the start of the occurrence as returned by list_events) and choose the scope
    with span:
        'this'   -> only this occurrence (detaches it from the series)
        'future' -> this occurrence and all later ones
        'all'    -> the entire series

    To change how an event recurs, pass a new `recurrence` spec (see create_event).
    To turn a recurring event into a one-off, pass clear_recurrence=True.
    Pass empty strings ('') to clear location/notes/url.
    """
    _ensure_access()
    e = _get_event(event_id, occurrence_start)
    target, ek_span = _resolve_span(e, span, occurrence_start)
    _apply_fields(target, title, start, end, all_day, location, notes, url)
    if calendar_id is not None:
        target.setCalendar_(_resolve_calendar(calendar_id))
    if clear_recurrence:
        target.setRecurrenceRules_(None)
    elif recurrence is not None:
        target.setRecurrenceRules_([_build_rule(recurrence)])
    _save(target, ek_span)
    return _serialize(target)


@mcp.tool()
def delete_event(
    event_id: str,
    occurrence_start: Optional[str] = None,
    span: str = "this",
) -> dict:
    """Delete an event or occurrence.

    For a non-recurring event, just pass event_id.
    For a recurring event, pass occurrence_start and a span:
        'this'   -> delete only that occurrence
        'future' -> delete that occurrence and all later ones
        'all'    -> delete the entire series
    """
    _ensure_access()
    e = _get_event(event_id, occurrence_start)
    target, ek_span = _resolve_span(e, span, occurrence_start)
    _remove(target, ek_span)
    return {"deleted": True, "event_id": event_id, "span": span}


@mcp.tool()
def detach_occurrence(
    event_id: str,
    occurrence_start: str,
    delete_from_series: bool = False,
    title: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    calendar_id: Optional[str] = None,
) -> dict:
    """Create a standalone, non-recurring copy of a single occurrence of a
    recurring event. The new event is fully independent of the series.

    Optionally override any field on the copy, and optionally remove that
    occurrence from the original series (delete_from_series=True) so it isn't
    duplicated.
    """
    _ensure_access()
    occ = _find_occurrence(event_id, occurrence_start)

    copy = EventKit.EKEvent.eventWithEventStore_(_store)
    copy.setCalendar_(
        _resolve_calendar(calendar_id) if calendar_id else occ.calendar()
    )
    copy.setTitle_(title if title is not None else occ.title())
    copy.setStartDate_(_to_nsdate(start) if start else occ.startDate())
    copy.setEndDate_(_to_nsdate(end) if end else occ.endDate())
    copy.setAllDay_(occ.isAllDay())
    copy.setLocation_(location if location is not None else occ.location())
    copy.setNotes_(notes if notes is not None else occ.notes())
    if occ.URL():
        copy.setURL_(occ.URL())
    _save(copy, EK_SPAN_THIS)

    if delete_from_series:
        # re-fetch: the object may have changed after the save above
        occ2 = _find_occurrence(event_id, occurrence_start)
        _remove(occ2, EK_SPAN_THIS)

    return {"created": _serialize(copy), "removed_from_series": bool(delete_from_series)}


if __name__ == "__main__":
    mcp.run()
