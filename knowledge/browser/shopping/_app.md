---
id: knowledge.browser.shopping.navigation
source_type: knowledge_navigation
platform: browser
app: shopping
scope:
  - orchestrator
  - worker
aliases:
  - One Stop Market
browser_origins:
  - http://localhost:7770
  - http://127.0.0.1:7770
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market catalog

- The mini-search uses broad term matching. A multi-word query can return products that match
  only some terms, so its result set is not authoritative for an exact product class.
- The catalog exposes a category hierarchy. For a product-class task, prefer the narrowest
  category for recall; for example, earbud products are under Electronics > Headphones > Earbud
  Headphones. Category membership alone is not authoritative row identity because bundles or
  misclassified products can appear there. Validate each row's primary product against the full
  task-supplied product-class phrase; a matching component mentioned inside a different primary
  product is not sufficient. Model this as normalized field `primary_product`, sourced from the
  visible `Product Name`, and filter with `primary_product_contains`. Use the category label as
  the target data surface so products encountered during navigation do not enter the collection.
- In this catalog taxonomy, an in-ear or behind-neck headphone/headset is an earphone product.
  Preserve this taxonomy in the data-requirement description when that distinction affects a
  product-class predicate, so semantic row validation can accept equivalent catalog wording.
- Product lists support Sort By = Price and a separate direction link. The direction link label
  names the action it will perform: `Set Ascending Direction` means the current order is
  descending, and `Set Descending Direction` means the current order is ascending.
- Product grids preserve the selected sort sequence from left to right across each row, then
  from top to bottom.
- For a filtered price boundary, sort in the needed direction and take the first row that
  satisfies the remaining row-level predicate. Do not traverse the whole category.
