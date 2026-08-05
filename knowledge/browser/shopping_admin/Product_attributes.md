---
id: knowledge.browser.shopping_admin.product_attributes
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 在 Stores > Attributes 中查找或编辑既有 catalog attribute、添加 new option、XXXL/XXS/swatch value 时查阅本节
when: 在 Stores > Attributes 中查找或编辑既有 catalog attribute、添加 new option、XXXL/XXS/swatch value 时查阅本节
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 12
---
# Product attributes

The **Product Attributes** grid manages attribute definitions. Existing definitions are located by
their **Attribute Code** or **Default Label**, then opened for editing. The grid is paginated and
has column filters; locating a named attribute should use its filter first, or paginate when no
filter is available. Scrolling only reveals the remaining rows on the current page and never moves
to the next grid page.

`Size` is an existing product attribute whose attribute code is `size`. Adding a new size such as
`XXXL` means:

1. Filter **Attribute Code** by `size` and submit **Search**.
2. Open the existing `size` row.
3. In that attribute's Options/Values or swatch-options area, ensure an option labeled `XXXL`
   exists; add it only when absent.
4. Save the existing attribute and verify that its option collection contains `XXXL`.

For an attribute whose input type is **Text Swatch**, one option row contains multiple related
fields. The swatch value and the displayed option/store-view label are not interchangeable. A new
row is complete only when its **Admin swatch value** contains the requested value (and the label
fields required by the form are populated). A row where `XXXL` appears only in a neighboring text
label while the swatch field still shows the `Swatch` placeholder is incomplete: fill the swatch
field before saving. Judge this from the row/field association in the DOM control inventory, not
from the mere presence of `XXXL` somewhere in the screenshot.

The browser control inventory exposes the two required admin-side semantics as **Admin Swatch**
and **Admin Description** (the row association is authoritative). For a requested value `V`, both
fields in the same option row must equal `V`. **Save Attribute** only persists the row; it is not
one of the business fields that defines whether the option member is complete.

After **Add Swatch** creates a blank row, fill **Admin Description first**, then fill **Admin
Swatch**, using the requested value `V` in both. Admin Description is required on this form: leaving
it empty prevents **Save Attribute** from persisting the new option, even when the swatch field
already contains `V`. When the executor performs one field write per turn, use this order:
`Admin Description -> Admin Swatch -> Save Attribute`. Do not treat a dispatched Save as success
when the editor remains open without a success message; re-check that both fields in the new row
are populated.

There are two visually similar Description inputs in each row. **Admin Description** is the left
Description input whose DOM group field is `Admin`; this is the required field to fill first.
**Default Store View Description** is the right Description input whose DOM group field is
`Default Store View`; filling only this right-hand field does not complete the option and must not
be accepted as evidence that `V` was added. The business target is the pair `Admin Description=V`
and `Admin Swatch=V`; **Options/Values** is only their container and **Save Attribute** is only the
persistence command, not either business target.

**Add New Attribute** creates a separate attribute definition. It must not be used to add an option
to the existing Size attribute. A task that asks for a new Size/Color value changes the existing
attribute's option collection; it does not create another Size/Color attribute definition.

The existing attribute row and its option member are distinct resources. A persisted option is
identified by both its owner (`Attribute Code=size`) and its member fields. For a value `V`, those
member fields are `Admin Description=V` and `Admin Swatch=V`; Options/Values is their container,
and Save Attribute is the persistence command rather than a business field.
