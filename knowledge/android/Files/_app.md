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
version: 2
---
# Files on Android

- The Files launcher opens the `Downloads` directory. Its file rows display a name,
  last-modified date, size, and media type.
- The `Documents` chip in Downloads' horizontal filter row is only a MIME/category filter;
  it is not the Documents directory. To change directories, use the top-left `Show roots`
  navigation drawer.
- Activating a ZIP row opens its archive entries in place. Files cannot preview a text
  document while it remains inside an archive.
- To make every archive entry readable, open `More options`, use `Select all`, reopen
  `More options`, choose `Extract to…`, select `Downloads`, and
  activate `EXTRACT`. This persistently extracts the files. After extraction, return
  from the archive to Downloads.
- In an opened archive, the top-left three-line control is the navigation-drawer
  button; it does not return to the parent directory. Use the Android system `Back`
  action to leave the archive and return to Downloads. Do not offer those two controls
  as interchangeable alternatives.
- In the `Extract to…` destination screen, a `Downloads` title/breadcrumb means the
  destination is already Downloads. The explicit `EXTRACT` button performs the final
  persistent action. The Android accessibility node named `pick_button_overlay` is
  only an implementation overlay beside it; it is not a folder picker.
- Opening an extracted TXT file may show an `Open with` chooser. `HTML Viewer` followed
  by `Just once` displays the complete plain-text document; navigate up to return to
  Downloads for the next file.
- The drawer's `Documents` item is an indexed category that can omit descendants or file types,
  including in attachment pickers. For the physical folder, use `Show roots` → device storage →
  `Documents`.
- A physical folder grid shows direct children, while top-bar search matches descendants
  recursively. Search an exact name or distinctive prefix before scrolling.
- A file is moved by targeting the row and choosing its move action with a destination
  folder. Moving consumes the source row: after the move the file appears only under
  the destination.
- In `Move to…`, keep the selection active and use the picker's `Show roots` and device storage to
  reach the destination before activating `MOVE`.

## Interface contract

- `DownloadFiles` collection:
  - coverage: complete
  - exact query fields: `name` (`text`), `modified_at` (`datetime`), `kind` (`text`)
  - exact source filter fields: none
  - row identity fields: `name`
  - detail fields: `content`
- A ZIP has an exact visible `name` ending in `.zip`.
- `ArchiveEntries` collection:
  - coverage: complete
  - query fields: `name` (`text`)
  - route identity fields: `name`, equal to the exact selected archive `name`
- Archive extraction mutates the selected archive directly; there is no preparatory
  Extraction entity. Its mutation fields and exact values for extracting every entry
  are `selection` equal to `all` and `destination` equal to `Downloads`. Extraction
  consumes the opened archive view and creates same-named `DownloadFiles` rows.
- Archive entry names are visible only in `ArchiveEntries` before extraction. They are
  the identities for matching the same-named extracted rows afterward, so they must be
  observed before the extraction consumes that view.
- `content` belongs to a concrete `DownloadFiles` row while that collection remains
  active. Concrete-row detail is its only retrieval interface. There is no separate
  FileContent entity, and `content` is never a collection query or source filter field.
- After extraction, the complete `DownloadFiles` names provide the matching surface;
  the collection has no name-prefix, month, type, or callable source filter. Extracted
  rows do not exist in any pre-extraction collection snapshot, so matching requires a
  fresh complete `DownloadFiles` snapshot after extraction.
