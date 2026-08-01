#!/usr/bin/env python3
"""
Apple Calendar MCP server (CalDAV backend).

Full CRUD over an iCloud calendar account via CalDAV, exposing the same MCP tool
surface as the original EventKit version (identical tool names + parameters).

Two transports, selected by MCP_TRANSPORT:
  * http  (default) - long-running HTTP/streamable service, authenticated by
                      Tailscale device identity (see TailscaleAuthASGI).
  * stdio           - classic per-session stdio server for local MCP clients.

The CalDAV backend is used in BOTH modes.

Environment:
  ICLOUD_USERNAME       Apple ID email.
  ICLOUD_APP_PASSWORD   iCloud app-specific password (appleid.apple.com).
  MCP_TRANSPORT         'http' (default) or 'stdio'.
  MCP_HOST              HTTP bind host (default 0.0.0.0).
  MCP_PORT              HTTP port (default 8420).
  CALENDAR_TZ           IANA tz for naive datetimes (default: system /etc/localtime).
  DEFAULT_CALENDAR      Name or URL of the calendar used when none is specified.
  TS_ALLOWED            Comma-separated allowlist of Tailscale hostnames/logins.
  TS_ALLOWLIST_FILE     File with one allowed identity per line (alt to TS_ALLOWED).
  TAILSCALE_SOCKET      tailscaled LocalAPI socket (default /var/run/tailscale/tailscaled.sock).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar import vRecur
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("apple-calendar-mcp")

mcp = FastMCP("apple-calendar")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "http").lower()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8420"))
TAILSCALE_SOCKET = os.environ.get(
    "TAILSCALE_SOCKET", "/var/run/tailscale/tailscaled.sock"
)


def _local_tz() -> ZoneInfo:
    name = os.environ.get("CALENDAR_TZ")
    if name:
        return ZoneInfo(name)
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return ZoneInfo(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    return ZoneInfo("UTC")


_LOCAL_TZ = _local_tz()


# ---------------------------------------------------------------------------
# CalDAV connection (lazy so pure helpers import without caldav/creds)
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {"client": None, "principal": None, "calendars": None}


def _principal():
    if _state["principal"] is None:
        user = os.environ.get("ICLOUD_USERNAME")
        pw = os.environ.get("ICLOUD_APP_PASSWORD")
        if not user or not pw:
            raise RuntimeError(
                "Set ICLOUD_USERNAME and ICLOUD_APP_PASSWORD (an iCloud "
                "app-specific password from appleid.apple.com)."
            )
        import caldav

        client = caldav.DAVClient(
            url="https://caldav.icloud.com", username=user, password=pw
        )
        _state["client"] = client
        _state["principal"] = client.principal()
    return _state["principal"]


def _calendars(refresh: bool = False) -> list:
    if _state["calendars"] is None or refresh:
        _state["calendars"] = list(_principal().calendars())
    return _state["calendars"]


def _cal_name(cal) -> Optional[str]:  # noqa: ANN001
    try:
        n = cal.get_display_name()
        if n:
            return str(n)
    except Exception:
        pass
    return str(cal.name) if getattr(cal, "name", None) else None


def _norm_color(value) -> Optional[str]:  # noqa: ANN001
    if not value:
        return None
    s = str(value).strip()
    if s.startswith("#") and len(s) == 9:  # #RRGGBBAA -> drop alpha
        s = s[:7]
    return s.upper() if s.startswith("#") else s


def _cal_color(cal) -> Optional[str]:  # noqa: ANN001
    try:
        from caldav.elements import ical

        return _norm_color(cal.get_property(ical.CalendarColor()))
    except Exception:
        return None


def _set_cal_color(cal, hex_value: str) -> None:  # noqa: ANN001
    if not (hex_value.startswith("#") and len(hex_value) in (7, 9)):
        raise ValueError(f"color must be hex like '#FF3B30', got {hex_value!r}")
    from caldav.elements import ical

    cal.set_properties([ical.CalendarColor(hex_value)])


def _default_cal_url() -> Optional[str]:
    hint = os.environ.get("DEFAULT_CALENDAR")
    cals = _calendars()
    if hint:
        for c in cals:
            if str(c.url).rstrip("/") == hint.rstrip("/") or _cal_name(c) == hint:
                return str(c.url)
    for c in cals:
        if (_cal_name(c) or "").lower() in ("home", "calendar"):
            return str(c.url)
    return str(cals[0].url) if cals else None


def _serialize_calendar(cal) -> dict:  # noqa: ANN001
    return {
        "calendar_id": str(cal.url),
        "title": _cal_name(cal),
        "source": os.environ.get("ICLOUD_USERNAME") or "iCloud",
        "color": _cal_color(cal),
        "writable": True,
        "is_default": str(cal.url) == _default_cal_url(),
    }


def _resolve_calendar(calendar_id: Optional[str]):
    if not calendar_id:
        url = _default_cal_url()
        if not url:
            raise RuntimeError("No calendars available; cannot pick a default.")
        return _calendar_by_url(url)
    return _calendar_by_url(calendar_id)


def _calendar_by_url(cid: str):
    for cal in _calendars():
        if str(cal.url).rstrip("/") == cid.rstrip("/") or _cal_name(cal) == cid:
            return cal
    _calendars(refresh=True)
    for cal in _calendars():
        if str(cal.url).rstrip("/") == cid.rstrip("/") or _cal_name(cal) == cid:
            return cal
    raise ValueError(f"Calendar not found: {cid!r}")


def _calendar_by_id_strict(cid: str):
    """URL-only match for destructive/color ops (no title fuzzy-match)."""
    for cal in _calendars(refresh=True):
        if str(cal.url).rstrip("/") == cid.rstrip("/"):
            return cal
    raise ValueError(f"No calendar with id {cid!r}")


# ---------------------------------------------------------------------------
# Date + recurrence translation
# ---------------------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    s = value.strip().replace("Z", "")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    if "T" not in s:
        s = s + "T00:00:00"
    return datetime.fromisoformat(s)


def _localize(dt: datetime) -> datetime:
    return dt.replace(tzinfo=_LOCAL_TZ) if dt.tzinfo is None else dt


def _is_date_only(value: str) -> bool:
    v = value.strip()
    return "T" not in v and " " not in v and len(v) <= 10


def _fmt(dt) -> Optional[str]:  # noqa: ANN001
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return None


_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


def _spec_to_vrecur(spec: dict) -> vRecur:
    freq = str(spec["frequency"]).upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise ValueError("frequency must be daily|weekly|monthly|yearly")
    d: dict[str, Any] = {"FREQ": [freq]}
    if spec.get("interval"):
        d["INTERVAL"] = [int(spec["interval"])]
    if spec.get("days_of_week"):
        days = []
        for x in spec["days_of_week"]:
            code = str(x).upper()[:2]
            if code not in _WEEKDAYS:
                raise ValueError(f"bad weekday: {x!r}")
            days.append(code)
        d["BYDAY"] = days
    if spec.get("days_of_month"):
        d["BYMONTHDAY"] = [int(x) for x in spec["days_of_month"]]
    if spec.get("count"):
        d["COUNT"] = [int(spec["count"])]
    elif spec.get("end_date"):
        until = _localize(_parse_dt(spec["end_date"])).astimezone(timezone.utc)
        d["UNTIL"] = [until]
    return vRecur(d)


def _vrecur_to_spec(rr) -> Optional[dict]:  # noqa: ANN001
    if not rr:
        return None

    def first(key):
        v = rr.get(key)
        if isinstance(v, list):
            return v[0] if v else None
        return v

    freq = first("FREQ")
    spec: dict[str, Any] = {
        "frequency": str(freq).lower() if freq else None,
        "interval": int(first("INTERVAL") or 1),
    }
    if rr.get("BYDAY"):
        spec["days_of_week"] = [str(x) for x in rr.get("BYDAY")]
    if rr.get("BYMONTHDAY"):
        spec["days_of_month"] = [int(x) for x in rr.get("BYMONTHDAY")]
    if rr.get("COUNT"):
        spec["count"] = int(first("COUNT"))
    if rr.get("UNTIL"):
        u = first("UNTIL")
        spec["end_date"] = u.isoformat() if hasattr(u, "isoformat") else str(u)
    return spec


# ---------------------------------------------------------------------------
# iCalendar component build + field application
# ---------------------------------------------------------------------------

def _new_vcal() -> ICalendar:
    cal = ICalendar()
    cal.add("prodid", "-//apple-calendar-mcp//EN")
    cal.add("version", "2.0")
    return cal


def _mk_dtstart(value: str, all_day: bool):
    if all_day:
        return _parse_dt(value).date()
    return _localize(_parse_dt(value))


def _build_vevent(
    title, start, end, all_day, location, notes, url, recurrence, uid=None
):  # noqa: ANN001
    ev = IEvent()
    ev.add("uid", uid or str(uuid.uuid4()))
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("summary", title)
    dtstart = _mk_dtstart(start, all_day)
    if end:
        dtend = _mk_dtstart(end, all_day)
    elif all_day:
        dtend = dtstart + timedelta(days=1)
    else:
        dtend = dtstart + timedelta(hours=1)
    if all_day and dtend <= dtstart:
        dtend = dtstart + timedelta(days=1)
    ev.add("dtstart", dtstart)
    ev.add("dtend", dtend)
    if location:
        ev.add("location", location)
    if notes:
        ev.add("description", notes)
    if url:
        ev.add("url", url)
    if recurrence:
        ev.add("rrule", _spec_to_vrecur(recurrence))
    return ev


def _set_or_del(comp, key: str, value) -> None:  # noqa: ANN001
    comp.pop(key, None)
    if value:
        comp.add(key, value)


def _apply_fields(comp, title, start, end, all_day, location, notes, url):  # noqa: ANN001
    if title is not None:
        _set_or_del(comp, "summary", title)
    cur_all = "dtstart" in comp and not isinstance(comp["dtstart"].dt, datetime)
    is_all = cur_all if all_day is None else bool(all_day)
    if start is not None:
        comp.pop("dtstart", None)
        comp.add("dtstart", _mk_dtstart(start, is_all))
    if end is not None:
        comp.pop("dtend", None)
        comp.add("dtend", _mk_dtstart(end, is_all))
    if location is not None:
        _set_or_del(comp, "location", location)
    if notes is not None:
        _set_or_del(comp, "description", notes)
    if url is not None:
        _set_or_del(comp, "url", url)


def _serialize_comp(comp, cal_url=None, cal_name=None) -> dict:  # noqa: ANN001
    dtstart = comp.get("dtstart")
    dtend = comp.get("dtend")
    start = dtstart.dt if dtstart is not None else None
    end = dtend.dt if dtend is not None else None
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    rrule = comp.get("rrule")
    rec = _vrecur_to_spec(rrule) if rrule else None
    return {
        "event_id": str(comp.get("uid")) if comp.get("uid") else None,
        "title": str(comp.get("summary")) if comp.get("summary") else None,
        "calendar": cal_name,
        "calendar_id": cal_url,
        "start": _fmt(start),
        "end": _fmt(end),
        "all_day": bool(all_day),
        "location": str(comp.get("location")) if comp.get("location") else None,
        "notes": str(comp.get("description")) if comp.get("description") else None,
        "url": str(comp.get("url")) if comp.get("url") else None,
        "is_recurring": bool(rrule) or comp.get("recurrence-id") is not None,
        "is_detached": comp.get("recurrence-id") is not None,
        "recurrence": [rec] if rec else None,
    }


# ---------------------------------------------------------------------------
# Recurring-event plumbing (find resource, occurrences, spans)
# ---------------------------------------------------------------------------

def _find_resource(uid: str):
    """Return (caldav_calendar, caldav_event_resource) for a UID, searching all
    calendars. The resource holds the master VEVENT (+ any override VEVENTs)."""
    import caldav

    for cal in _calendars():
        try:
            res = cal.event_by_uid(uid)
            if res is not None:
                return cal, res
        except caldav.error.NotFoundError:
            continue
        except Exception:
            continue
    raise ValueError(f"Event not found: {uid!r}")


def _master_vevent(vcal):  # noqa: ANN001
    vevents = [c for c in vcal.subcomponents if c.name == "VEVENT"]
    for c in vevents:
        if c.get("recurrence-id") is None:
            return c
    return vevents[0] if vevents else None


def _occurrence_dt(master, occurrence_start: str):  # noqa: ANN001
    dts = master.get("dtstart").dt
    target = _parse_dt(occurrence_start)
    if isinstance(dts, datetime):
        tz = dts.tzinfo or _LOCAL_TZ
        return target.replace(tzinfo=tz) if target.tzinfo is None else target.astimezone(tz)
    return target.date()


def _duration(master):  # noqa: ANN001
    dts = master.get("dtstart")
    dte = master.get("dtend")
    if dts is not None and dte is not None:
        return dte.dt - dts.dt
    return timedelta(hours=1)


def _add_exdate(master, occ_dt) -> None:  # noqa: ANN001
    master.add("exdate", occ_dt)


def _truncate_until(master, occ_dt) -> None:  # noqa: ANN001
    rr = master.get("rrule")
    if rr is None:
        return
    rr.pop("COUNT", None)
    if isinstance(occ_dt, datetime):
        until = occ_dt.astimezone(timezone.utc) - timedelta(seconds=1)
    else:
        until = occ_dt - timedelta(days=1)
    rr["UNTIL"] = [until]


def _find_override(vcal, occ_dt):  # noqa: ANN001
    for c in vcal.subcomponents:
        if c.name != "VEVENT":
            continue
        rid = c.get("recurrence-id")
        if rid is not None and rid.dt == occ_dt:
            return c
    return None


def _make_override(master, occ_dt):  # noqa: ANN001
    ov = IEvent()
    for key in ("uid", "summary", "location", "description", "url"):
        if master.get(key) is not None:
            ov.add(key, master.get(key))
    ov.add("dtstamp", datetime.now(timezone.utc))
    ov.add("recurrence-id", occ_dt)
    ov.add("dtstart", occ_dt)
    ov.add("dtend", occ_dt + _duration(master))
    return ov


def _save_resource(res, vcal) -> None:  # noqa: ANN001
    res.data = vcal.to_ical().decode("utf-8")
    res.save()


# ---------------------------------------------------------------------------
# Tools  (identical names + parameters to the EventKit version)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_calendars() -> list:
    """List all calendars, with their id, title, source, color (hex), whether
    they're writable, and which is the default.

    Note: subscribed / webcal (read-only) calendars are NOT returned — iCloud
    does not expose those over CalDAV.
    """
    return [_serialize_calendar(c) for c in _calendars(refresh=True)]


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
    cals = [_resolve_calendar(calendar_id)] if calendar_id else _calendars()
    s = _localize(_parse_dt(start))
    e = _localize(_parse_dt(end))
    out: list[dict] = []
    for cal in cals:
        cal_url, cal_name = str(cal.url), _cal_name(cal)
        try:
            results = cal.search(start=s, end=e, event=True, expand=True)
        except Exception:
            results = cal.date_search(start=s, end=e)
        for r in results:
            for comp in r.icalendar_instance.subcomponents:
                if comp.name == "VEVENT":
                    out.append(_serialize_comp(comp, cal_url, cal_name))
    # best-effort recurrence flag: a UID appearing >1x in the window is a series
    counts: dict[str, int] = {}
    for ev in out:
        counts[ev["event_id"]] = counts.get(ev["event_id"], 0) + 1
    for ev in out:
        if counts.get(ev["event_id"], 0) > 1:
            ev["is_recurring"] = True
    out.sort(key=lambda ev: ev["start"] or "")
    if query:
        q = query.lower()
        out = [
            ev for ev in out
            if q in " ".join(
                str(ev.get(k) or "") for k in ("title", "location", "notes")
            ).lower()
        ]
    return out


@mcp.tool()
def get_event(event_id: str, occurrence_start: Optional[str] = None) -> dict:
    """Fetch full detail for a single event (or a specific occurrence if
    occurrence_start is given)."""
    cal, res = _find_resource(event_id)
    vcal = res.icalendar_instance
    master = _master_vevent(vcal)
    if occurrence_start:
        occ_dt = _occurrence_dt(master, occurrence_start)
        comp = _find_override(vcal, occ_dt)
        if comp is None:
            comp = _make_override(master, occ_dt)  # synthesized view of the occurrence
    else:
        comp = master
    return _serialize_comp(comp, str(cal.url), _cal_name(cal))


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
    cal = _resolve_calendar(calendar_id)
    if all_day is False and _is_date_only(start) and _is_date_only(end):
        all_day = True
    ev = _build_vevent(title, start, end, all_day, location, notes, url, recurrence)
    vcal = _new_vcal()
    vcal.add_component(ev)
    cal.save_event(vcal.to_ical())
    return _serialize_comp(ev, str(cal.url), _cal_name(cal))


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
        'this'   -> only this occurrence (detaches it into an override)
        'future' -> this occurrence and all later ones (splits the series)
        'all'    -> the entire series

    To change how an event recurs, pass a new `recurrence` spec (see create_event).
    To turn a recurring event into a one-off, pass clear_recurrence=True.
    Pass empty strings ('') to clear location/notes/url.
    """
    cal, res = _find_resource(event_id)
    vcal = res.icalendar_instance
    master = _master_vevent(vcal)
    is_rec = master.get("rrule") is not None
    span = (span or "this").lower()

    # Whole-event edits (non-recurring, or explicit 'all', or no occurrence given)
    if not is_rec or span == "all" or occurrence_start is None:
        _apply_fields(master, title, start, end, all_day, location, notes, url)
        if clear_recurrence:
            master.pop("rrule", None)
            master.pop("exdate", None)
        elif recurrence is not None:
            master.pop("rrule", None)
            master.add("rrule", _spec_to_vrecur(recurrence))
        if calendar_id is not None:
            return _move_event(res, vcal, master, calendar_id)
        _save_resource(res, vcal)
        return _serialize_comp(master, str(cal.url), _cal_name(cal))

    occ_dt = _occurrence_dt(master, occurrence_start)

    if span == "this":
        override = _find_override(vcal, occ_dt) or _make_override(master, occ_dt)
        _apply_fields(override, title, start, end, all_day, location, notes, url)
        if _find_override(vcal, occ_dt) is None:
            vcal.add_component(override)
        _save_resource(res, vcal)
        return _serialize_comp(override, str(cal.url), _cal_name(cal))

    # span == 'future': truncate original series, start a new one at occ_dt
    _truncate_until(master, occ_dt)
    _save_resource(res, vcal)

    new_ev = IEvent()
    new_ev.add("uid", str(uuid.uuid4()))
    new_ev.add("dtstamp", datetime.now(timezone.utc))
    for key in ("summary", "location", "description", "url"):
        if master.get(key) is not None:
            new_ev.add(key, master.get(key))
    new_ev.add("dtstart", occ_dt)
    new_ev.add("dtend", occ_dt + _duration(master))
    if master.get("rrule") is not None and recurrence is None:
        rr = vRecur(dict(master.get("rrule")))
        rr.pop("UNTIL", None)
        new_ev.add("rrule", rr)
    elif recurrence is not None and not clear_recurrence:
        new_ev.add("rrule", _spec_to_vrecur(recurrence))
    _apply_fields(new_ev, title, start, end, all_day, location, notes, url)
    tgt_cal = _resolve_calendar(calendar_id) if calendar_id else cal
    nv = _new_vcal()
    nv.add_component(new_ev)
    tgt_cal.save_event(nv.to_ical())
    return _serialize_comp(new_ev, str(tgt_cal.url), _cal_name(tgt_cal))


def _move_event(res, vcal, master, calendar_id):  # noqa: ANN001
    """Move a whole event to another calendar (CalDAV: copy to target + delete)."""
    tgt = _resolve_calendar(calendar_id)
    tgt.save_event(vcal.to_ical())
    res.delete()
    return _serialize_comp(master, str(tgt.url), _cal_name(tgt))


@mcp.tool()
def delete_event(
    event_id: str,
    occurrence_start: Optional[str] = None,
    span: str = "this",
) -> dict:
    """Delete an event or occurrence.

    For a non-recurring event, just pass event_id.
    For a recurring event, pass occurrence_start and a span:
        'this'   -> delete only that occurrence (adds an EXDATE)
        'future' -> delete that occurrence and all later ones (UNTIL split)
        'all'    -> delete the entire series
    """
    cal, res = _find_resource(event_id)
    vcal = res.icalendar_instance
    master = _master_vevent(vcal)
    is_rec = master.get("rrule") is not None
    span = (span or "this").lower()

    if not is_rec or span == "all" or occurrence_start is None:
        res.delete()
        return {"deleted": True, "event_id": event_id, "span": span}

    occ_dt = _occurrence_dt(master, occurrence_start)
    if span == "future":
        _truncate_until(master, occ_dt)
    else:  # this
        _add_exdate(master, occ_dt)
        ov = _find_override(vcal, occ_dt)
        if ov is not None:
            vcal.subcomponents.remove(ov)
    _save_resource(res, vcal)
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
    recurring event. The new event is fully independent of the series (new UID,
    no recurrence rule).

    Optionally override any field on the copy, and optionally remove that
    occurrence from the original series (delete_from_series=True) so it isn't
    duplicated.
    """
    cal, res = _find_resource(event_id)
    vcal = res.icalendar_instance
    master = _master_vevent(vcal)
    occ_dt = _occurrence_dt(master, occurrence_start)
    dur = _duration(master)

    copy = IEvent()
    copy.add("uid", str(uuid.uuid4()))
    copy.add("dtstamp", datetime.now(timezone.utc))
    copy.add("summary", title if title is not None else (master.get("summary") or ""))
    copy.add("dtstart", _mk_dtstart(start, isinstance(occ_dt, date) and not isinstance(occ_dt, datetime)) if start else occ_dt)
    if end:
        copy.add("dtend", _mk_dtstart(end, isinstance(occ_dt, date) and not isinstance(occ_dt, datetime)))
    else:
        copy.add("dtend", occ_dt + dur)
    loc = location if location is not None else master.get("location")
    if loc:
        copy.add("location", loc)
    desc = notes if notes is not None else master.get("description")
    if desc:
        copy.add("description", desc)
    if master.get("url"):
        copy.add("url", master.get("url"))

    tgt = _resolve_calendar(calendar_id) if calendar_id else cal
    nv = _new_vcal()
    nv.add_component(copy)
    tgt.save_event(nv.to_ical())

    if delete_from_series:
        _add_exdate(master, occ_dt)
        _save_resource(res, vcal)

    return {
        "created": _serialize_comp(copy, str(tgt.url), _cal_name(tgt)),
        "removed_from_series": bool(delete_from_series),
    }


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
        source: Ignored for iCloud CalDAV (single account). Accepted for
            interface compatibility.
    """
    p = _principal()
    cal = p.make_calendar(name=title)
    if color:
        _set_cal_color(cal, color)
    _calendars(refresh=True)
    return _serialize_calendar(cal)


@mcp.tool()
def set_calendar_color(calendar_id: str, color: str) -> dict:
    """Set a calendar's color. `color` is a hex string like '#34C759'.
    Requires the calendar's id (from list_calendars), not its title."""
    cal = _calendar_by_id_strict(calendar_id)
    _set_cal_color(cal, color)
    return _serialize_calendar(cal)


@mcp.tool()
def delete_calendar(calendar_id: str) -> dict:
    """Permanently delete a calendar AND all events in it. Irreversible.

    Requires the calendar's exact id (from list_calendars), never a title, so it
    can't fire on a fuzzy match.
    """
    cal = _calendar_by_id_strict(calendar_id)
    title = _cal_name(cal)
    cal.delete()
    _calendars(refresh=True)
    return {"deleted": True, "calendar_id": calendar_id, "title": title}


# ---------------------------------------------------------------------------
# Tailscale identity auth (HTTP mode)
# ---------------------------------------------------------------------------

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._socket_path)
        self.sock = s


def _tailscale_whois(remote_addr: str) -> dict:
    """Query tailscaled LocalAPI WhoIs for 'ip:port'. Raises on any failure."""
    conn = _UnixHTTPConnection(TAILSCALE_SOCKET)
    try:
        conn.request(
            "GET",
            f"/localapi/v0/whois?addr={remote_addr}",
            headers={"Host": "local-tailscaled.sock"},
        )
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"whois HTTP {resp.status}: {body[:200]!r}")
        return json.loads(body)
    finally:
        conn.close()


_ALLOWLIST_CACHE: dict[str, Any] = {"value": "unset"}


def _allowlist() -> Optional[set]:
    if _ALLOWLIST_CACHE["value"] != "unset":
        return _ALLOWLIST_CACHE["value"]
    items: set[str] = set()
    env = os.environ.get("TS_ALLOWED", "").strip()
    if env:
        items |= {x.strip() for x in env.split(",") if x.strip()}
    path = os.environ.get("TS_ALLOWLIST_FILE", "").strip()
    if path and os.path.exists(path):
        with open(path) as f:
            items |= {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    value = items or None
    _ALLOWLIST_CACHE["value"] = value
    return value


def _authorize(ip: str, port: int) -> tuple[bool, str]:
    """Resolve the peer to a Tailscale identity and check the allowlist.
    Fails CLOSED: any WhoIs error => rejected."""
    try:
        who = _tailscale_whois(f"{ip}:{port}")
    except Exception as ex:
        log.error("Tailscale LocalAPI whois failed (fail-closed): %s", ex)
        return False, f"whois-error:{type(ex).__name__}"
    node = who.get("Node") or {}
    user = who.get("UserProfile") or {}
    name = (node.get("ComputedName") or node.get("Name") or "").rstrip(".")
    login = user.get("LoginName") or ""
    identity = name or login or "unknown"
    allowed = _allowlist()
    if allowed is None:
        return True, identity  # open mode (warned at startup)
    for a in allowed:
        if a in (name, login, identity) or (name and name.startswith(a + ".")):
            return True, identity
    return False, identity


class TailscaleAuthASGI:
    """Pure-ASGI middleware (no response buffering, so SSE streaming works)."""

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        client = scope.get("client")
        if not client:
            return await self._deny(send, "no client address")
        ip, port = client[0], client[1]
        ok, identity = _authorize(ip, port)
        if not ok:
            log.warning("REJECT %s:%s -> %s", ip, port, identity)
            return await self._deny(send, "forbidden")
        log.info("ALLOW %s:%s as '%s' (%s)", ip, port, identity, scope.get("path"))
        scope = dict(scope)
        scope["ts_identity"] = identity
        return await self.app(scope, receive, send)

    async def _deny(self, send, msg: str):  # noqa: ANN001
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": json.dumps({"error": msg}).encode(),
        })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _startup_checks() -> None:
    if not os.environ.get("ICLOUD_USERNAME") or not os.environ.get("ICLOUD_APP_PASSWORD"):
        log.warning("ICLOUD_USERNAME / ICLOUD_APP_PASSWORD not set — tools will error.")
    if not os.path.exists(TAILSCALE_SOCKET):
        log.warning(
            "Tailscale socket %s not found — ALL requests will be rejected "
            "(fail-closed). Is tailscaled running?", TAILSCALE_SOCKET,
        )
    if _allowlist() is None:
        log.warning(
            "No TS_ALLOWED / TS_ALLOWLIST_FILE set — allowing ANY device that "
            "resolves on this tailnet. Set an allowlist to restrict."
        )
    else:
        log.info("Allowlist: %s", sorted(_allowlist()))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if MCP_TRANSPORT == "stdio":
        log.info("Starting apple-calendar MCP over stdio (CalDAV backend).")
        mcp.run()
        return

    import uvicorn

    _startup_checks()
    app = TailscaleAuthASGI(mcp.streamable_http_app())
    log.info(
        "Serving apple-calendar MCP over HTTP at http://%s:%s/mcp",
        MCP_HOST, MCP_PORT,
    )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
