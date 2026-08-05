---
id: knowledge.browser.shopping_admin.products_query_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: products material quantity units SKU inventory detail
source: manual_curated
confidence: high
ttl: session
---
# Products query interface

- **Products** supports **Name**, **Type**, **SKU**, and **Quantity** filters and exposes
  **Name** (`text`), **Type** (`text`), **SKU** (`text`), and **Quantity** (`number`).
- **Material** and **Size** are not collection fields and cannot appear in a Products collection
  projection; they belong to product detail.
- Material resolution has one application-specific inheritance rule. A request for Material of
  products selected by Quantity first queries every matching Products row with exactly **Name**,
  **Type**, and **SKU**, then reads **Material** from every row. When that value is empty on a
  `Simple Product`, Material is inherited from its `Configurable Product` parent: remove the final
  two hyphen-separated variation segments from the child SKU, query that base SKU together with
  Type `Configurable Product`, and read the parent's Material. The business result is all distinct
  nonempty resolved Material values, preserving first-seen order.
