---
id: knowledge.browser.shopping_admin.create_a_product
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要创建产品、给 Configurable Product 添加 size/color/XXXL/XXS 变体组合，或理解产品类型时查阅本节
when: 当需要创建产品、给 Configurable Product 添加 size/color/XXXL/XXS 变体组合，或理解产品类型时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 2
---
# Create a product

Choosing a product type is one of the first things that you must do to create a product. If you are just beginning to construct your product catalog, you can create a few sample products to experiment with each product type. In addition to the basic product types, the term _complex product_ is sometimes used to refer to products with multiple options, such as a configurable product that is available in various colors and sizes.

**NOTE:**
For a deeper understanding, refer to catalog navigation, how to set up categories and attributes, and the catalog URL options that are available. After you understand these concepts, the most efficient way to add many products to the catalog is to import them from a CSV file.

## Product types

**Simple product** - A simple product is a physical item with a single SKU. Simple products have various pricing and of input controls which makes it possible to sell variations of the product. Simple products can be used in association with grouped, bundle, and configurable products.

**Configurable product** - A configurable product appears to be a single product with lists of options for each variation. However, each option represents a separate, simple product with a distinct SKU, which makes it possible to track inventory for each variation.

**Grouped product** - A grouped product presents multiple, standalone products as a group. You can offer variations of a single product, or group them for a promotion. The products can be purchased separately or as a group.

**Virtual products** - A virtual product is not a tangible product, and is typically used for products such as services, memberships, warranties, and subscriptions. Virtual products can be used in association with grouped and bundle products.

**Bundle product**  - A bundle product lets customers "build their own" from an assortment of options. The bundle could be a gift basket, computer, or anything else that can be customized. Each item in the bundle is a separate, standalone product.

**Downloadable product** - A digitally downloadable product consists of one or more files that are downloaded. The files can reside on your server or be provided as URLs to any other server.

**Gift card** - (Adobe Commerce only) There are three kinds of gift cards. _Virtual_ gift cards are sent by email. _Physical_ gift cards are shipped to the recipient. _Combined_ gift cards that are a combination of virtual and physical. Each has a unique code, which is redeemed during checkout. Gift cards can also be included in a grouped product.

## Product settings

The most frequently used product settings and attributes are displayed at the top of the page, followed by custom attributes. Any other product settings are in expandable sections at the bottom of the page.

|Setting|Description|
|--- |--- |
|Sources| (When Inventory Management is enabled) Lists the sources from which the product can be distributed.|
|Content|Used to enter and edit the main product description that appears on the storefront product page.|
|Configurations| Lists any existing variations of the product and can be used to generate variations for use with the Configurable product type.|
|Product Reviews|Lists all reviews that customers have submitted for the product.|
|Search Engine Optimization|Specifies the URL Key and metadata fields that are used by search engines to index the product.|
|Related Products, Up-Sells, and Cross-Sells|Used to set up simple promotional blocks on the storefront that present a selection of additional products that might be of interest to the customer.|
|Customizable Options|Adds customizable options to a product.|
|Product in Websites| Identifies each website where the product is available, according to the store hierarchy.|
|Design|Used to apply a different theme to the product page, change the column layout, determine where product options appear, and enter custom XML code.|
|Gift options|Used to enable or disable a gift message option during checkout at the product level.|
|Product In Shared Catalogs |  (Available with Adobe Commerce B2B only) Enables the ability to maintain shared catalogs with custom pricing for different companies.|
|Downloadable Information|Used to define the parameters for product download.|

## Configurable product variation dependencies

The **Configurations** collection belongs to the configurable parent product. Each generated
color/size combination is a variation represented by a separate simple product.
Product-name search can return both the parent and its simple variations, so use the product Type
to select the unique **Configurable Product** owner before editing Configurations.
After collecting the filtered product rows, verify that exactly one row matches both the product
identity and `Type=Configurable Product`. Return a detail URL only when that count is one; do not
hide zero or multiple owner candidates with an arbitrary first-row / `LIMIT 1` selection.

Configuration values come from the globally defined product attribute options. If a requested
Size or Color value does not exist yet, create and save that option under **Stores > Attributes >
Product** before generating the combination in the parent product. Saving an attribute option and
saving the parent product's Configurations collection are two independent persistent changes; the
attribute option must be durable before Configurations can consume it.
Only a value explicitly requested as new or missing creates this prerequisite mutation. Existing
Size or Color values that merely qualify the requested combinations are selected in the
Configurations wizard and must not be re-added to the global attribute option collection.

Treat these as ordered resource phases, not as pages to keep open for later. Complete and save the
global attribute-option mutation first. Only then locate and open the configurable parent product.
Do not open the parent before editing the attribute and do not rely on browser Back to recover an
earlier parent editor; that route can restore stale form state or the wrong owner.

Generating a requested color/size combination and saving the configurable parent are one
Configurations mutation boundary. In the DSL, express them as one action whose terminal state is
the saved Configurations collection containing that exact combination. Expanding Configurations,
starting the wizard, selecting attributes, generating rows, and clicking the final product Save are
runtime steps inside that action; do not split generation and Save into separate action statements.

The configuration wizard can open with the parent's existing Size and Color values already
selected. It generates the Cartesian product of every selected value, not merely the values clicked
during the current run. To add one requested combination, use **Deselect All** separately for each
attribute dimension, then select only the requested Size and Color. On **Step 4: Summary**, the
**New Product Review** table is the pending generation set: it must contain exactly one row for that
requested combination. If it contains any additional rows, go back and correct the selections;
do not click **Generate Products**. After generating the one row, save the configurable parent.
Rows returned to the parent Configurations matrix by **Generate Products** are not durable until
the parent Save. If that pending matrix contains multiple newly generated rows for a request that
authorizes one combination, reopen **Edit Configurations** and correct the generated set before
saving the parent; returning from the wizard alone is not commit authorization.

## Advanced pricing and inventory

To access the advanced pricing and inventory settings, click the link below **Price** and **Quantity**. For more information, see Managing Pricing and Inventory Management.
