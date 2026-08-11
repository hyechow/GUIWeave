---
id: knowledge.browser.shopping_admin.product_workspace_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: product description configurable parent configuration green tee existing size color variants
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
- The parent editor's **Edit Configurations** wizard owns creation of a new combination. In its
  Attribute Values step, clear inherited selections for each relevant attribute, select only the
  requested Size and Color values, advance through the remaining steps, use **Generate Products**,
  then save the parent. A selected value in the wizard is not durable until both generation and
  the parent save have completed.
- Supplying one requested Configurations member appends that member and preserves unrelated
  existing members; the current Configurations collection is not read or merged by the caller.
- A request to add one or more Size values to all existing Color variants is one mutation on the
  configurable parent. Use that parent's Configurations editor to include the requested Size
  values together with every existing Color value, then save the parent; do not edit each simple
  variation as an independent product.
