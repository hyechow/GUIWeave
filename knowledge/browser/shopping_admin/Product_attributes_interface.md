---
id: knowledge.browser.shopping_admin.product_attributes_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: product attribute swatch option attribute code values size new
source: manual_curated
confidence: high
ttl: session
---
# Product attributes interface

- **Product Attributes** has the stable same-origin route
  `/admin/catalog/product_attribute/`. It is also reachable from
  **Stores > Attributes > Product**; it is not under the Catalog sidebar menu. When the direct URL
  capability is available, prefer the stable route over guessing a menu path.
- Attribute-option mutation invariant: adding a Size option always selects the existing
  **Product Attributes** row by **Attribute Code** = `size`, then uses a target-bound reach and
  targeted commit on that row. It is never an untargeted new-record commit.
- **Product Attributes** is filtered by **Attribute Code** or **Default Label**. The existing Size
  row has Attribute Code `size` and owns its option members. The Attribute Code **size** filter
  input and the result-row cell share a label — open the editor via that row's **Edit** action,
  not the filter input or column header.
- A new Text Swatch option stores the requested label in both **Admin Description** and
  **Admin Swatch** on that existing attribute row. Both values are the requested Size label;
  the product's Color is not the Size attribute's Admin Swatch value.
- In the Size attribute editor, **Add Swatch** sits below the last **Manage Swatch** row and is
  usually under the fold — scroll the form, not the admin menu. It appends a blank option row.
  Fill that new row in the order **Admin Description**, then **Admin Swatch**, and finally use
  **Save Attribute**. Remaining in the editor without a save-success message is not persistence
  evidence. If the requested label is already a Manage Swatch value, skip **Add Swatch**.
- The global attribute option and an existing configurable parent's Configuration member are two
  separate durable resources. Adding a previously unavailable Size to an existing configurable
  product requires both persisted changes: first the two Text Swatch fields on the existing Size
  attribute, then one **Configurations** member on the existing parent. The option must be durable
  before the parent member can reference it.
