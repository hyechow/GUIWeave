---
id: knowledge.browser.shopping.order_history_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: orders order number history bought purchased date status total price cost spend spent subtotal shipping refund first latest oldest item SKU size color option configuration canceled cancelled reorder
source: official_trace_distilled
confidence: high
sensitivity: internal
ttl: session
version: 4
---
# One Stop Market order-history data

## List and detail sources

My Orders is newest first and has no filters. Its list rows provide Order #, Date, Order Total,
Status, and a View Order link. Date is a complete calendar date displayed as `M/D/YY`; normalize it
as `datetime`. Use the list alone for status/date/order-total questions. Status is an exact label,
not free text: treat user wording "cancelled" as visible `Canceled` and "completed" as `Complete`;
otherwise retain the requested phrase's exact spelling and capitalization, never approximating it
with another label.

Each View Order detail provides order date/status, addresses, Items Ordered, and a totals block.
Items Ordered rows provide Product Name, Price, Qty, and line Subtotal. The totals block separately
provides Subtotal, Shipping & Handling, and Grand Total. The storefront does not expose an arrival
date, so its value is unavailable even when a status is visible.

## Chronology and linked item lookup

For latest or most recent, return exactly one order record: the first qualifying row in the
newest-first list. Stop as soon as it satisfies all list-level predicates. For first/oldest,
traverse to the final page and use the oldest qualifying row. For the date last ordered a product,
traverse orders newest first and inspect linked Items Ordered until the first matching line item is
found; do not open every remaining order after that.

For spending over a date interval and product class, use one linked order-detail collection at
line-item grain. Retain Order # as the required stable parent identity because the detail title
identifies its order by that value; also retain Date, then sum matching line Subtotals
deterministically. Do not use the order-level Subtotal or Grand Total for a subset of items, and do
not include Shipping & Handling.

For whole-order counts or spending, filter list rows by date and status first. Count qualifying
orders once each. Use Grand Total when shipping/handling must be included and Subtotal when shipping
must be excluded; do not sum both. An empty bounded result is not a numeric zero unless the task
explicitly requests zero for no matches.

## Refund arithmetic

For canceled-order refund estimates, first bound orders by visible `Canceled` status and date. If
shipping is refundable, sum Grand Total. If shipping is not refundable, sum Subtotal. If the user
also keeps one item, subtract that item's line Subtotal from the otherwise refundable merchandise
Subtotal. Do not subtract the whole order, item unit price, or shipping twice.

## Purchased options and identity

For a requested attribute of a purchased item, use the same linked line-item collection. Its schema
must retain required `order_number` from Order # as the stable parent identity, plus Date; omitting
Order # prevents attribution on linked details. Collect the raw Product Name cell: it contains the
title and SKU followed by any selected option labels and values. Use `primary_product`, sourced from
Product Name, as the sole schema field for both item identity and raw option text; apply the requested
product class with `primary_product_contains`, then parse the requested labeled option from that
field deterministically after collection. Do not invent separate Purchase Year, Product Type,
Width, Height, Color, or other columns that the order detail does not provide.

A lookup naming one purchased item must not be pluralized into line items: use
`cardinality="one"` and `coverage="first_match"`, even when the requested response is an array. In
Product Name option text, a compound Size value is shown after the literal `Size` label in
width-by-height order; the next option label, such as `Color`, ends that value. Isolate those label
boundaries before splitting. A quote mark denotes inches, so return each component as
`<number> inches` rather than dropping the unit.

`Forest Canvas` is this storefront's picture-frame product title; the title omits the category
words. For this singular purchased-item lookup, describe the one matching line item as
`Forest Canvas`, the picture-frame product, so the requirement carries this explicit taxonomy
equivalence while preserving the user's `picture frame` filter and `cardinality="one"` /
`coverage="first_match"`.
