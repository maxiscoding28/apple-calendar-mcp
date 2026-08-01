# apple-calendar-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives Claude full
CRUD access to the **macOS Calendar** via Apple's **EventKit** framework —
including real recurring-event operations.

Everything runs on your Mac. No cloud, no accounts, no external services — your
calendar data never leaves the machine.

## Requirements

- **macOS** (uses EventKit / the native Calendar database)
- **Python 3.10+** (the MCP SDK requires it)
- An MCP client — these instructions cover **Claude Desktop**

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

### Working with recurring events

`list_events` returns each occurrence with its `event_id` **and** its own
`start`. To act on a specific occurrence, pass that `start` back as
`occurrence_start`, plus a `span`:

- `this` — only that occurrence (detaches it from the series)
- `future` — that occurrence and every later one
- `all` — the entire series

Change how an event recurs by passing a new `recurrence` spec to `update_event`;
turn a series into a one-off with `clear_recurrence=True`.

Recurrence spec shape:

```json
{
  "frequency": "weekly",            // daily | weekly | monthly | yearly
  "interval": 2,                    // every N periods
  "days_of_week": ["MO","WE","FR"], // weekly only
  "days_of_month": [1, -1],         // monthly only (-1 = last day)
  "count": 10,                      // total occurrences  (or)
  "end_date": "2026-12-31"          // stop after this date
}
```

## Install

```bash
git clone https://github.com/maxiscoding28/apple-calendar-mcp.git
cd apple-calendar-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Use any Python 3.10+ interpreter to create the venv. If your default `python3`
is older, substitute a newer one (e.g. `python3.12 -m venv .venv`).

## Connect to Claude Desktop

Add the server to your `claude_desktop_config.json`. You can open it from
Claude Desktop via **Settings → Developer → Edit Config**, or find it at:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add an `apple-calendar` entry under `mcpServers`, using **absolute paths** to the
Python interpreter in your venv and to `server.py`:

```json
{
  "mcpServers": {
    "apple-calendar": {
      "command": "/absolute/path/to/apple-calendar-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/apple-calendar-mcp/server.py"]
    }
  }
}
```

Replace `/absolute/path/to/apple-calendar-mcp` with wherever you cloned the repo
(run `pwd` in the project directory to get it). Then **fully quit and reopen
Claude Desktop** so it picks up the change.

## Permissions (the one gotcha)

The first time a tool runs, macOS shows a **"Claude wants to access Calendar"**
prompt — approve it. The grant is tied to the app that launches the server
(Claude Desktop), not to Python.

If it was denied, re-enable it under
**System Settings → Privacy & Security → Calendars**, enable your MCP client,
then fully quit and reopen it.

## Try it

Ask Claude things like:

- "What's on my calendar this week?"
- "Move my 3pm Friday meeting to 4pm and add a location."
- "Create a standup every weekday at 9am for the next two weeks."
- "Delete just next Tuesday's occurrence of that standup."

See [DEMO.md](DEMO.md) for a copy-paste prompt that exercises every tool on a
self-cleaning scratch calendar — safe to run against a real account.

## How it works

`server.py` is a single-file [FastMCP](https://modelcontextprotocol.io) server
that talks to EventKit through [PyObjC](https://pyobjc.readthedocs.io). It speaks
MCP over stdio, so any stdio-capable MCP client can use it — Claude Desktop is
just the documented example.

## License

MIT — see [LICENSE](LICENSE).
