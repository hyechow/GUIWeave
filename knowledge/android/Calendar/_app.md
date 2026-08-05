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
  - planner-visible mutation fields: none
  - business description: the exact source text observed in the originating application
- A generic summary or implicit active context cannot replace the business description
  because the Calendar UI resolves the source's date, time, duration, and title.
