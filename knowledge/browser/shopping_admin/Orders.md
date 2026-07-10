---
id: knowledge.browser.shopping_admin.orders
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要查看、创建或编辑订单，管理 Orders 网格布局和视图，或按订单历史统计 customer email(s)、completed/any-state orders、most/second/fifth number of orders、have N orders 等订单数聚合任务时查阅本节
when: 当需要查看、创建或编辑订单，管理 Orders 网格布局和视图，或按订单历史统计 customer email(s)、completed/any-state orders、most/second/fifth number of orders、have N orders 等订单数聚合任务时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 2
---
# Orders

The _Orders_ grid lists all current orders and tracks their progress and order status through the workflow. An easy way to understand the basic process is that an order becomes an invoice, and an invoice becomes a shipment. The grid represents the first stage of the process, and is where you can update existing orders and create orders.

Usually, orders are created when customers complete the checkout process from the storefront. However, if a customer needs assistance, you can also access their shopping cart or create an order either from the _Orders_ grid or directly from their customer account.

## Orders workspace

The Orders workspace lists all current orders, and gives you the ability to edit existing orders and create orders. Each row in the grid represents a customer order, and each column represents an attribute, or data field. Use the standard controls to sort and filter the list, find orders, and apply actions to selected orders. Use the tabs above the pagination controls to filter the list, change the default view, change and rearrange columns, and export data.

Planning note for Orders date filters: when a task requires filtering by
`Purchase Date`, write the page filter step with slash-form UI dates
(`MM/DD/YYYY` or `M/D/YYYY`). Never write ISO `YYYY-MM-DD` values in the visible
Orders filter step; ISO is only the provider/storage date shape after rows are
read. For example, January 1, 2023 should be written as `01/01/2023` (or
`1/1/2023`), and May 31, 2023 should be written as `05/31/2023` (or
`5/31/2023`).

For tasks that ask for customer email(s) by total order count across the entire
history, the Orders grid is the most reliable source of raw order rows: show
`Customer Email`, `Customer Name`, and `Status`, then count rows per email
yourself (do **not** export/download — the agent cannot read downloaded files
back). The Orders grid does not provide built-in aggregation/group by/customer-
count controls; it only supports filtering, sorting, pagination, and column
selection. Do not assume the Customers grid exposes a reliable `Total Orders`
column.

⚠️ **统计订单数前,判口径的关键 = intent 里有没有 "completed" 这个词:**
- **含 "completed"**(筛 Complete):"who completed the most/second/Nth number of
  orders" / "who completed N orders" / "completed orders" —— 按 **`Status = Complete`**
  计数。**第一步先 `Clear all` 清掉所有残留 `Active filters`**(残留 `Purchase Date`
  range 说明源已不是 entire history,必须删),再**只应用 `Status = Complete`**,采全量
  Complete 行。
- **字面 "any state" / 只说 "have N orders"**(不筛 Status):**所有状态**订单数,
  **不要**筛 `Status = Complete`。**第一步先 `Clear all` 清掉所有残留 `Active filters`**
  (含残留的 `Status: Complete`,绝不能沿用),再不加状态筛选地采全量。

For monthly completed-order counts over a `Purchase Date` range, prefer the page
filters before reading rows: in **Sales > Orders**, open **Filters**, set
`Status = Complete`, set `Purchase Date` **from**/**to** (US-style `MM/DD/YYYY`,
not ISO — see the date-filter note above), click **Apply Filters**, then read the
complete grid and aggregate by month. Data-shape note: the Magento grid provider
exposes `created_at` as `YYYY-MM-DD HH:MM:SS` and `status` as lowercase values
such as `complete`, while the visible DOM column is labeled `Purchase Date` and
displays values like `Feb 3, 2023 6:08:03 PM`.

For tasks phrased as "customer email(s) who completed the most/second/fifth
number of orders" or "customer email(s) who have N orders in any state", use this
Orders grid as the primary UI source, not Customers grid and not Customer Reports.
The reason to prefer the Orders grid is field coverage: it provides both the
filter field (`Status`) and the final grouping/output field (`Customer Email`),
while Customer Reports usually show customer names and interval aggregates, and
the Customers grid does not reliably expose total order counts. Apply the status
constraint per the ⚠️ 口径 rule above (intent contains "completed" → filter
`Status = Complete`; literal "any state" → no filter, `Clear all` first). Then
aggregate the complete raw rows by `Customer Email`, counting one per order. Two
business semantics: for most / second / fifth, tied emails share a rank — return
**all** emails tied at the requested rank; "have N orders" means a count of
**exactly N**, not N-or-more.

The `Status` filter accepts one positive status at a time. A request for
**non-cancelled** orders therefore cannot be represented by selecting another positive status:
clear the Status filter, collect rows with `Status`, `Purchase Date`, and `Grand Total
(Purchased)`, and exclude `Canceled` during structured analysis. For recent/oldest payment
questions, `Purchase Date` is the ordering field and `Grand Total (Purchased)` is the payment
measure; the grid's current/default row order is not a recency guarantee.

Order-number lookup and keyword lookup are different capabilities. The order number shown as
`#304` is stored as a zero-padded increment ID such as `000000304`; use the Filters panel's **ID**
field with the numeric value `304`. The top **Search by keyword** box is intended for text lookup
and does not reliably match either `#304` or `304`. Customer lookup may use the top keyword box or
the explicit `Bill-to Name`, `Ship-to Name`, or `Customer Email` filters.

Each order row provides its `Purchase Date` and a detail entry. The detail page's **Items Ordered**
table is the source for line-level Product and Price values. Product cells can include a `SKU:`
suffix, and subtotal/summary rows can have no Price; consumers of this table should treat only rows
with a line-item Price as products and remove the SKU suffix when a plain product name is requested.

If downloads/exports are not allowed, collect the raw rows by combining grid
pagination with within-page vertical scrolling. This is not infinite-scroll
loading: the pager still controls which result page is loaded, but a visual agent
can only see the rows currently in the viewport, so it must scroll within each
loaded page and read rows in chunks. Use a page size that the agent can reliably
scan (`20`, `30`, or `50` can be safer for visual collection than `200`), collect
every visible chunk on each page, then use the pager's next page control until
all `totalRecords` have been covered. Large Orders grids can span more rows than
one viewport/page chunk, so order-count tasks require multi-chunk collection
before counting by `Customer Email`.

### Grid layout

The selection of columns and their order in the grid can be changed according to your preference. The new layout can be saved as a grid _view_. By default, only nine of 20 available columns are included in the grid.

#### Change the column selection

In the upper-right corner, click the _Columns_ (  ) control and do the following:

- Select the checkbox of any column that you want to add to the grid.
- Clear the checkbox of any column that you want to remove from the grid.

#### Reset the column selection

1. Click the _Columns_ (  ) control.

1. To reset the grid columns, click **Reset**.

   The grid layout changes to display only default columns.

#### Move a column

1. Click and hold the header of the column.

1. Drag the column to the new position and release.

#### Save a grid view

1. Click the **View** (  ) control.

1. Click **Save Current View**.

1. Enter a **name** for the view.

1. To save all changes, click the arrow (  ).

    The name of the view now appears as the current view.

#### Change the view

Click the **View** (  ) control. Then, do one of the following:

- To use a different view, click the name of the view.

- To change the name of a view, click the _Edit_ (  ) icon and update the name.

### Workspace controls

|Control|Description|
|--- |--- |
|Create New Order|Creates an order. See Creating an Order for more information.|
|Go to Archive|Displays the list of archived orders.|
|Search|Initiates a search for orders based on the current filters.|
|Filters|Defines a set of search parameters used to filter the records that appear in the grid.|
|Default View|Determines the default column layout of the grid.|
|Columns|Determines the selection of columns and their order in the grid. The column layout can be changed and saved as a _view_. By default, only some of the columns are included in the grid.|
|Export|Exports the selected records as a CSV or Excel XML file.|

### Actions

To apply an action to specific orders, select the checkbox in the first column of each order. To select or deselect all orders, use the control at the top of the column.

|Control|Description|
|--- |--- |
|Actions|Lists all actions that can be applied to selected orders. To apply an action to an order or group of orders, select the checkbox in the first column of each order.  Order actions: `Cancel` / `Hold` / `Unhold` / `Print Invoices` / `Print Packing Slips` / `Print Credit Memos` / `Print All` / `Print Shipping Labels` / `Move to Archive`  (Adobe Commerce only)|
|Mass Actions|Can be used to select multiple records as the target of action. Select the checkbox in the first column of each record that is subject to the action. Options: `Select All` / `Unselect All` / `Select Visible` / `Unselect Visible`|
|Submit|Applies the current action to the selected order records.|
|Edit|Opens the order in edit mode.|

### Column descriptions

|Column|Description|
|--- |--- |
|Select|Select the checkboxes for the quotes to be subject to an action, or use the selection control in the column header. Options: Select All / Deselect All|
|ID|A unique, sequential number that is assigned when a new order is saved for the first time.|
|Purchase Point|Identifies the store view where the order was placed.|
|Purchase Date|The date and time when the order was placed. It is always displayed according to the default time zone.|
|Bill-to Name|The name of the person who is responsible to pay for the order.|
|Ship-to Name|The name of the person to whom the order is to be shipped.|
|Grand Total (Base)|The grand total of the order.|
|Grand Total (Purchased)|The grand total of products purchased in the order.|
|Status|The current order status.|
|Action|_View_ opens the order in edit mode.|
|Allocated sources| The sources allocated to that specific order.|

Additional columns available:

|Column|Description|
|--- |--- |
|Billing Address|The billing address of the customer who placed the order.|
|Shipping Address|The address where the order is to be shipped.|
|Shipping Information|The method that is to be used to ship the order.|
|Customer Email|The email address of the person who placed the order.|
|Customer Group|The customer group to which the person who placed the order is assigned.|
|Subtotal|The order subtotal, without shipping and handling, and tax.|
|Shipping and Handling|The amount charged for shipping and handling.|
|Customer Name|The first and last name of the customer who placed the order.|
|Payment Method|The method of payment to be used for the order.|
|Total Refunded|Any amount from the order that is to be refunded to the customer.|
|Refunded to Store Credit| (Adobe Commerce only) Any amount from the order that is to be refunded to the customer's store credit.|
|Company Name| (Available with Adobe Commerce B2B) The name of the company who placed the order.|

## Order search

The Search box in the upper left of the Orders grid can be used to find specific orders by keyword, or by filtering the order records in the grid.

For customer-name order lookup, do not rely on a generic `Customer Name`
filter. In this Magento Orders grid, the filter panel exposes `Bill-to Name`,
`Ship-to Name`, and `Customer Email`, plus the top `Search by keyword` box.
`Customer Name` can appear as an optional column in the Columns menu, but that
does not make it a usable text filter. To find a customer's pending/latest
order, use the top keyword search with the full name or fallback keyword, or
filter explicitly by `Bill-to Name` / `Ship-to Name`; then add `Status=Pending`
and sort/collect by `Purchase Date`.

When using the top `Search by keyword` box, submit that keyword with Enter or
the magnifying-glass button inside the search box. The **Filters** panel and its
**Apply Filters** button only apply the expanded column-filter fields; they do
not submit the top keyword search box.

### Search for a match

1. Enter a search term into the page search box.

1. To display the results, press Enter or click _Search_ (  ) inside that box.

### Filter the search

1. To display the selection of search filters, click the _Filters_ (  ) tab.

1. Complete as many of the filters as you want to describe the orders that you want to find.

1. Click **Apply Filters** to display the results.

### Search filters

|Filter|Description|
|--- |--- |
|Purchase Date|Filters the search based on the date purchased. To find orders within a range of dates, enter both the **from** and **to** dates.|
|ID|Filters the search based on order ID.|
|Grand Total (Base)|Filters the search based on the Grand Total of each order, which includes any credits applied to the order.|
|Grand Total (Purchased)|Filters the search based on Grand Total of items purchased in each order.|
|Bill-to Name|Filters the search according to the name of the person who is responsible to pay for the order.|
|Ship-to Name|Filters the search according to name of the person to whom each order is shipped .|
|Purchase Point|Filters the search by website, store, or store view where the order was placed.|
|Status|Filters the search based on order status. Options: `Canceled` / `Closed` / `Complete` / `Suspected Fraud` / `On Hold` / `Payment Review` / `PayPal Canceled Reversal` /` PayPal Reversed` /` Pending` / `Pending Payment` / `Pending PayPal` / `Processing`|
|Braintree Transaction Source|Filters the search based on transaction source.|

### Search tools

|Tool|Description|
|--- |--- |
|Apply Filters|Applies all filters to the search results.|
|Cancel|Cancels the current search.|
|Clear All|Clears all search filters.|
