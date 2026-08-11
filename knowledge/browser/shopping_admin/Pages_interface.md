---
id: knowledge.browser.shopping_admin.pages_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: change page title home privacy CMS content
source: manual_curated
confidence: high
ttl: session
---
# Pages interface

**Pages** uses **Title** as its exact filter and row identity. To mutate a page, use the matching
row's **Select > Edit** action to open the full page editor; clicking the row body only opens the
grid's inline editor and is not the record-edit workflow. The full editor owns the distinct
mutable field **Page Title**, and **Save** persists that page record.
