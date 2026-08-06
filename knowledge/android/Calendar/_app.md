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
version: 1
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
  - an interval-scoped `Event` view can be opened for the exact date/time described by
    an observed source record; after that reach, a complete `Event` query returns the
    events in the requested interval
  - pass the observed source text as the `source_text` runtime value in the interval-view
    reach so Calendar resolves its date and time; do not parse natural-language date/time
    text with host code or invent a host-clock value
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
  - when creating an event from a source message, the program reads the event's start and
    end as semantic `datetime` fields off that source (end derived from the message's stated
    duration), then passes those structured values in the commit. It does not embed the raw
    source prose in the commit goal and leave the executor to guess the time; the executor
    fills the declared fields from the supplied values.
