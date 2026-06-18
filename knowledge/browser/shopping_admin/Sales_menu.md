---
id: knowledge.browser.shopping_admin.sales_menu
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - planner
  - replanner
selector_when: 在需要查看订单工作流中的交易记录、管理报价订单发票发货退款退货账单协议或支付活动时查阅本节
when: 在需要查看订单工作流中的交易记录、管理报价订单发票发货退款退货账单协议或支付活动时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# Sales menu

The Sales menu lists transactions according to where they are in the order workflow. You might think of each of option as a different stage in the lifetime of an order.

### Adobe Commerce

### Adobe Commerce as a Cloud Service

## Display the Sales menu

On the _Admin_ sidebar, click **Sales**.

## Menu options

### Quotes

 (Available with Adobe Commerce B2B)

Authorized buyers can negotiate the price with the seller by sending a request from the shopping cart.

### Quote Templates

 (Available with Adobe Commerce B2B)

Allows buyers and sellers to streamline the quote process by creating reusable and customizable quote templates.

### Orders

When an order is placed, a sales order is created as a temporary record of the transaction. Payment has not been processed, and the order can still be canceled.

### Invoices

An invoice is a record of the receipt of payment for an order. Multiple invoices can be created for a single order, each with as many, or as few of the purchased products that you specify. Depending on the payment action, payment can be automatically captured when the invoice is generated.

### Shipments

A shipment is a record of the products in an order that have been shipped. As with invoices, multiple shipments can be associated with a single order, until all products in the order are shipped.

### Credit Memos

A credit memo is a document that shows the amount that is due the customer for a full or partial refund. The amount can be applied toward a purchase or refunded to the customer.

### Returns

 (Adobe Commerce only)

A returned merchandise authorization (RMA) can be granted to customers who request to return an item for replacement or refund. RMAs can be issued for Simple, Grouped, Configurable, and Bundle product types. However, RMAs are not available for virtual and downloadable products, or gift cards.

### Billing Agreements

A billing agreement is similar to a purchase order, except that it isn't limited to a single purchase. During checkout, the customer chooses Billing Agreement as the payment method. A billing agreement streamlines the checkout process because the customer doesn't have to enter payment information for each purchase.

### Transactions

The Transactions page lists all payment activity that has taken place between your store and all payment systems, and provides access to more detailed information.

### Braintree Virtual Terminal

On the Braintree Virtual Terminal page, an Admin user can accept the payment for the selected amount. To make the terminal feature available, a merchant should configure basic Braintree settings. Braintree offers a fully customizable checkout experience with fraud detection and PayPal integration.

### Archive

 (Adobe Commerce only)

(Archive option must be enabled) Archiving orders and other sales documents regularly improves performance and keeps your workspace free of unnecessary information.