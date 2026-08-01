# apple-calendar-mcp

A self-hosted [MCP](https://modelcontextprotocol.io) server giving Claude full
CRUD access to an **iCloud calendar** over **CalDAV** — including real
recurring-event operations.

Runs as a long-lived HTTP service, authenticated by **Tailscale device
identity** (no tokens, no passwords in the client), so you can reach your
calendar from any device on your tailnet — phone, laptop, desktop. A local
**stdio** mode is also included for running it right next to an MCP client.

> Previously this was a macOS-only, EventKit/PyObjC stdio server. It's now a
> CalDAV backend behind the exact same MCP tools, so it runs on any OS
> (a Linux home server is the intended target) and works remotely.

## Requirements

- **Python 3.10+**
- An **iCloud app-specific password** (create one at
  [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security →
  App-Specific Passwords). Your normal Apple ID password will not work.
- For HTTP mode: **Tailscale** installed and `tailscaled` running on the host
  (auth resolves the caller's identity via the local `tailscaled` socket).

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
| `create_calendar` | New calendar, optional color |
| `set_calendar_color` | Recolor a calendar (hex, e.g. `#FF3B30`) |
| `delete_calendar` | Permanently delete a calendar + its events (guarded, id-only) |

> **Colors:** Apple has no per-*event* color — events inherit their calendar's
> color — so coloring is a calendar-level operation.

### Known limitation: subscribed / webcal calendars

**Subscribed calendars (webcal feeds, holiday calendars, shared read-only
feeds) will not appear** in `list_calendars` and can't be read or written.
iCloud does not expose those over CalDAV — only calendars that live in your
iCloud account do. This is a protocol limitation, not a bug.

### Working with recurring events

`list_events` returns each occurrence with its `event_id` **and** its own
`start`. To act on a specific occurrence, pass that `start` back as
`occurrence_start`, plus a `span`:

- `this` — only that occurrence (added as an EXDATE / detached override)
- `future` — that occurrence and every later one (the series is split with UNTIL)
- `all` — the entire series

Change how an event recurs by passing a new `recurrence` spec to `update_event`;
turn a series into a one-off with `clear_recurrence=True`.

Recurrence spec shape (unchanged from the EventKit version — translated to
iCalendar `RRULE` under the hood):

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

Use any Python 3.10+ interpreter for the venv (e.g. `python3.12 -m venv .venv`).

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ICLOUD_USERNAME` | — | Apple ID email (required) |
| `ICLOUD_APP_PASSWORD` | — | iCloud app-specific password (required) |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind host |
| `MCP_PORT` | `8420` | HTTP port |
| `TS_ALLOWED` | — | Comma-separated allowed Tailscale hostnames/logins |
| `TS_ALLOWLIST_FILE` | — | File with one allowed identity per line |
| `TAILSCALE_SOCKET` | `/var/run/tailscale/tailscaled.sock` | tailscaled LocalAPI socket |
| `CALENDAR_TZ` | system tz | IANA tz for naive datetimes |
| `DEFAULT_CALENDAR` | first/"Home" | Calendar used when a tool omits `calendar_id` |

## Running

### HTTP mode (remote service)

```bash
export ICLOUD_USERNAME="you@icloud.com"
export ICLOUD_APP_PASSWORD="xxxx-xxxx-xxxx-xxxx"
export TS_ALLOWED="my-laptop,my-phone"
.venv/bin/python server.py
# Serving apple-calendar MCP over HTTP at http://0.0.0.0:8420/mcp
```

### stdio mode (local)

```bash
ICLOUD_USERNAME="you@icloud.com" ICLOUD_APP_PASSWORD="xxxx-..." \
  MCP_TRANSPORT=stdio .venv/bin/python server.py
```

### As a systemd service (unattended home server)

An [`apple-calendar-mcp.service`](apple-calendar-mcp.service) unit and an
[`apple-calendar-mcp.env.example`](apple-calendar-mcp.env.example) are included.
It orders after `tailscaled.service` and restarts on failure / on boot.

```bash
sudo cp -r . /opt/apple-calendar-mcp
sudo cp apple-calendar-mcp.env.example /etc/apple-calendar-mcp.env
sudo chmod 600 /etc/apple-calendar-mcp.env      # then edit it with your creds
sudo cp apple-calendar-mcp.service /etc/systemd/system/
sudo systemctl enable --now apple-calendar-mcp
journalctl -u apple-calendar-mcp -f
```

Note: the Tailscale LocalAPI socket is root-owned by default. Either run the
service as root, or make an unprivileged user the Tailscale operator
(`sudo tailscale set --operator=<user>`) and set `User=` in the unit.

## Authentication model

There is **no token or password on the client side**. Instead, on every
incoming HTTP request the server:

1. Takes the peer's source IP:port from the connection.
2. Resolves it against the local `tailscaled` LocalAPI (`WhoIs`) to get the
   requesting device's Tailscale identity (hostname + login).
3. Rejects the request unless it resolves to a device on your tailnet, and — if
   `TS_ALLOWED` / `TS_ALLOWLIST_FILE` is set — unless that identity is on the
   allowlist.
4. Logs the resolved identity, so you can see which device made each change.

**Fails closed:** if the LocalAPI socket is unreachable (Tailscale down,
permissions wrong), *every* request is rejected rather than allowed. If no
allowlist is configured, any device that resolves on your tailnet is allowed
(a warning is logged at startup) — set an allowlist to restrict to specific
devices.

## Adding it to Claude as a remote connector

In Claude, add a **custom connector** pointing at the server's MCP endpoint on
your tailnet — no token required, since auth is Tailscale-identity-based:

```
http://<your-server-hostname>.<tailnet>.ts.net:8420/mcp
```

(Use the Tailscale MagicDNS name or the `100.x` address of the host.) The
requesting device must itself be on your tailnet and (if configured) on the
allowlist.

## Try it

See [DEMO.md](DEMO.md) for a copy-paste prompt that exercises every tool on a
self-cleaning scratch calendar — safe to run against a real account.

## How it works

`server.py` is a single-file [FastMCP](https://modelcontextprotocol.io) server.
It talks to iCloud with the [`caldav`](https://pypi.org/project/caldav/) library
and translates events to/from [`icalendar`](https://pypi.org/project/icalendar/)
`VEVENT`/`RRULE`. HTTP mode wraps FastMCP's streamable-HTTP ASGI app in a
pure-ASGI Tailscale-auth middleware (so SSE streaming isn't buffered).

## License

MIT — see [LICENSE](LICENSE).
