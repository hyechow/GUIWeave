---
id: knowledge.browser.shopping.navigation
source_type: knowledge_navigation
platform: browser
app: shopping
scope:
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
version: 3
---
# One Stop Market

## Catalog

- The mini-search uses broad OR term matching, so it is not a bounded product-class source.
- Advanced Search > Product Name performs contiguous substring matching. When no catalog category
  covers the full class, use this recall source and preserve required source order in the approach.
- Equivalent titles preserve use/problem wording but may vary base-type labels. For Product Name
  recall, never enter the full class: use an exact noun-plus-gerund use/problem phrase when supplied,
  or the base type without modifiers. Keep the full class unchanged in the contract and row filter.
- Use the narrowest category only when its taxonomy directly covers the full class; for example,
  earbud products are under Electronics > Headphones > Earbud Headphones. Validate primary product
  identity against the full class because bundles and misclassified products may appear. Model it
  as `primary_product`, sourced from `Product Name`, with filter `primary_product_contains`. Use the
  category label as target data surface only for a category-based approach.
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

## Order history

- My Account > My Orders lists Order # and Date newest first, ten per page, without date or category
  filters. For a closed date interval, advance until the upper boundary appears; after the
  first row older than the lower boundary, later pages cannot qualify.
- View Order opens Items Ordered rows with Product Name, Price, Qty, and line Subtotal. The
  order summary separately shows Subtotal, Shipping & Handling, and Grand Total.
