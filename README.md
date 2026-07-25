# apple-calendar-mcp

A local, self-contained MCP server giving Claude Desktop full CRUD over the
macOS Calendar via **EventKit** — including real recurring-event operations.

No cloud, no external services. Your calendar data never leaves the Mac.

## Tools

| Tool | What it does |
|------|--------------|
| `list_calendars` | All calendars: id, title, source, color, writable, default |
| `list_events` | Events in a date range, recurring events expanded to occurrences |
| `get_event` | Full detail for one event / occurrence |
| `create_event` | New event, optionally recurring |
| `update_event` | Change any field; retarget/reshape recurrence |
| `delete_event` | Delete one occurrence, "this and future", or the whole series |
| `detach_occurrence` | Make a standalone, non-recurring copy off a recurring series |
| `create_calendar` | New calendar in a chosen source, optional color |
| `set_calendar_color` | Recolor a calendar (hex, e.g. `#FF3B30`) |
| `delete_calendar` | Permanently delete a calendar + its events (guarded, id-only) |

> **Note on colors:** Apple Calendar has no per-*event* color — events inherit
> their calendar's color. So coloring happens at the calendar level; there's no
> event-color tool because EventKit exposes no such property.

See [DEMO.md](DEMO.md) for a copy-paste prompt that exercises every tool on a
self-cleaning scratch calendar.

### Recurring events

`list_events` returns each occurrence with its `event_id` **and** its own
`start`. To act on a specific occurrence, pass that `start` back as
`occurrence_start`, plus a `span`:

- `this` — only that occurrence (detaches it from the series)
- `future` — that occurrence and every later one
- `all` — the entire series

Change the recurrence rule by passing a new `recurrence` spec to
`update_event`; turn a series into a one-off with `clear_recurrence=True`.

Recurrence spec shape:

```json
{
  "frequency": "weekly",           // daily | weekly | monthly | yearly
  "interval": 2,                    // every N periods
  "days_of_week": ["MO","WE","FR"], // weekly only
  "days_of_month": [1, -1],         // monthly only (-1 = last day)
  "count": 10,                      // total occurrences  (or)
  "end_date": "2026-12-31"          // stop after this date
}
```

## Install / rebuild

```bash
cd /Users/maxwinslow/Code/apple-calendar-mcp
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Requires Python 3.10+ (uses 3.14 here — the system 3.9 is too old for the MCP SDK).

## Claude Desktop wiring

Already registered in `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"apple-calendar": {
  "command": "/Users/maxwinslow/Code/apple-calendar-mcp/.venv/bin/python",
  "args": ["/Users/maxwinslow/Code/apple-calendar-mcp/server.py"]
}
```

## Permissions (the one gotcha)

On first use, macOS shows a **"Claude wants to access Calendar"** prompt —
approve it. The grant is tied to Claude.app (the app launching the server).

If it was denied, re-enable it under
**System Settings → Privacy & Security → Calendars → Claude**, then fully quit
and reopen Claude Desktop.
