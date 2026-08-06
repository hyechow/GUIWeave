---
id: knowledge.android.Mail.navigation
source_type: knowledge_navigation
platform: android
app: Mail
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
source: manual_verified
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# Mail on Android

- Mail is the email application. The inbox lists received messages; the compose
  action opens a new-message form.
- The compose form carries a recipient (`To`) field, a subject field, and an attach
  action; the send action submits the message. The `To` recipient is typed as an
  email address.
- Attaching files opens a file picker rooted in the file directories; the files moved
  into the destination folder are selectable there and are attached by name.

## Interface contract

- Sending an email is an untargeted commit that owns the compose form. Its mutation
  fields are `recipient` (`text`), `subject` (`text`), and `attachments` (a list of
  file names). The executor opens the compose form, fills `To` and `Subject`, attaches
  every given file, and sends. No preparatory reach or creation entity is needed
  before the send commit.
- The attachment names come from a prior Files read (the files that were moved into
  the destination folder), so the send commit passes those observed names in
  `attachments`; the file picker selects them by name.
