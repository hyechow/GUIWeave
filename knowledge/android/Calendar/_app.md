---
id: knowledge.android.Calendar.navigation
source_type: knowledge_navigation
platform: android
app: Calendar
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
source: mobileworld_app_contract
confidence: medium
sensitivity: internal
ttl: session
version: 2
---
# Calendar on Android

## Interface contract

- Calendar is a separate application.
- Existing event collection:
  - resource name: `Event`
  - query fields: `start_ts` (`datetime`), `end_ts` (`datetime`), `title` (`text`),
    and `description` (`text`)
  - `start_ts` and `end_ts` are the authoritative interval boundaries; retrieve the
    complete event set before deciding whether a proposed interval overlaps an existing event
  - an overlap requires `existing.start_ts < proposed_end` and
    `existing.end_ts > proposed_start`; touching endpoints are not an overlap
- Event display by view:
  - the MONTH grid renders each event as a compact cell carrying its title plus a single
    display range string (e.g. `08:00 AM - 09:00 AM`); that range is not structured into
    `start_ts`/`end_ts` fields and is not reliably machine-extractable from the grid.
  - tapping a day opens the DAY view, where events are listed as rows with readable start
    and end times.
  - availability/overlap checks must acquire events from the DAY view (or an interval view),
    not from the month grid.
  - an interval-scoped `Event` view can be opened for the exact date/time visibly observed
    from a source record; that view shows the events in the requested interval
  - use the observed source date and time through the visible Calendar flow; do not parse
    natural-language date/time text with host code or invent a host-clock value
  - use that interval view only when a later availability query/read is required; it is not
    preparatory navigation for the schema-free New-entry contract
- Event form time picker: the New Event form's start and end time selectors open a modal
  time picker with a separate AM/PM area, one period pre-selected. The selected period must
  be verified against the requested time and switched to the correct AM/PM button before
  confirming with OK; confirming while the wrong period is active leaves the wrong time in
  the form.
- Event form date and duration: the New Event form defaults its date to the currently
  displayed calendar day and its end time to the start time. Both fields must be set
  explicitly from the source message before saving: the date to the day the message
  requests (a relative day such as "tomorrow" resolves to that specific date, not the
  displayed default), and the end time to the requested duration (an event with a stated
  length must end after that length, not equal the start time). Saving with either left at
  its default produces an event on the wrong day or with the wrong duration.
- New-entry contract:
  - existing target: none
  - preparatory entity or view: none
  - mutation fields: `title` (text), `start_ts` (`datetime`), `end_ts` (`datetime`),
    and `description` (text)
  - when creating an event from a source message, the saved form must use that message's
    exact start and end (with end derived from its stated duration), rather than guessing
    from source prose or leaving either field at its default.
- Conference-day counting: when the task asks how many days a set of meetings spans
  (e.g. "how many days of conference meetings in October"), the answer is the total
  number of calendar days the matching events cover:
  - the MONTH grid cell and search row render only PART of an event's span (often
    just the end day, e.g. `All-day (10 Friday)`), which is NOT enough to count days.
  - tap the event and read its full Start/End dates from the event detail/edit form
    (e.g. start `October 4 (Sat)`, end `October 10 (Fri)`). The day count is
    inclusive: `end - start + 1` days (Oct 4-10 = 7 days).
  - sum the day counts across every matching event; if two events share a date it is
    still counted once overall, so collect each event's start/end and union the days.
- Collect a month-scoped set of events by SEARCH, not the month grid: use the top
  search box and type the distinctive title term (e.g. `conference`). Search returns
  the matching event rows as an accessible list, which structured perception can
  read as rows (the month grid is rendered as opaque date cells that perception
  does not extract). The requirement filter carries the date range (e.g.
  `'start_ts': '2025-10-*'`) so rows from other months/years are rejected by scope
  validation. The month view is only a visual confirmation surface, not the
  acquisition surface.
- A natural date phrase in search (e.g. `conference october 2025`) is unreliable:
  it can return events from a different year or an empty result. Prefer the bare
  title term and let the filter date scope do the year bounding.
