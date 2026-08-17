---
id: knowledge.android.Files.interface
source_type: knowledge_interface
platform: android
app: Files
scope:
  - orchestrator
selector_when: Downloads earliest zip July archive entries extract files content lines Documents folder review pdf move paper 移动 文件夹 邮件 Documents
source: manual_verified
confidence: high
ttl: session
---
# Files interface

- **DownloadFiles** has complete coverage and exposes **name** (`text`), **modified_at**
  (`datetime`), and **kind** (`text`). It has no source filters. ZIP membership and the requested
  month are therefore evaluated in Python from the complete rows; a month without a year does not
  constrain the year. A row is identified by **name**.
- **Documents** means the physical folder reached through `Show roots` → device storage →
  `Documents`; the drawer's namesake item is an indexed category that can omit descendants or file
  types. Its grid contains direct children and top-bar search matches descendants recursively, so
  search an exact name or distinctive prefix before scrolling. Rows expose **name** (`text`) as
  identity and **path** (`text`) as detail; a PDF row's name ends in `.pdf`.
- The `Documents` chip in Downloads' horizontal filter row filters by file type and does not
  navigate to that directory. Directory navigation uses the top-left `Show roots` drawer and its
  `Documents` item.
- Attachment pickers have the same category limitation. Use the physical path when complete folder
  contents are required.
- Moving a file is a durable per-row mutation of that identified Documents row. Reach the exact
  row (target-bound), then `commit(target=row, values={"destination_folder": <text>})`. The move
  consumes the source row: after it, the file is no longer listed under Documents and appears
  under the destination folder.
- In `Move to…`, preserve the selection, navigate within the destination picker via `Show roots`
  and device storage, then activate `MOVE`.
- A destination subfolder (e.g. `Document/paper`) is reached like any folder: reach it by name,
  then the same Documents-style rows list its files. Querying `Documents` after that reach returns
  the opened folder's rows, so the complete moved-file set is read from the reached subfolder, not
  from the root Documents list.
- Opening a selected ZIP exposes the complete **ArchiveEntries** collection with query field
  **name** (`text`). Locate a computed exact archive name through the Downloads top-bar search,
  then visually open the matching row; the name is text entered into search, not a tap target.
  Entry names identify the same-named DownloadFiles rows created by extraction.
- Extracting every entry is a durable mutation of that selected ZIP with **selection** = `all` and
  **destination** = `Downloads`. It creates same-named DownloadFiles rows but leaves the archive
  view open; return to Downloads after confirmation, then query the fresh DownloadFiles rows. The
  Downloads view is not scoped to the latest extraction and may also contain unrelated prior files;
  the archive entry **name** is the identity link to its same-named DownloadFiles row.
- **content** is a detail field of each concrete extracted DownloadFiles row; it is not a
  collection field.
