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

- Review-selection invariant: when the task names a product, every **All Reviews** candidate query
  uses the **Product** source filter for that product and projects **Action**. A rating threshold
  never replaces this product filter; it is evaluated only after reading each candidate's detail.
  Product is an association filter literal here, not a separate collection lookup: query the full
  product mention directly on All Reviews, then use the shorter literal only in that query's empty
  branch. Do not query a `Product` entity first or pass a dynamically looked-up product name.
- **All Reviews** is the exact review collection. It supports the **Product** filter and exposes
  **Action**, **Nickname**, **Title**, and **Review** on each row.
- **Action** is the row's detail locator and is required when a linked detail value is needed.
  The exact detail field spelling is **Detailed Rating** (`number`); it is never
  `detailed_rating` and never belongs to the collection row.
