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
version: 6
---
# Mail on Android

- Mail is the email application. The inbox lists received messages; the compose
  action opens a new-message form.
- The compose form carries a recipient (`To`) field, a subject field, a message body,
  and an attach action; the send action submits the message. The `To` recipient is
  typed as an email address.
- Mail search matches ordinary message text; it does not interpret advanced date operators or
  date phrases as scope syntax. Search with distinctive content terms for recall, then verify
  date predicates from the visible message date or opened message. Use the navigation drawer's
  `All mail` source when the current folder does not contain the requested received messages.
- Attaching files opens a file picker rooted in the file directories. Activating an exact file
  row attaches that file and returns to Compose; Android Back leaves the visible file unattached.
  Reopen the picker for each additional file and verify each returned attachment by name.
- One invocation of an incoming attachment's blue download control copies the file into Android
  `Downloads`. This control has no visual completion state: the open email and blue control remain
  unchanged after the copy. A matching Runtime invocation receipt therefore completes the download
  step; do not invoke the same control again on that unchanged screen. When the task needs content
  inside the file, open the exact `Downloads` row through `Files` and a compatible viewer, record
  the visible content as Evidence, then return to Mail. Repeating the download can fail because the
  destination file already exists.
- For “all files in a folder”, include every direct file regardless of type. Use `Show roots` and
  device storage because the `Documents` category can omit types. The picker may return after one
  selection; verify the attachment in compose and reopen it for each remaining file.

## Interface contract

- Sending an email owns the compose form: fill `recipient` and `subject`; when the task asks to
  tell, ask, explain, or include message content, fill the body with that content before Send.
  Attach files through the picker until the visible pending attachment set exactly matches the
  request, then activate Send.
