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
version: 3
---
# Mail on Android

- Mail is the email application. The inbox lists received messages; the compose
  action opens a new-message form.
- The compose form carries a recipient (`To`) field, a subject field, a message body,
  and an attach action; the send action submits the message. The `To` recipient is
  typed as an email address.
- Attaching files opens a file picker rooted in the file directories; the files moved
  into the destination folder are selectable there and are attached by name.
- For “all files in a folder”, include every direct file regardless of type. Use `Show roots` and
  device storage because the `Documents` category can omit types. The picker may return after one
  selection; verify the attachment in compose and reopen it for each remaining file.

## Interface contract

- Sending an email owns the compose form: fill `recipient` and `subject`; when the task asks to
  tell, ask, explain, or include message content, fill the body with that content before Send.
  Attach files through the picker until the visible pending attachment set exactly matches the
  request, then activate Send.
