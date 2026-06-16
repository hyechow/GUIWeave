---
when: 当需要生成或筛选订单、税务、发票、发货、退款、优惠券及PayPal结算等Sales reports时查阅本节
---
# Sales reports

The selection of sales reports includes Orders, Tax, Invoiced, Shipping, Refunds, Coupons, and PayPal Settlement.

## Report filters

You can generate a sales report for a whole website or for one store. The sales reports can be filtered by time interval, date, and status.

To filter a sales report, set the following options:

| Option | Description |
|--- |--- |
|Date Used|Sets the data to be used for the report.|
|Period|The period for which the data is used: Day/Month/Year.|
|From/To|Used to define search data by start and end date.|
|Order Status|Indicates the order status|
|Empty Rows|Indicates whether to add blank rows to the report.|

## Orders Report

The Orders Report includes the number of orders placed and canceled, with totals for sales, amounts invoiced, refunded, tax collected, shipping charged, and discounts.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Orders**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

## Tax Report

The Tax Report includes the tax rule applied, tax rate, number of orders, and amount of tax charged.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Tax**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

## Invoice Report

The Invoice Report includes the number of orders and invoices during the time period, with amounts invoiced, paid, and unpaid.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Invoiced**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

## Shipping Report

The Shipping Report includes the number of orders for the carrier or shipping method used, including amounts for total sales and total shipping.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Shipping**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

## Refunds Report

The Refunds Report includes the number of refunded orders, and total amount refunded online and offline.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Refunds**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

## Coupons Report

The Coupons Report includes each coupon code used during the specified time interval, related price rule, and number of times used, with totals and subtotals for sales and discounts.

1. On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Coupons**.

1. In the **Filter** section, select the reporting period options and order status used to populate the report.

1. Click **Show Report**.

For more information about using the Coupons Report to gather data for your promotion campaigns, see Coupons reporting in the _Merchandising and Promotions Guide_.

<!---  need coupon data  -->

## PayPal Settlement Reports

The [PayPal Settlement Reports] page includes the type of event, such as a debit card transaction, the start and finish dates, gross amount, and related fees. The report can be automatically updated with the most current data from PayPal. There are filtering options for date range, merchant account, transaction ID, invoice ID, or PayPal reference ID.

On the _Admin_ sidebar, go to **Reports** > _Sales_ > **PayPal Settlement**.

For more information about using the PayPal Settlement Reports to retrieve information about each PayPal transaction that affects the settlement of funds, see PayPal Settlement reports in the _Stores and Purchase Experience Guide_.

## Braintree Settlement Report

The Braintree Settlement Report can be filtered according to creation date, amount, status, transaction type, payment type, transaction ID, order ID, PayPal payment ID, type, merchant account ID, or settlement batch ID. The report contains the transaction ID, order ID, PayPal payment ID, type, creation date, amount, settlement code, status, settlement response text, reimbursement IDs, merchant account ID, settlement batch ID, and currency.

On the _Admin_ sidebar, go to **Reports** > _Sales_ > **Braintree Settlement**.

<!---  need a Braintree connection to update report screen -->

## Export reports

1. To export the report, select the file type: `Excel XML` or `CSV`

1. Click **Export**.

## Refresh statistics

To reduce the performance impact of generating sales reports, Commerce calculates and stores the required statistics for each report. Rather than recalculate the statistics every time a report is generated, the stored statistics are used, unless you refresh the statistics. To include the most recent data, the report statistics must be refreshed before a sales report is generated.

1. On the _Admin_ sidebar, go to **Reports** > _Statistics_ > **Refresh Statistics**.

1. In the list, select the checkbox for each report to be refreshed.

1. Set the **Actions** control to one of the following:

   - `Refresh Lifetime Statistics`
   - `Refresh Statistics for the Last Day`

1. Click **Submit**.