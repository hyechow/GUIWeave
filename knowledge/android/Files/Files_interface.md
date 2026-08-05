---
id: knowledge.android.Files.interface
source_type: knowledge_interface
platform: android
app: Files
scope:
  - orchestrator
selector_when: Downloads earliest zip July archive entries extract files content lines
source: manual_verified
confidence: high
ttl: session
---
# Files interface

- **DownloadFiles** has complete coverage and exposes **name** (`text`), **modified_at**
  (`datetime`), and **kind** (`text`). It has no source filters. ZIP membership and the requested
  month are therefore evaluated in Python from the complete rows; a month without a year does not
  constrain the year. A row is identified by **name**.
- Opening a selected ZIP exposes the complete **ArchiveEntries** collection with query field
  **name** (`text`). Entry names must be collected before extraction.
- Extracting every entry is a durable mutation of that selected ZIP with **selection** = `all` and
  **destination** = `Downloads`. It creates same-named DownloadFiles rows and consumes the archive
  view, so DownloadFiles must be queried again after extraction.
- **content** is a detail field of each concrete extracted DownloadFiles row; it is not a collection
  field. Match the fresh DownloadFiles rows to the previously collected entry names, read content
  from every match, and sum their line counts.
