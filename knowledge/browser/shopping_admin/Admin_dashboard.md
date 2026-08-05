---
id: knowledge.browser.shopping_admin.admin_dashboard
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要使用 Admin dashboard 的 sales overview、search term statistics、Top Search Terms、chart 或 startup page 能力时查阅本节
when: 当需要使用 Admin dashboard 的 sales overview、search term statistics、Top Search Terms、chart 或 startup page 能力时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 3
---
# Admin dashboard

The dashboard is usually the first page that appears when you log in to the _Admin_ and can provide a real-time overview of sales and customer activity. Dashboard data provides a snapshot of lifetime sales, average order amount, recent orders, and search terms. The chart shows completed orders and amounts for the selected date range, and can be generated from either dynamic, real-time data, or historical aggregated data. The tabs at the bottom provide quick reports of your best-selling products, most viewed products, new customers, and customers who have purchased the most.

If you have a significant amount of data to process, the chart can be turned off to improve performance. The dashboard in the following example is configured to use real-time data and shows completed orders by the hour for the last 24 hours. The chart is updated for each completed order.

Advanced Reporting displays a personalized dashboard based on your product, order, and customer data.

## Configure the dashboard

1. On the _Admin_ sidebar, go to **Stores** > _Settings_ > **Configuration** and complete any of the following settings.

1. When the configuration is complete, click **Save Config**.

1. After saving the changes, click **Cache Management** and refresh every invalid cache.

### Enable charts

If you have a large amount of data to process, you can turn off the display of the chart to improve performance. When not enabled, the message "No Data Found" appears in place of the chart, although the summary totals below are still generated.

1. In the left navigation panel under **Advanced**, choose **Admin**.

1. If necessary, expand the **Dashboard** section.

1. To change the default value, clear the **Use system value** checkbox.

1. Set **Enable Charts** to `Yes`.

For more information about the Admin configuration options, see the Configuration Reference Guide.

### Change the startup page

The dashboard is the default startup page for the Admin, although you can configure a different startup page.

1. If you do not already have the Admin configuration options open, choose **Admin** under _Advanced_ in the left navigation panel.

1. Click to expand the **Startup Page** section.

1. Clear the **Use system value** checkbox and choose the **Startup Page** that you want to appear when you log in to the Admin.

### Choose the starting dates

1. In the left navigation panel under **General**, choose **Reports**.

1. On the page, expand the **Dashboard** section.

1. Clear the **Use system value** checkboxes for the date settings and do the following:

   - Set **Year-To-Date Starts** to the **Month** and **Day**.

   - Set **Current Month Starts** to the **Day**.

For more information about the Reports configuration options, see the _Configuration Reference Guide_.

### Configure the data source

The dashboard chart can be generated in real time or by using historical, aggregated data. If performance is an issue, you can speed up things by using aggregated data.

1. In the left navigation panel, click to expand **Sales** and choose **Sales** underneath.

1. On the page, expand the **Dashboard** section.

1. Clear the **Use system value** checkbox and set **Use Aggregated Data** to one of the following:

   - For historical, aggregated data, choose `Yes`.
   - For real-time data, choose `No`.

## Chart sections

|Section|Description|
|--- |--- |
|Orders|This tab displays a real-time chart of all completed orders for the current store view and specified time period.|
|Amounts|This tab displays a real-time chart of all completed order amounts for the current store view and specified time period.|
|Time Range|Determines the data that is represented in the chart and summary totals below. Options: `Last 7 Days` / `Current Month` / `YTD` / `2YTD`|
|Summary Totals|The revenue, tax, shipping, and quantity totals below the chart are based on the chart data and current time range setting.|

## Snapshot data

|Section|Description|
|--- |--- |
|Lifetime Sales|The aggregated total sales during the lifetime of the store.|
|Average Order|The average order amount during the lifetime of the store.|
|Last Orders| A summary of the last five placed orders.|
|Last Search Terms|The last five search terms.|
|Top Search Terms|The five most commonly used search terms.|

### Snapshot collection interfaces

- **Top Search Terms** is a collection source with fields `Search Term`, `Results`, and `Uses`.
  `Uses` is the usage count for ranking most-used terms; `Search Term` is the returned term text.

## Report tabs

|Section|Description|
|--- |--- |
|Bestsellers|The five best-selling products during the specified time period.|
|Most Viewed Products|The five products viewed the most during the specified time period.|
|New Customers|The most recent five customers who registered for an account during the specified time period.|
|Customers|The last five customers with an order that completed processing during the specified time period.|

## Dashboard buttons

|Button|Description|
|--- |--- |
|Reload Data|Refreshes dashboard data.|
|Go to Advanced Reporting|Displays a personalized dashboard of dynamic charts and reports based on your product, order, and customer data. For more extensive analysis, see Advanced Reporting.|
