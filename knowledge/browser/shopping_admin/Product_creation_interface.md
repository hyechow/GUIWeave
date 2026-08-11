---
id: knowledge.browser.shopping_admin.product_creation_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: create simple product stock catalog item
source: manual_curated
confidence: high
ttl: session
---
# Product creation interface

A new **Simple Product** is one new record without an existing owner. Its fields are **Name**,
**Price**, **Quantity**, **Stock Status**, **Size**, and **Color**. Stock Status values are
`In Stock` and `Out of Stock`.

- If the Simple Product form does not yet show a required optional field such as **Size** or
  **Color**, use the top-level **Add Attribute** action. In the Select Attribute grid, select the
  existing `size` and `color` rows and choose **Add Selected**; their value controls then become
  part of this product form inside a new collapsed **Attributes** section near the bottom. Expand
  that section and select the requested Size and Color values before saving.
- Do not use **Create Configurations** for this case. That workflow generates variation members
  for a Configurable Product, while a Simple Product stores its own Size and Color values directly.
