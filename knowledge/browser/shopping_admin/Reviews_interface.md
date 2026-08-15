---
id: knowledge.browser.shopping_admin.reviews_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: reviews rating stars nickname status product
source: manual_curated
confidence: high
ttl: session
---
# Reviews interface

- **All Reviews** has the stable same-origin route `/admin/review/product/index/`. When a direct
  URL capability is available, prefer this exact route over opening the Marketing menu and then
  selecting User Content > All Reviews.
- **All Reviews** is the exact review collection. It supports the **Product** filter and exposes
  **Action**, **Nickname**, **Title**, and **Review** on each row.
- Mention, keyword, and review-count queries use **All Reviews**, not **Reports > Reviews**
  (By Customers / By Products / Customer Reviews Report) — those are aggregates without review text.
- To recall reviews whose text matches a keyword, use the **Review** source filter (`text`) and enter
  the exact keyword literal. The review-text filter input is labeled **Review** on the grid filter
  row (it is a `text` input, not a select); do not confuse it with the **Visibility** / **Type**
  `native_select` controls that sit alongside it.
- When a query names a product, every **All Reviews** candidate query uses the **Product** source
  filter and projects **Action**. A rating threshold is not a grid filter; it is evaluated only
  after reading each candidate's detail. Product is an association filter literal, not a separate
  collection lookup. The control matches a contiguous substring of the stored product name.
  An empty result means that literal does not occur in any stored name. Do not Reset-and-scan
  the unfiltered grid, drop Product, or look up a Product entity first.
- A review status or deletion mutation only needs records not already in the requested terminal
  state. When the query explicitly targets pending reviews, use the **Status** filter to recall
  **Pending** candidates before following their Action links; already-approved reviews require no
  approval mutation.
- **Status**, **Delete Review**, and **Detailed Rating** coexist only on the review editor.
  The grid **ID** filter is a range control, not an arbitrary-list selector, so do not collect
  review IDs and hand them to a later Worker.
- On the review editor, **Next** (or **Save and Next** after a mutation) advances through
  the current filtered set in increasing review ID, not the grid's newest-first sort.
  **Previous** reverses that walk. If **Next** is absent, the editor is the last row of
  this page — return to the grid and open the next page of the same filtered set.
- Magento persists leftover All Reviews filters via `ui_bookmark`. A leftover **Product**
  token from a prior session is not the current query. **Clear all** before applying this
  query's filters.
- **Action** is the row's detail locator and is required when a linked detail value is needed.
  The exact detail field spelling is **Detailed Rating** (`number`). It is not a
  grid column: declare it on the collection schema as a linked-detail field and
  acquire it on every candidate editor. The control lives on the same form as
  **Nickname**. Read its selected 1–5 value; do not count painted stars, and do
  not use the display-only **Summary Rating** row.
