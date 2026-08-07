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
    NSDateComponents,
    NSRunLoop,
    NSDefaultRunLoopMode,
)
from AppKit import NSColor, NSColorSpace
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


def _calendar_by_id(calendar_id: str):
    """Strict lookup by calendar identifier only (no title fuzzy-match).
    Used for destructive/color ops so a title can't accidentally match."""
    for c in _store.calendarsForEntityType_(EK_ENTITY_EVENT):
        if c.calendarIdentifier() == calendar_id:
            return c
    raise ValueError(f"No calendar with id {calendar_id!r}")


# EKSourceType: local=0, exchange=1, caldav(iCloud)=2, mobileme=3, subscribed=4, birthdays=5
def _resolve_source(source_hint: Optional[str]):
    sources = list(_store.sources() or [])
    if source_hint:
        for s in sources:
            if s.title() == source_hint:
                return s
        raise ValueError(
            f"Source {source_hint!r} not found. Available: "
            f"{[s.title() for s in sources]}"
        )
    # default: the source that owns the default calendar
    dc = _store.defaultCalendarForNewEvents()
    if dc is not None and dc.source() is not None:
        return dc.source()
    for wanted in (2, 0):  # prefer iCloud (CalDAV), then Local
        for s in sources:
            if s.sourceType() == wanted:
                return s
    if sources:
        return sources[0]
    raise RuntimeError("No calendar source available to create a calendar in.")


def _nscolor_from_hex(value: str):
    h = value.strip().lstrip("#")
    if len(h) != 6:
        raise ValueError(f"color must be a hex string like '#FF3B30', got {value!r}")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def _hex_from_calendar(cal) -> Optional[str]:  # noqa: ANN001
    ns = None
    try:
        ns = cal.color()
    except Exception:
        ns = None
    if ns is None:
        try:
            cg = cal.CGColor()
            if cg is not None:
                ns = NSColor.colorWithCGColor_(cg)
        except Exception:
            ns = None
    if ns is None:
        return None
    rgb = ns.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
    if rgb is None:
        return None
    return "#%02X%02X%02X" % (
        round(rgb.redComponent() * 255),
        round(rgb.greenComponent() * 255),
        round(rgb.blueComponent() * 255),
    )


def _set_calendar_color(cal, value: str) -> None:  # noqa: ANN001
    nscolor = _nscolor_from_hex(value)
    try:
        cal.setColor_(nscolor)
    except Exception:
        cal.setCGColor_(nscolor.CGColor())


def _serialize_calendar(cal) -> dict:  # noqa: ANN001
    default = _store.defaultCalendarForNewEvents()
    default_id = default.calendarIdentifier() if default else None
    return {
        "calendar_id": cal.calendarIdentifier(),
        "title": cal.title(),
        "source": cal.source().title() if cal.source() else None,
        "color": _hex_from_calendar(cal),
        "writable": bool(cal.allowsContentModifications()),
        "is_default": cal.calendarIdentifier() == default_id,
    }


def _save_calendar(cal) -> None:  # noqa: ANN001
    ok, err = _store.saveCalendar_commit_error_(cal, True, None)
    if not ok:
        raise RuntimeError(
            f"Calendar save failed: "
            f"{err.localizedDescription() if err else 'unknown error'}"
        )


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
    """List all calendars, with their id, title, source, color (hex), whether
    they're writable, and which is the default."""
    _ensure_access()
    return [
        _serialize_calendar(c)
        for c in _store.calendarsForEntityType_(EK_ENTITY_EVENT)
    ]


@mcp.tool()
def create_calendar(
    title: str,
    color: Optional[str] = None,
    source: Optional[str] = None,
) -> dict:
    """Create a new calendar and return it.

    Args:
        title: Name for the new calendar.
        color: Optional hex color, e.g. '#FF3B30'.
        source: Optional account/source title to create it in (e.g. 'iCloud',
            'On My Mac'). Default: the source of your default calendar. Use
            list_calendars to see which sources exist.
    """
    _ensure_access()
    cal = EventKit.EKCalendar.calendarForEntityType_eventStore_(EK_ENTITY_EVENT, _store)
    cal.setTitle_(title)
    cal.setSource_(_resolve_source(source))
    if color:
        _set_calendar_color(cal, color)
    _save_calendar(cal)
    return _serialize_calendar(cal)


@mcp.tool()
def set_calendar_color(calendar_id: str, color: str) -> dict:
    """Set a calendar's color. `color` is a hex string like '#34C759'.
    Requires the calendar's id (from list_calendars), not its title."""
    _ensure_access()
    cal = _calendar_by_id(calendar_id)
    if not cal.allowsContentModifications():
        raise ValueError("That calendar is read-only/subscribed; its color can't be changed.")
    _set_calendar_color(cal, color)
    _save_calendar(cal)
    return _serialize_calendar(cal)


@mcp.tool()
def delete_calendar(calendar_id: str) -> dict:
    """Permanently delete a calendar AND all events in it. Irreversible.

    Requires the calendar's exact id (from list_calendars), never a title, so it
    can't fire on a fuzzy match. Refuses read-only/subscribed calendars.
    """
    _ensure_access()
    cal = _calendar_by_id(calendar_id)
    if not cal.allowsContentModifications():
        raise ValueError("Refusing to delete a read-only/subscribed calendar.")
    title = cal.title()
    ok, err = _store.removeCalendar_commit_error_(cal, True, None)
    if not ok:
        raise RuntimeError(
            f"Delete failed: {err.localizedDescription() if err else 'unknown error'}"
        )
    return {"deleted": True, "calendar_id": calendar_id, "title": title}


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


# ---------------------------------------------------------------------------
# Reminders  (EKReminder — same store, separate permission, async fetch)
# ---------------------------------------------------------------------------

EK_ENTITY_REMINDER = EventKit.EKEntityTypeReminder
_UNDEF = 9223372036854775807  # NSDateComponentUndefined (NSIntegerMax)

_PRIORITY_TO_INT = {"none": 0, "high": 1, "medium": 5, "low": 9}


def _request_full_reminders_access() -> bool:
    """Trigger the Reminders TCC prompt and block until it resolves."""
    result: dict[str, Any] = {"done": False, "granted": False}

    def handler(granted, error):  # noqa: ANN001
        result["granted"] = bool(granted)
        result["done"] = True

    if hasattr(_store, "requestFullAccessToRemindersWithCompletion_"):
        _store.requestFullAccessToRemindersWithCompletion_(handler)
    else:  # pragma: no cover - very old macOS
        _store.requestAccessToEntityType_completion_(EK_ENTITY_REMINDER, handler)

    runloop = NSRunLoop.currentRunLoop()
    deadline = time.time() + 60
    while not result["done"] and time.time() < deadline:
        runloop.runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1)
        )
    return result["granted"]


def _ensure_reminders_access() -> None:
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EK_ENTITY_REMINDER)
    if status == _ST_FULL:
        return
    if status in (_ST_RESTRICTED, _ST_DENIED):
        raise RuntimeError(
            "Reminders access is denied. Grant it in "
            "System Settings > Privacy & Security > Reminders, enable the app that "
            "launched this server (Claude), then restart it."
        )
    if not _request_full_reminders_access():
        raise RuntimeError(
            "Reminders access was not granted. Approve the Reminders prompt, or "
            "enable it in System Settings > Privacy & Security > Reminders."
        )


def _fetch_reminders(predicate) -> list:  # noqa: ANN001
    """Reminder fetches are async — block on the completion handler."""
    result: dict[str, Any] = {"done": False, "items": []}

    def handler(reminders):  # noqa: ANN001
        result["items"] = list(reminders) if reminders else []
        result["done"] = True

    _store.fetchRemindersMatchingPredicate_completion_(predicate, handler)
    runloop = NSRunLoop.currentRunLoop()
    deadline = time.time() + 30
    while not result["done"] and time.time() < deadline:
        runloop.runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05)
        )
    return result["items"]


def _is_date_only(value: str) -> bool:
    v = value.strip()
    return "T" not in v and ":" not in v and len(v) <= 10


def _to_due_components(value: str):
    dt = _parse_dt(value)
    c = NSDateComponents.alloc().init()
    c.setYear_(dt.year)
    c.setMonth_(dt.month)
    c.setDay_(dt.day)
    if not _is_date_only(value):
        c.setHour_(dt.hour)
        c.setMinute_(dt.minute)
    return c


def _from_due_components(comps) -> Optional[str]:  # noqa: ANN001
    if comps is None:
        return None
    y, mo, d = comps.year(), comps.month(), comps.day()
    if _UNDEF in (y, mo, d) or y < 1:
        return None
    h, mi = comps.hour(), comps.minute()
    if h == _UNDEF or mi == _UNDEF:
        return "%04d-%02d-%02d" % (y, mo, d)
    return "%04d-%02d-%02dT%02d:%02d" % (y, mo, d, h, mi)


def _priority_to_int(p) -> Optional[int]:  # noqa: ANN001
    if p is None:
        return None
    if isinstance(p, int):
        return p
    key = str(p).lower()
    if key not in _PRIORITY_TO_INT:
        raise ValueError("priority must be one of none|low|medium|high")
    return _PRIORITY_TO_INT[key]


def _priority_from_int(n: int) -> str:
    if n == 0:
        return "none"
    if 1 <= n <= 4:
        return "high"
    if n == 5:
        return "medium"
    return "low"  # 6-9


def _resolve_reminder_list(list_id: Optional[str]):
    if not list_id:
        cal = _store.defaultCalendarForNewReminders()
        if cal is None:
            raise RuntimeError("No default reminder list available; pass list_id.")
        return cal
    for c in _store.calendarsForEntityType_(EK_ENTITY_REMINDER):
        if c.calendarIdentifier() == list_id or c.title() == list_id:
            return c
    raise ValueError(f"Reminder list not found: {list_id!r}")


def _get_reminder(reminder_id: str):
    item = _store.calendarItemWithIdentifier_(reminder_id)
    if item is None:
        raise ValueError(f"Reminder not found: {reminder_id!r}")
    return item


def _serialize_reminder(r) -> dict:  # noqa: ANN001
    url = r.URL()
    return {
        "reminder_id": r.calendarItemIdentifier(),
        "title": r.title(),
        "list": r.calendar().title() if r.calendar() else None,
        "list_id": r.calendar().calendarIdentifier() if r.calendar() else None,
        "due": _from_due_components(r.dueDateComponents()),
        "completed": bool(r.isCompleted()),
        "completion_date": _from_nsdate(r.completionDate()),
        "priority": _priority_from_int(r.priority()),
        "notes": r.notes(),
        "url": url.absoluteString() if url else None,
        "is_recurring": bool(r.hasRecurrenceRules()),
        "recurrence": _summarize_rules(r.recurrenceRules()),
    }


def _save_reminder(r) -> None:  # noqa: ANN001
    ok, err = _store.saveReminder_commit_error_(r, True, None)
    if not ok:
        raise RuntimeError(
            f"Reminder save failed: "
            f"{err.localizedDescription() if err else 'unknown error'}"
        )


def _remove_reminder(r) -> None:  # noqa: ANN001
    ok, err = _store.removeReminder_commit_error_(r, True, None)
    if not ok:
        raise RuntimeError(
            f"Reminder delete failed: "
            f"{err.localizedDescription() if err else 'unknown error'}"
        )


@mcp.tool()
def list_reminder_lists() -> list:
    """List all reminder lists (id, title, color, writable, default)."""
    _ensure_reminders_access()
    default = _store.defaultCalendarForNewReminders()
    default_id = default.calendarIdentifier() if default else None
    return [
        {
            "list_id": c.calendarIdentifier(),
            "title": c.title(),
            "color": _hex_from_calendar(c),
            "writable": bool(c.allowsContentModifications()),
            "is_default": c.calendarIdentifier() == default_id,
        }
        for c in _store.calendarsForEntityType_(EK_ENTITY_REMINDER)
    ]


@mcp.tool()
def list_reminders(
    list_id: Optional[str] = None,
    include_completed: bool = False,
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    query: Optional[str] = None,
) -> list:
    """List reminders, optionally filtered.

    Args:
        list_id: Restrict to one reminder list (id or title). Default: all lists.
        include_completed: Include completed reminders (default: incomplete only).
        due_before / due_after: ISO datetimes bounding the due date.
        query: Case-insensitive substring filter on title/notes.
    """
    _ensure_reminders_access()
    cals = [_resolve_reminder_list(list_id)] if list_id else None
    if include_completed:
        items = _fetch_reminders(_store.predicateForRemindersInCalendars_(cals))
    else:
        start = _to_nsdate(due_after) if due_after else None
        end = _to_nsdate(due_before) if due_before else None
        items = _fetch_reminders(
            _store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                start, end, cals
            )
        )
    out = [_serialize_reminder(r) for r in items]
    if include_completed and (due_before or due_after):
        lo = due_after or ""
        hi = due_before or "9999"
        out = [r for r in out if r["due"] and lo <= r["due"] <= hi]
    if query:
        q = query.lower()
        out = [
            r for r in out
            if q in " ".join(str(r.get(k) or "") for k in ("title", "notes")).lower()
        ]
    out.sort(key=lambda r: r["due"] or "9999")
    return out


@mcp.tool()
def get_reminder(reminder_id: str) -> dict:
    """Fetch full detail for a single reminder."""
    _ensure_reminders_access()
    return _serialize_reminder(_get_reminder(reminder_id))


@mcp.tool()
def create_reminder(
    title: str,
    list_id: Optional[str] = None,
    due: Optional[str] = None,
    notes: Optional[str] = None,
    priority: Optional[str] = None,
    url: Optional[str] = None,
    recurrence: Optional[dict] = None,
) -> dict:
    """Create a reminder and return it.

    Args:
        title: Reminder title.
        list_id: Target reminder list (id or title). Default: your default list.
        due: Optional ISO due date/datetime ('2026-08-10' = no time-of-day).
        notes: Optional notes.
        priority: none | low | medium | high.
        url: Optional URL to attach.
        recurrence: Optional recurrence spec (same shape as events);
            requires a due date to anchor the series.
    """
    _ensure_reminders_access()
    r = EventKit.EKReminder.reminderWithEventStore_(_store)
    r.setCalendar_(_resolve_reminder_list(list_id))
    r.setTitle_(title)
    if due is not None:
        r.setDueDateComponents_(_to_due_components(due))
    if notes is not None:
        r.setNotes_(notes or None)
    if priority is not None:
        r.setPriority_(_priority_to_int(priority))
    if url is not None:
        r.setURL_(NSURL.URLWithString_(url) if url else None)
    if recurrence:
        r.addRecurrenceRule_(_build_rule(recurrence))
    _save_reminder(r)
    return _serialize_reminder(r)


@mcp.tool()
def update_reminder(
    reminder_id: str,
    title: Optional[str] = None,
    due: Optional[str] = None,
    notes: Optional[str] = None,
    priority: Optional[str] = None,
    url: Optional[str] = None,
    list_id: Optional[str] = None,
    completed: Optional[bool] = None,
    clear_due: bool = False,
    recurrence: Optional[dict] = None,
    clear_recurrence: bool = False,
) -> dict:
    """Update a reminder. Only the fields you pass are changed.

    Set completed=True to mark it done (False to reopen). clear_due=True removes
    the due date. Pass empty strings ('') to clear notes/url. Change how it
    repeats with a new `recurrence` spec, or clear_recurrence=True for a one-off.
    """
    _ensure_reminders_access()
    r = _get_reminder(reminder_id)
    if title is not None:
        r.setTitle_(title)
    if clear_due:
        r.setDueDateComponents_(None)
    elif due is not None:
        r.setDueDateComponents_(_to_due_components(due))
    if notes is not None:
        r.setNotes_(notes or None)
    if priority is not None:
        r.setPriority_(_priority_to_int(priority))
    if url is not None:
        r.setURL_(NSURL.URLWithString_(url) if url else None)
    if list_id is not None:
        r.setCalendar_(_resolve_reminder_list(list_id))
    if completed is not None:
        r.setCompleted_(bool(completed))
    if clear_recurrence:
        r.setRecurrenceRules_(None)
    elif recurrence is not None:
        r.setRecurrenceRules_([_build_rule(recurrence)])
    _save_reminder(r)
    return _serialize_reminder(r)


@mcp.tool()
def delete_reminder(reminder_id: str) -> dict:
    """Permanently delete a reminder."""
    _ensure_reminders_access()
    _remove_reminder(_get_reminder(reminder_id))
    return {"deleted": True, "reminder_id": reminder_id}


if __name__ == "__main__":
    mcp.run()
