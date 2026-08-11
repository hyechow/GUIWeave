---
id: knowledge.browser.shopping_admin.reviews_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: product reviews rating stars nickname customer review description
source: manual_curated
confidence: high
ttl: session
---
# Reviews interface

- **All Reviews** has the stable same-origin route `/admin/review/product/index/`. When a direct
  URL capability is available, prefer this exact route over opening the Marketing menu and then
  selecting User Content > All Reviews.
- Review-selection invariant: when the task names a product, every **All Reviews** candidate query
  uses the **Product** source filter for that product and projects **Action**. A rating threshold
  never replaces this product filter; it is evaluated only after reading each candidate's detail.
  Product is an association filter literal here, not a separate collection lookup: query the full
  product mention directly on All Reviews, then use the shorter literal only in that query's empty
  branch. Do not query a `Product` entity first or pass a dynamically looked-up product name.
- **All Reviews** is the exact review collection. It supports the **Product** filter and exposes
  **Action**, **Nickname**, **Title**, and **Review** on each row.
- A review status mutation only needs records not already in the requested terminal status. When
  approving unapproved reviews, or when the task explicitly targets pending reviews, use the
  **Status** filter to recall **Pending** candidates before following their Action links;
  already-approved reviews require no approval mutation.
- For a status mutation selected by **Detailed Rating**, use one cohesive operator over those
  Pending candidates. On each candidate's detail page, read Detailed Rating and, only when it
  satisfies the threshold, set **Status** to the requested value and use **Save Review**; otherwise
  return to the list without saving. Do not collect review IDs, join them into a comma-separated
  string, and hand them to a later Worker: this grid's ID filter is a range control, not an
  arbitrary-list selector, while the rating and mutation control coexist only on the detail page.
- **Action** is the row's detail locator and is required when a linked detail value is needed.
  The exact detail field spelling is **Detailed Rating** (`number`); it is never
  `detailed_rating` and never belongs to the collection row.
