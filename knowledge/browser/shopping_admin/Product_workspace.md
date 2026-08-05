---
id: knowledge.browser.shopping_admin.product_workspace
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要 update/edit existing catalog 的 Configurations、Price、Quantity、Stock、Material、Short Description/Description 时查阅本节
when: 当需要 update/edit existing catalog 的 Configurations、Price、Quantity、Stock、Material、Short Description/Description 时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 8
---
# Product workspace

The product workspace is basically the same for all product types, although the selection of fields changes depending on the attribute set that is used. The product attributes are at the top of the form, followed by expandable sections of product information. When a new product is saved for the first time, the _Store View_ chooser appears at the upper left of the form.

## Enable Product setting

The online status of the product is indicated by the switch at the top of the form. To change the online status, set the **Enable Product** switch to `Yes` or `No`.

| Control | Description |
|-------- | ----------- |
|  | Indicates that the product is online. |
|  | Indicates that the product is offline. |

## Attribute set

The name of the attribute set appears in the upper-left corner and determines the fields that appear in the product record. To choose a different attribute set, click the down arrow next to the default attribute set name.

## Expand/collapse

To expand or collapse a section, click either the expand  or collapse  icon.

## Save menu

The _Save_ menu includes several options that let you save and continue, save and create a product, save and duplicate the product, or save and close.

|Command|Description|
|--- |--- |
|Save|Save the current product and continue working.|
|Save & New|Save and close the current product, and begin a new product based on the same product type and template.|
|Save & Duplicate|Save and close the current product, and open a new duplicate copy.|
|Save & Close|Save the current product and return to the _Products_ workspace.|

## Content, variants, and inventory ownership

The expandable **Content** section contains two different text resources: **Short Description**
and the main **Description**. They are not aliases. Product-summary tasks that target the short
catalog description must write Short Description; changing the main Description does not update it.
In this admin's product data contract, an unqualified request to update the catalog-facing
"product description" refers to **Short Description**. The main **Description** is the long-form
content resource and should only be selected when the request explicitly asks for the main, full,
or long description.
When a product-name lookup returns a configurable family, the catalog-facing Short Description is
owned by its **Configurable Product** parent rather than by an arbitrary Simple variation. Candidate
rows therefore need the Type discriminator, and selecting the write target must actually constrain
that discriminator instead of taking an unspecified first match.

A configurable parent owns the **Configurations** collection, while every generated color/size
combination is a separate **Simple Product** with its own SKU and Price. Changing the price of a
specific color/size combination therefore changes that Simple Product's current Price, not the
parent's Price and not an attribute selector on another variation. Percentage changes are derived
from the variation's live current Price.

Adding one requested color/size combination and saving the configurable parent form one durable
Configurations mutation. The durable state is the saved **Configurations** collection containing
the exact Size/Color combination. The section, wizard, and generated row are editor surfaces of
that same parent resource rather than independently persisted resources.

Size and Color belong to the variation. Material can be inherited/owned by the configurable parent
and may be empty on a Simple child. Variation SKUs commonly append size and color segments to the
parent SKU; the parent record is identified by the base SKU together with
`Type=Configurable Product`. For a Material query, an empty child value must fall back to that
verified parent; filtering out the empty child without reading the parent is incorrect.

**Stock Status** (`In Stock` / `Out of Stock`) and **Quantity** are distinct resources. Marking a
product out of stock changes Stock Status; setting Quantity to zero is not equivalent. For a
configurable product, the parent workspace owns the aggregate Stock Status for the product, while
individual Simple products own their quantities and variation prices.
Consequently, a request to mark all of one configurable product out of stock targets that single
parent-owned aggregate state. It does not mean applying the same mutation independently to every
Simple variation. Per-variation iteration is appropriate for fields owned by variations, such as
their Price or Quantity, not for this parent aggregate Stock Status.

## Default field values

To save time when creating products, the default value of several product fields references values from another field. You can either accept the default value or enter another. The following fields have automatically generated default values:

|Field |Default |
|----- |------- |
|SKU|Based on product name. |
|Meta Title|Based on product name. |
|Meta Keywords|Based on product name. |
|Meta Description|Based on product name and description. |

The placeholders that represent the value of another field are enclosed in double-curly braces. Any attribute code that is included in the product attribute set can be used as a placeholder.

For a detailed list of these settings, see Product Fields Auto-Generation in the _Configuration Reference_.

### Edit the placeholder value

1. On the _Admin_ sidebar, go to **Stores** > _Settings_ > **Configuration**.

1. In the left panel, expand **Catalog** and choose **Catalog** underneath.

1. Expand  the **Product Fields Auto-Generation** section and make any needed changes to the placeholder values.

   For example, if there is a specific keyword that you want to include for every product or a phrase that you want to include in every meta description, enter the value directly into the appropriate field.

   **NOTE:**
   >
   >If you want to keep the existing placeholder values, preserve the double curly braces that enclose each markup tag.

1. When complete, click **Save Config**.

### Common placeholders

- `{{color}}`
- `{{country_of_manufacture}}`
- `{{description}}`
- `{{gender}}`
- `{{material}}`
- `{{name}}`
- `{{short_description}}`
- `{{size}}`
- `{{sku}}`
