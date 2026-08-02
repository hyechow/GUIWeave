---
id: knowledge.android.Files.navigation
source_type: knowledge_navigation
platform: android
app: Files
scope:
  - decompose
  - planner
  - replanner
source: manual_verified
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# Files on Android

- The Files launcher opens the `Downloads` directory. Its file rows display a name,
  last-modified date, size, and media type.
- Activating a ZIP row opens its archive entries in place. Files cannot preview a text
  document while it remains inside an archive.
- To make every archive entry readable, open `More options`, use `Select all`, reopen
  `More options`, choose `Extract to…`, select `Downloads`, and
  activate `EXTRACT`. This is a persistent file extraction and therefore a commit, not
  navigation. After extraction, return from the archive to Downloads.
- In an opened archive, the top-left three-line control is the navigation-drawer
  button; it does not return to the parent directory. Use the Android system `Back`
  action to leave the archive and return to Downloads. Do not offer those two controls
  as interchangeable alternatives.
- In the `Extract to…` destination screen, a `Downloads` title/breadcrumb means the
  destination is already Downloads. Activate the explicit `EXTRACT` button with
  `atomic_role=commit`. The Android accessibility node named `pick_button_overlay` is
  only an implementation overlay beside that final action; it is not a folder picker
  and must not be activated as a destination-selection step.
- Opening an extracted TXT file may show an `Open with` chooser. `HTML Viewer` followed
  by `Just once` displays the complete plain-text document; navigate up to return to
  Downloads for the next file.

## Planning boundary

- The Downloads collection entity is `DownloadFiles`. Its complete-list query contract is
  `fields={"name": "text", "modified_at": "datetime"}, filters={}, coverage="complete"`.
  It also exposes `kind` as `text`. A ZIP file has an exact visible
  `name` ending in `.zip`; use that lossless identifier suffix together with
  `modified_at` to select a ZIP by time, without requiring a separate derived `kind`
  field. Query the complete collection. The file list has no
  field-specific month/type filter: the valid `DownloadFiles` filter-key set is empty,
  so use `filters={}` (or omit it), then select `.zip` names and month `7` in Python.
  A request for July without a year must not invent a current year.
- An opened ZIP exposes the complete `ArchiveEntries` collection with `name` as `text`;
  bind that state to its source row with top-level `name` equal to the selected row's
  exact `name`.
- Extracting all opened entries to Downloads is represented by
  `ctx.commit(..., values={"selection": "all", "destination": "Downloads"})`, bound
  to the exact archive row used to reach `ArchiveEntries`. Query and retain every
  `ArchiveEntries.name` before this commit because the commit consumes that active UI.
- After extraction, query `DownloadFiles` again and match each original archive-entry
  `name` to the same-named extracted row in Python. `content` is detail-only and is not
  a `DownloadFiles` query field or list filter. Read each matched row's `content` as
  text while `DownloadFiles` remains the active collection; do not reopen
  `ArchiveEntries` between this query and those reads. Line counting and aggregation
  are ordinary Python over the read results.
