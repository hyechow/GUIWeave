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
- The Products **Quantity** filter has separate `from` and `to` inputs. Exact `N` requires both
  bounds; `from` alone produces `N - ...`. They are controls, not result fields: the logical filter
  remains `quantity = N`; never add `quantity_from`/`quantity_to` to the row schema.
- **Material** and **Size** are not Products grid columns; they must be read from product detail.
  A task that requests either field still includes it in the logical collection output while the
  GUI Worker traverses the linked detail pages.
- Material resolution has one application-specific inheritance rule. A request for Material of
  products selected by Quantity first queries every matching Products row with exactly **Name**,
  **Type**, and **SKU**, then reads **Material** from every row. When that value is empty on a
  `Simple Product`, Material is inherited from its `Configurable Product` parent: remove the final
  two hyphen-separated variation segments from the child SKU, temporarily remove the original
  Quantity filter, locate the base SKU, and choose the row whose Type is `Configurable Product`;
  add SKU/Type filters only when the current results do not identify that row uniquely. Read the
  parent's Material, then restore the original Quantity scope before continuing candidate traversal.
  The business result is all distinct nonempty resolved Material values, preserving first-seen order.
  The final `material` schema is `{"type": "string", "minLength": 1}`: an empty child value is
  intermediate resolution state, never a completed row.
