---
id: knowledge.browser.shopping_admin.product_workspace_interface
source_type: knowledge_interface
platform: browser
app: shopping_admin
scope:
  - orchestrator
selector_when: product products catalog inventory quantity price stock size sizes color colors configurations short description keyword new combination
source: manual_curated
confidence: high
ttl: session
---
# Existing product interface

- The **Products** grid has the stable same-origin route `/admin/catalog/product/`. When the
  direct URL capability is available, prefer this sourced route over reopening the Catalog menu.
- Locate rows with the global **Search by keyword** input above the table, not a table-header
  Name box. Type the query and submit (Enter or **Search**) in the same step. A leftover
  placeholder is not an empty field until that submit runs.
- Keyword search is a word query, not a substring. A color or size that only appears after a
  hyphen in `{base}-{Size}-{Color}` may return 0 rows — drop the glued token and keep the
  product-line words, then open only simples whose Name contains the requested color or size.
  Informal synonyms that are not whole words on a visible Name also return 0 rows.
- A further **Name** restriction belongs in the **Filters** side panel (then **Apply Filters**),
  not the table-header Name box. Magento persists leftover **Active filters** via `ui_bookmark`;
  **Clear all** before a new search.
- A configurable parent has Type `Configurable Product` and owns **Short Description** and
  **Configurations**. A simple variation has Type `Simple Product` and owns **Price**,
  **Quantity**, and **Stock Status**. Parent **Stock Status** is independent of its simple
  children. **Quantity** and **Price** are absolute per-simple fields: typing a number
  replaces the current value. Actions → Update Attributes does not add or apply a percent.
- After a keyword search, Name alone can also match simple variations; Type distinguishes
  the configurable parent.
- **Short Description** is the WYSIWYG under the product form's collapsible **Content**
  heading. It is not the top admin **CONTENT** menu (that opens CMS Pages). Content sits
  immediately above **Configurations**; a large wheel overshoots it.
- **Edit Configurations** sits immediately above the configurations table. Option dropdowns
  above that table (Activity, Style, Material, Color) are always-open in-page multi-selects —
  do not tap them to dismiss. The per-row **Select** menu and page-header **Add Attribute**
  do not add a Configurations member. Size attribute options (Stores → Attributes) are a
  separate resource from a parent's Configurations members.
- The **Edit Configurations** wizard owns a new combination. In Attribute Values, **Deselect
  All** in every participating attribute (inherited checks are not the requested set), select
  only the requested values, then **Generate Products** and save the parent. On **Summary**,
  New Product Review rows must equal the Cartesian product of only those requested values.
  If Summary shows a superset, the offending inherited check is usually **offscreen** in a long
  checkbox list (the Runtime's authoritative choice state lists checked values outside the
  viewport): do not re-verify visible boxes — go Back and apply **Deselect All** in **every**
  attribute section, then re-select only the requested values. Worker specs for this flow must
  phrase the selection contract as performed actions, not verified state: "Deselect All was
  clicked in every attribute section during this pass, then exactly {requested values} were
  checked" — visible boxes are not evidence that offscreen values are clear. A multi-section
  selection mutation should be decomposed into **one operator per attribute section** whose
  success criteria require (a) the section's Deselect All click and (b) the Runtime's
  authoritative choice state showing zero checked values in that section outside the
  requested set; a final operator verifies Summary rows equal the requested Cartesian product
  before Generate Products, and must `fail` on any superset instead of rationalizing extra
  rows as pre-existing. **Generate Products only stages the matrix inside the open editor**:
  the Current Variations grid re-renders in-page before anything is persisted, so new rows
  there are staging evidence, not persistence. The mutation exists only after the parent
  editor's **Save** fires its POST and the page reloads with the "You saved the product"
  message — never complete after Generate Products alone, and never treat the in-page grid
  as the saved state.
- Supplying one requested Configurations member appends that member and preserves unrelated
  existing members. Adding a Size to all existing Color variants is one mutation on the
  configurable parent, not a per-simple edit.
