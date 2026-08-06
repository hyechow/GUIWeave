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
- **Documents** is a separate directory in the same Files app (distinct from Downloads). Its rows
  expose **name** (`text`) as the row identity and **path** (`text`) as a detail field. A PDF in
  Documents is a plain Documents row whose **name** ends in `.pdf`; there is no separate PDF
  collection, so `review*.pdf` files are the Documents rows matching the `review` name prefix and
  the `.pdf` suffix.
- Moving a file is a durable per-row mutation of that identified Documents row. Reach the exact
  row (target-bound), then `commit(target=row, values={"destination_folder": <text>})`. The move
  consumes the source row: after it, the file is no longer listed under Documents and appears
  under the destination folder.
- A destination subfolder (e.g. `Document/paper`) is reached like any folder: reach it by name,
  then the same Documents-style rows list its files. Querying `Documents` after that reach returns
  the opened folder's rows, so the complete moved-file set is read from the reached subfolder, not
  from the root Documents list.
- Opening a selected ZIP exposes the complete **ArchiveEntries** collection with query field
  **name** (`text`). Entry names must be collected before extraction.
- Extracting every entry is a durable mutation of that selected ZIP with **selection** = `all` and
  **destination** = `Downloads`. It creates same-named DownloadFiles rows and consumes the archive
  view, so DownloadFiles must be queried again after extraction.
- **content** is a detail field of each concrete extracted DownloadFiles row; it is not a collection
  field. Match the fresh DownloadFiles rows to the previously collected entry names, read content
  from every match, and sum their line counts.
