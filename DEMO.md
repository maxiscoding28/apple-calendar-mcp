# Demo prompt

Paste the block below into any Claude client with the `apple-calendar` MCP
server connected — locally over stdio, or remotely via the Tailscale-auth HTTP
connector — to see every capability in one run. The scratch calendar it creates
lives in your iCloud account and is deleted at the end, so **it won't touch any
of your real events.**

---

```
Use the apple-calendar tools to run a guided demo of your calendar abilities.
Narrate each step as you go. Do everything on a dedicated scratch calendar so my
real calendars are untouched.

1. List my calendars and tell me which is the default.
2. Create a new calendar called "MCP Demo" with color #5856D6.
3. On the MCP Demo calendar, create:
   - a one-off event "Coffee with Sam" tomorrow from 3:00pm to 3:30pm, and
   - a recurring event "Standup" every weekday (Mon–Fri) at 9:00–9:15am,
     ending after 10 occurrences.
4. List all events on the MCP Demo calendar for the next two weeks so I can see
   the recurring occurrences expanded out.
5. Move "Coffee with Sam" to 4:00pm and set its location to "Blue Bottle".
6. Take the SECOND Standup occurrence and detach it into a standalone,
   non-recurring event titled "Standup (all-hands)", and remove that occurrence
   from the series so it isn't duplicated.
7. Delete just the THIRD remaining Standup occurrence (that one only).
8. Now delete the rest of the Standup series in one shot.
9. Change the MCP Demo calendar color to #FF9500.
10. Show me the final state of the MCP Demo calendar's events.
11. Clean up: permanently delete the MCP Demo calendar and confirm it's gone.
```

---

## What it exercises

| Step | Tool(s) |
|------|---------|
| 1 | `list_calendars` |
| 2 | `create_calendar` (+ color) |
| 3 | `create_event` (one-off **and** recurring) |
| 4 | `list_events` (recurrence expanded to occurrences) |
| 5 | `update_event` (time + location) |
| 6 | `detach_occurrence` (standalone copy + remove from series) |
| 7 | `delete_event` (span `this` — single occurrence) |
| 8 | `delete_event` (span `all` — whole series) |
| 9 | `set_calendar_color` |
| 11 | `delete_calendar` (guarded hard delete) |
