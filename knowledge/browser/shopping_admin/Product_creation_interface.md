---
id: knowledge.browser.shopping_admin.product_creation_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: create simple product stock catalog item
source: manual_curated
confidence: high
ttl: session
---
# Product creation interface

A new **Simple Product** is one new record without an existing owner. Its fields are **Name**,
**Price**, **Quantity**, **Stock Status**, **Size**, and **Color**. Stock Status values are
`In Stock` and `Out of Stock`.

- If the Simple Product form does not yet show a required optional field such as **Size** or
  **Color**, first expand the collapsed **Attributes** section near the bottom of the form.
  If several form scrolls never reveal an **Attributes** heading, stop scrolling and use
  the page-header **Add Attribute** action. A 0-row **Add Attribute** filter on Attribute
  Code means that attribute is already on this attribute set — close the modal and set the
  value in **Attributes**; do not page the modal or **Create New Attribute**. Filter the
  modal by **Attribute Code** instead of paging. Check the matching row, then **Add Selected**
  on the modal chrome (often top-right), not in the page header. Keep a checked row selected
  while searching for the next code; clearing the filter or paging the modal can drop the
  checkbox. After the attributes are on the form, expand **Attributes**, set the requested
  option labels, and **Save**.
- Do not use **Create Configurations** for this case. That workflow generates variation members
  for a Configurable Product, while a Simple Product stores its own Size and Color values directly.
