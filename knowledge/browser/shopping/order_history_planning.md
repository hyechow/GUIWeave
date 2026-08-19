---
id: knowledge.browser.shopping.order_history_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: One Stop Market orders order history bought purchased shopping date subtotal shipping size color option configuration
source: official_trace_distilled
confidence: high
sensitivity: internal
ttl: session
version: 2
---
# One Stop Market order-history data

My Orders has no date, product-category, or category-spending aggregate. Its newest-first list
provides Order # and Date; Date is a complete calendar date displayed as `M/D/YY` and should be
normalized as `datetime`. Each View Order detail provides Items Ordered rows with Product
Name, Qty, and line Subtotal. For spending over a date interval and product class, use one linked
order-detail collection at line-item grain. Retain Order # as the required stable parent identity
because the detail title identifies its order by that value; also retain Date, then sum matching line
Subtotals deterministically. Do not use the order-level Subtotal or Grand Total for a subset of items,
and do not include Shipping & Handling.

For a requested attribute of a purchased item, use the same linked line-item collection. Its schema
must retain required `order_number` from Order # as the stable parent identity, plus the Date; omitting
Order # prevents attribution on linked details. Collect the raw Product Name cell: it contains the
title and SKU followed by any selected option labels and values. Use `primary_product`, sourced from
Product Name, as the sole schema field for both item identity and raw option text; apply the requested
product class with `primary_product_contains`, then parse the requested labeled option from that field
deterministically after collection. Do not invent separate
Purchase Year, Product Type, Width, Height, Color, or other columns that the order detail does not
provide. A lookup naming one purchased item must not be pluralized into line items: use
`cardinality="one"` and `coverage="first_match"`, even when the requested response is an array. In
Product Name option text, a compound Size value is shown after the literal `Size` label in
width-by-height order; the next option label, such as `Color`, ends that value. Isolate those label
boundaries before splitting; a quote mark denotes inches, so return each component as
`<number> inches` rather than dropping the unit.

`Forest Canvas` is this storefront's picture-frame product title; the title omits the category
words. For this singular purchased-item lookup, describe the one matching line item as
`Forest Canvas`, the picture-frame product, so the requirement carries this explicit taxonomy
equivalence while preserving the user's `picture frame` filter and `cardinality="one"` /
`coverage="first_match"`.
