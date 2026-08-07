#!/usr/bin/env python3
"""Local smoke test for apple-calendar-mcp — run it in your OWN Terminal.

  Read-only (safe):       .venv/bin/python test_local.py
  Full round-trip (write): .venv/bin/python test_local.py --write

The first run triggers the macOS "access Calendar" and "access Reminders"
prompts (attributed to Terminal) — approve them.

NOTE: Claude Desktop has its OWN separate permission grants. Passing here proves
the code works; Desktop will still prompt you separately the first time.
"""

import json
import sys
from datetime import datetime, timedelta

import server


def show(label, val):
    print(f"\n=== {label} ===")
    print(json.dumps(val, indent=2, default=str))


def main():
    write = "--write" in sys.argv
    now = datetime.now()

    show("calendars", server.list_calendars())
    show(
        "events (next 7 days)",
        server.list_events(
            now.strftime("%Y-%m-%dT00:00"),
            (now + timedelta(days=7)).strftime("%Y-%m-%dT00:00"),
        ),
    )
    show("reminder lists", server.list_reminder_lists())
    show("reminders (incomplete)", server.list_reminders())

    if not write:
        print("\n(read-only done — pass --write for a create/delete round-trip)")
        return

    print("\n=== WRITE round-trip (self-cleaning) ===")
    ev = server.create_event(
        title="__MCP TEST__ delete me",
        start=(now + timedelta(days=1)).strftime("%Y-%m-%dT15:00"),
        end=(now + timedelta(days=1)).strftime("%Y-%m-%dT15:30"),
        notes="created by test_local.py",
    )
    print("created event:", ev["event_id"])
    print("  read back:", server.get_event(ev["event_id"])["title"])
    server.delete_event(ev["event_id"])
    print("deleted event OK")

    rem = server.create_reminder(
        title="__MCP TEST__ delete me",
        due=(now + timedelta(days=1)).strftime("%Y-%m-%dT09:00"),
        priority="high",
        notes="created by test_local.py",
    )
    print("created reminder:", rem["reminder_id"])
    print("  read back:", server.get_reminder(rem["reminder_id"])["title"])
    server.delete_reminder(rem["reminder_id"])
    print("deleted reminder OK")

    print("\nWrite round-trip complete — nothing left behind.")


if __name__ == "__main__":
    main()
