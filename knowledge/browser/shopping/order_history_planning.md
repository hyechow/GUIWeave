---
id: knowledge.browser.shopping.order_history_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: One Stop Market orders order history spent purchased shopping date subtotal shipping
source: official_trace_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market order-history spending

My Orders has no date, product-category, or category-spending aggregate. Its newest-first list
provides Order # and Date; Date is a complete calendar date displayed as `M/D/YY` and should be
normalized as `datetime`. Each View Order detail provides Items Ordered rows with Product
Name, Qty, and line Subtotal. For spending over a date interval and product class, use one linked
order-detail collection at line-item grain. Retain Order # as the required stable parent identity
because the detail title identifies its order by that value; also retain Date, then sum matching line
Subtotals deterministically. Do not use the order-level Subtotal or Grand Total for a subset of items,
and do not include Shipping & Handling.
