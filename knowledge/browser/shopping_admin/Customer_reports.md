---
id: knowledge.browser.shopping_admin.customer_reports
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - planner
  - replanner
selector_when: 当需要查看指定时间范围内的客户活动、订单总额或订单数量报告时查阅本节
when: 当需要查看指定时间范围内的客户活动、订单总额或订单数量报告时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# Customer reports

Customer reports provide insight into customer activity during a specified time period or date range.

Do not use Customer Reports as the primary source for tasks that ask for
`customer email(s)` by total order count across the entire history, such as "most
number of orders", "second most number of orders", or "have N orders". These
tasks need the final output field `Customer Email` and often need exact
group/count/rank/tie handling. Prefer **Sales > Orders** and aggregate complete
Orders grid/export rows by `Customer Email`.

Customer reports can show **No records found** before a date range is applied.
When using these reports, set the intended **From** / **To** range and click
**Refresh** before treating an empty report as evidence that no matching
customer/order data exists.

There is no separate "aggregate" button on these report pages. The report is
aggregated automatically only after **Refresh**, and `Show By` controls the time
bucket (`Day` / `Month` / `Year`). Result rows are grouped by **Interval +
Customer**, so a customer can appear in multiple rows when the range spans
multiple intervals. For true "entire history per customer" counts, sum the rows
per customer/email yourself or use the Orders grid/export and count rows per
`Customer Email`.

## Order Total Report

The Order Total Report shows customer orders for a specified time interval or date range. The report includes the number of orders per customer, average order amount, and total amount.

On the _Admin_ sidebar, go to **Reports** > _Customers_ > **Order Total**.

### Workspace controls

|Control|Description|
|--- |--- |
|From / To| Used to define a search for the orders based on the start and end date.|
|Show By|Defines the granularity of the order record splitting. Options: `Month` / `Day` / `Year` |
|Refresh|Updates the grid to the specified filters.|
|Export|Exports the selected records as a CSV or Excel XML file.|
|Scope| Used to set the site or store for which the report is generated.|

### Column descriptions

|Column|Description|
|--- |--- |
|Interval|The order total interval, by `Month` / `Day` / `Year`.|
|Customer|The name of the customer who placed the orders.|
|Orders|The number of orders for the specified interval.|
|Average|Average order amount. This amount is always calculated for product prices **excluding tax** even if catalog product prices, order subtotal and order total include tax. As a result, the amount shown in the report is different than the amount shown in order details in cases where order totals include tax.|
|Total|The sum of all orders for the period. This amount is always calculated for product prices **excluding tax** even if catalog product prices, order subtotal and order total include tax. As a result, the total shown in the report is different than the amount shown in order details in cases where order totals include tax.|

## Order Count Report

The Order Count Report shows the number of orders per customer for a specified time interval or date range. The report includes the number of orders per customer, average order amount, and total amount.

On the _Admin_ sidebar, go to **Reports** > _Customers_ > **Order Count**.

### Workspace controls

|Control|Description|
|--- |--- |
|From / To| Used to define a search for the orders based on the start and end date.|
|Show By|Defines the granularity of the order record splitting. Options: `Month` / `Day` / `Year` |
|Refresh|Updates the grid to the specified filters.|
|Export|Exports the selected records as a CSV or Excel XML file.|
|Scope| Used to set the site or store for which the report is generated.|

### Column descriptions

|Column|Description|
|--- |--- |
|Interval|The order count interval, by `Month` / `Day` / `Year`.|
|Customer|The customer who placed the order.|
|Orders|The number of orders for the specified interval.|
|Average|Average order amount. This amount is always calculated for product prices **excluding tax** even if catalog product prices, order subtotal and order total include tax. As a result, the amount shown in the report is different than the amount shown in order details in cases where order totals include tax.|
|Total|The sum of all orders for the period. This amount is always calculated for product prices **excluding tax** even if catalog product prices, order subtotal and order total include tax. As a result, the total shown in the report is different than the amount shown in order details in cases where order totals include tas.|

## New Accounts Report

The New Accounts Report shows the number of new customer accounts opened during a specified time interval or date range.

On the _Admin_ sidebar, go to **Reports** > _Customers_ > **New**.

### Workspace controls

|Control|Description|
|--- |--- |
|From / To|Used to define a search for the new accounts based on the start and end date.|
|Show By|Defines the granularity of the order record splitting. Options: Month / Day / Year |
|Refresh|Updates the grid to the specified filters.|
|Export|Exports the selected records as a CSV or Excel XML file.|
|Scope|Used to set the site or store for which the report is generated.|

### Column descriptions

|Column|Description|
|--- |--- |
|Interval|New account creation interval, by Month / Day / Year.|
|New Accounts|The number of new accounts created in a certain interval.|

## Customer Wish List Report

 (Adobe Commerce only)

The Customer Wish List Report provides information about customer wish lists.

On the _Admin_ sidebar, go to **Reports** > _Customers_ > **Wish Lists**.

### Workspace controls

|Control|Description|
|--- |--- |
|Scope|Used to set the site or store for which the report is generated.|
|Search| Initiates a search by the specified parameters.|
|Reset Filter| Initiates a reset of all search parameters.|
|Per Page| Sets the number of records displayed in a single page. |
|Export|Exports the selected records as a CSV or Excel XML file.|
|From / To|Used to define a search for the wish lists based on the start and end date.|
|Wishlist| Initiates a wish list search by name.|
|Status| The status of the wish list. Options: `Private` / `Public` |
|Comment| Initiates a search by text in the wish list comments.|

### Column descriptions

|Column|Description|
|--- |--- |
|Added| Date the wish list was created.|
|Customer| First and last name of the customer who created the wish list.|
|Wishlist| Name of the wish list.|
|Status| The status of the wish list. Options: `Private` / `Public` |
|Product| Name of the product added to the wish list.|
|SKU| SKU of the product added to the wish list.|
|Comment| The comment text that was entered when the wish list was created.|

## Customer Segment Report

 (Adobe Commerce only)

The Customer Segment Report provides information about the number of customers in each segment.

On the _Admin_ sidebar, go to **Reports** > _Customers_ > **Segments**.

### Workspace controls

|Control|Description|
|--- |--- |
|Search| Initiates a search by the specified parameters.|
|Reset Filter| Initiates a reset of all search parameters.|
|Action| Initiates the display of segments by parameters. Options: `Action` / `View Combined Report`|
|Per Page| Sets the number of records displayed in a single page.|

### Column descriptions

|Column|Description|
|--- |--- |
|ID|A unique numeric identifier that is assigned to each segment.|
|Segment|Segment name.|
|Status|Segment status. Options: `Active` / `Inactive`|
|Website|Website to which the segment is assigned.|
|Customers|Number of customers assigned to the segment.|
