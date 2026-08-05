---
id: knowledge.browser.shopping_admin.orders_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: customer email completed monthly purchase date payment pending order notification shipment tracking USPS
source: manual_curated
confidence: high
ttl: session
---
# Orders interface

- Range-filter invariant: every explicit start/end period uses the **Purchase Date** source filter,
  including when output groups rows by month. A month-to-month period runs from the first day of
  the first month through the last day of the last month. Purchase Date ranges use
  `{"from": "MM/DD/YYYY", "to": "MM/DD/YYYY"}`; Python groups only the filtered rows.
- Chronological-order invariant: `last`, `latest`, `recent`, and `oldest` orders are ranked only by
  typed **Purchase Date**. **ID** is never a chronological substitute. A total for the last N
  completed orders therefore queries **Purchase Date** (`datetime`) and **Grand Total (Purchased)**
  (`money`) with Status `Complete`, sorts by Purchase Date, slices N, and sums the totals.
- **Orders** is the exact collection name. Its source-native filters are **ID**, **Status**,
  **Bill-to Name**, **Ship-to Name**, **Customer Email**, and **Purchase Date**.
- A displayed order number `#N` uses numeric **ID** value `N`. Completed status is `Complete`.
- Queryable values include **ID**, **Customer Email** (`text`), **Purchase Date** (`datetime`),
  and **Grand Total (Purchased)** (`money`). Purchase Date is the chronological ordering field.
- An Orders row owns its order-detail mutations. A customer notification uses **Comment** (`text`)
  and **Notify Customer by Email** (`boolean`). Shipment tracking uses **Carrier** and
  **Tracking Number** on the owning order flow. USPS maps to `United States Postal Service`, UPS
  to `United Parcel Service`, and FedEx to `Federal Express`.
