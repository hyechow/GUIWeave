---
id: knowledge.browser.shopping_admin.product_workspace_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: product description configurable parent configuration green tee existing
source: manual_curated
confidence: high
ttl: session
---
# Existing product interface

- A configurable parent has Type `Configurable Product` and owns **Short Description** and
  **Configurations**. A simple variation has Type `Simple Product` and owns **Price**,
  **Quantity**, and **Stock Status**.
- The configurable owner is selected from **Products** by both **Name** and
  **Type** = `Configurable Product`; Name alone can also match simple variations.
- A **Short Description** change therefore uses Products collection fields **Name** and **Type**,
  with both the requested Name and Type `Configurable Product` as source filters. The full-name and
  fallback-name lookups preserve that same Type discriminator.
- Adding a Size/Color combination to an existing named configurable product adds one
  **Configurations** member on that parent. Configurations is a list-valued parent field with
  member shape `{"Color": <color>, "Size": <size>}`; it is not two top-level parent fields and not
  a separate Simple Product creation.
- Supplying one requested Configurations member appends that member and preserves unrelated
  existing members; the current Configurations collection is not read or merged by the caller.
