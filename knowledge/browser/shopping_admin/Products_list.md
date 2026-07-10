---
id: knowledge.browser.shopping_admin.products_list
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 在 Products 页面需要按 Name/Quantity/Color/Material/Size 查找产品、区分 Configurable 父商品与 Simple 变体、创建或编辑产品时查阅
when: 在 Products 页面需要按 Name/Quantity/Color/Material/Size 查找产品、区分 Configurable 父商品与 Simple 变体、创建或编辑产品时查阅
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 2
---
# Products list

All products in the catalog are accessible from the _Products_ page in the Admin, where you can create products and edit existing ones. For a multi-site installation, each website can offer a different selection of products for sale from the same catalog.

The _Products_ list includes all products in the catalog, indicates the websites where they are available, and if they are currently enabled for sale. In Adobe Commerce B2B installations with shared catalogs enabled, the grid includes a column that indicates which products have alternate discount pricing in a shared catalog.

You can browse through the list page by page, or search for specific products. Use the standard controls to sort and filter the list, and apply actions to selected products.

The list can contain both a **Configurable Product** parent and its **Simple Product** variations.
Mutations must use the Type column to select the record that owns the requested capability:
Configurations and aggregate product state belong to the configurable parent; a variation's Price,
Quantity, Size, and Color belong to the simple child. Each row's Action/Edit link is its stable
detail entry.

The Columns control can expose **Color**, but this grid does not expose Material or Size as optional
columns in this environment. Material/Size requests that are absent from the grid require the
relevant product detail; an empty child Material can require resolving the configurable parent by
base SKU and Type.

## Limit product display

To improve performance for large catalogs, it is recommended that you limit the number of products displayed in the grid. You can limit displayed product grids for:

- Products page
- Add Related/Up-Sell/Cross-Sell Products
- Add Products to Bundle Product
- Add Products to Group Product
- Create Order (Admin)

This configuration setting for the product display limitation is disabled by default. By enabling it, you can limit the number of products in the grid to a specific value. If it is enabled and the number of matching products for the grid display is greater than the record limit, a limited collection of records is returned. When the limit is reached, the total records found, number of selected records, and pagination elements do not appear in the grid header.

**NOTE:**
If you do not want your product grid to be limited, use filters more precisely to produce a collection that has fewer items than the number specified in the _Records Limit_ field.

**_To configure the product display limitation:_**

1. On the _Admin_ sidebar, go to **Stores** > _Settings_ > **Configuration**.

1. Expand **Advanced** and choose **Admin**.

1. Expand  the **Admin Grids** section and do the following:

   - Set **Limit Number of Products in Grid** to `Yes`.

   - (Optional) Enter a value in the **Records Limit** field to limit the number of products in the grid to a specific value. The default minimum value is `20000`.

1. When complete, click **Save Config**.

## Page controls

|Control|Description|
|--- |--- |
|Add Product|Initiates the process to create a new simple product. To choose a specific product type, click the down arrow. Options: Simple Product / Configurable Product / Grouped Product / Virtual Product / Bundle Product / Downloadable Product / Gift Card|
|Actions|Lists all actions that can be applied to selected products in the list. To apply an action to a product or group of products, select the checkbox in the first column of each product. Options: `Delete` / `Change Status` / `Update Attributes` / `Assign Inventory Source` / `Unassign Inventory Source` / `Transfer Inventory To Source`|
|Filters|Initiates a catalog search based on the current filters.|
|Default View|Indicates the current grid column layout. If there are saved grid column views, you can choose another.|
|Columns|Lists all actions that can be applied to selected products in the list. To apply an action to a product or group of products, select the checkbox in the first column of each product.|
|Search by keyword|The search box, in the top-left corner, is used to find products by keyword.|
|Edit|Opens the product in edit mode. You can accomplish the same thing by clicking anywhere on the row.|

## Default columns

|Column|Description|
|--- |--- |
|(Checkbox)|Selects multiple records to be subject to an action. The checkbox in the first column of each selected record is marked. Options:  **Select All** - Selects all records found that match the current filter settings.  **Select All on This Page** - Selects only the records found on the current page that match the filter settings.|
|ID|A unique, sequential number that is assigned when a new product is saved for the first time.|
|Thumbnail|Displays a thumbnail of the main product image.|
|Name|The product name.|
|Type|The product type.|
|Attribute Set|The name of the attribute set that is used as a template for the product.|
|SKU|The unique Stock Keeping Unit that is assigned to the product.|
|Price|The unit price of the product.|
|Quantity|The quantity that is in stock.|
|Salable Quantity|The sum of all available units of this product.|
|Visibility|Indicates where the product is visible in the catalog. Options: `Not Visible Individually` / `Catalog` / `Search` / `Catalog, Search`|
|Status|Indicates the status of the product. Options: `Enabled` and `Disabled`|
|Websites|Indicates the websites where the product is available.|
|Action|Opens the product in Edit mode.|
|Shared Catalog| (Available with Adobe Commerce B2B only) Indicates the shared catalogs that contain custom pricing for the product.|

## Other columns

|Column|Description|
|--- |--- |
|Short Description|Short description of the product.|
|Special Price From Date|The first date of the special price promotion.|
|Special Price To Date|The last date of the special price promotion.|
|Cost|The actual cost of the item.|
|Manufacturer|The manufacturer of the product.|
|Meta Keywords|Meta keywords for the product.|
|Color|The product color. **不在默认列里**：需先点 Columns 按钮，勾选 Color 后该列才出现在网格中。适合「找出颜色」类任务时提前启用。|
|Set Product as New from Date|The first date of the set product as a new promotion.|
|Set Product as New to Date|The last date of the set product as a new promotion.|
|Active From / To|The product start and end date.|
|Layout|The product layout.|
|Minimum Advertised Price|The minimum advertised price of the product.|
|Allow Gift Message|The gift message to customers who purchase a gift card.|
|Special Price|Special price for the product.|
|Weight|The product weight.|
|Meta Title|Meta title for the product.|
|Meta Description|The product metadata description.|
|Country of Manufacture|The country of manufacture.|
|New Theme|Applied custom theme to the product.|
|URL Key|The URL Key of the product.|
|Tax Class|The product tax class.|
|Allow Gift Message|Displays the availability of the gift message option for the product.|
