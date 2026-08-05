---
id: knowledge.browser.shopping_admin.marketing_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: marketing price rule discount registered customers checkout cart sales orders report date range
source: manual_curated
confidence: high
ttl: session
---
# Marketing and reports interface

- **Cart Price Rules** and **Catalog Price Rules** are separate new-record resources without an
  existing owner. Cart Price Rules use **Rule Name**, **Active**, **Websites**,
  **Customer Groups**, **Coupon**, **Apply**, and **Discount Amount**.
- **Websites** and **Customer Groups** are multi-value lists. The website is `Main Website`.
  Registered customer groups are `General`, `Wholesale`, and `Retailer`; `NOT LOGGED IN` is the
  guest group. The no-coupon value is `No Coupon`.
- Percentage cart discounts use `Percent of product price discount`; Discount Amount is numeric.
- Each report subtype is a transient **Sales Reports** rendered page, not a durable record. Its
  final state is identified by report subtype, **From**, **To**, and `rendered=true`.
- From and To use `MM/DD/YYYY`. Subtypes include `Orders`, `Tax`, `Invoiced`, `Shipping`,
  `Refunds`, `Coupons`, and `PayPal Settlement`. Showing a report does not request a numeric result.
  `rendered` is an observable success condition, not a readable field; reaching that state completes
  the show-report request without a subsequent read or return value.
