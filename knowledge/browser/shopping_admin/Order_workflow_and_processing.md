---
id: knowledge.browser.shopping_admin.order_workflow_and_processing
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要查看订单 Items Ordered 商品/价格、在 pending order 发送 Comment/Notify message、处理订单状态或待处理订单时查阅本节
when: 当需要查看订单 Items Ordered 商品/价格、在 pending order 发送 Comment/Notify message、处理订单状态或待处理订单时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 6
---
# Order workflow and processing

When a customer places an order, a sales order is created as a temporary record of the transaction. In the Orders grid, sales orders initially have a status of "Pending," and can be canceled at any time until the payment is processed. After payment is confirmed, the order can be invoiced and shipped.

**Step 1: Place Order** - The checkout process begins when the shopper clicks **Go to Checkout** on the shopping cart page or reorders directly from their customer account.

**Step 2: Order Pending** - The initial sales order status is `Pending`. In this state, the payment has not been processed and the order can still be edited or canceled. This state occurs when the payment method is configured for authorization mode.

**Step 3: Receive Payment** - The order status changes to `Processing` when payment is received or authorized. Depending on the payment method, you might receive notification when the transaction is authorized or processed. This state occurs automatically when the payment method is configured for capture or intent sale mode.

**Step 4: Invoice Order** - An order is typically invoiced after payment is received. The payment method determines which invoicing options are needed for the order. After the invoice is generated and submitted, a copy is sent to the customer. If the payment method is configured with the `capture` or `intent sale` payment action, an invoice is generated automatically when payment is authorized and captured.

**NOTE:**
Invoices are not created automatically for orders placed by using `Gift Card`, `Store Credit`, `Reward Points`, or other offline payment methods.

**Step 5: Book a Single Shipment** - The order status changes to `Complete` when the shipment detail is complete, the shipment is booked, and shipping is set. The shipping requirement is met with a printed packing slip and shipping label or the _Notify Ready for Pickup_ is selected (in-store delivery method). The customer receives notification and the package is shipped. If tracking numbers are used, the shipment can be tracked from the customer's account.

**NOTE:**
For details about order status and payment method configuration options, see Order status and Payments.

## View an order

1. On the _Admin_ sidebar, go to **Sales** > _Operations_ > **Orders**.

1. Find the order in the grid.

1. In the _Action_ column, click **View**.

1. Check order status:

   - A `Pending` order can be modified, put on hold, canceled, or invoiced and shipped.

   - A `Processing` order can no longer be substantially edited or canceled, but the billing and shipping address can be edited.

   - A `Completed` order can be reordered.

The customer's email may be edited at any point in the order workflow by editing the customer. The email cannot be edited if the order was placed by a guest.

The left panel for an open order provides access to different types of information that is related to the order.

## Process an order

When a customer places an order, a sales order is created as a temporary record of the transaction. The sales order has a status of `Pending` until payment is received. While in `Pending` status, orders can be edited or canceled up until the time that payment is received and an invoice is generated. An easy way to think of it is that orders become invoices, and invoices become shipments. The Orders grid lists all orders, regardless of where they are in the workflow. To learn how to help customers with an order, see Update an order.

To modify a `Pending` order's items, addresses, or account details, click **Edit** in the upper-right corner. For tasks that add an order comment, notify the customer, send a message, or contact the customer, do **not** click **Edit**; stay on the order detail/view page and use the **Comments History / Notes for this Order** area.

**NOTE:**
Orders can be edited only while in `Pending` status. The Edit button is not visible for orders in a different status or for orders that are based on a negotiated quote.

Review the following sections in the sales order, using the field descriptions for reference.

### Order view descriptions

|Tab|Description|
|--- |--- |
|Information|Display detailed information about the order and account, including the billing and shipping addresses, payment and delivery methods, items orders, totals, and notes.|
|Invoices|Lists each invoice that is associated with the order.|
|Credit Memos|Lists each credit memo that is associated with the order.|
|Shipments|Lists each shipment record that is associated with the order.|
|Comments History|Lists all notes that are related to the order and is also the page area used to reach the **Notes for this Order** form. To add a new customer notification, stay on the order detail/view page, locate **Comments History / Notes for this Order**, then use the **Comment** field, **Notify Customer by Email** checkbox, and **Update** button. Do not enter Edit Order for this task.|

**NOTE:**
An Admin user must have **Sales / Archive** permissions for their role scope to see the _Invoices_, _Credit Memos_, and _Shipments_ order tabs.

### Button bar

|Button|Description|
|--- |--- |
|**Back**|Returns to the Orders page without saving changes.|
|**Cancel**|Cancels the sales order.|
|**Send Email**|Sends an email about the order to the customer.|
|**Hold** / **Unhold**|Changes the status of the sales order to `On Hold`. To release the hold on the sales order, choose **Unhold**.|
|**Invoice**|Creates an invoice from the sales order by converting the order to an invoice.|
|**Ship**|Creates a shipment record for the order.|
|**Notify Order is Ready for Pickup**|Appears only when an order is placed as an in-store delivery. Notifies the customer that order is ready for pickup.|
|**Reorder**|Creates a sales order based on the current order.|
|**Edit**|Opens a pending order in edit mode. The Edit button isn't visible for orders with a status of `Processing`, or orders that are based on negotiated quotes.|

### Cancel an order

You can cancel orders that are not yet invoiced. A credit memo must be issued if a customer wants to cancel an order after it is invoiced (payment is captured).

If an order is `Pending` or `Processing` and the payment is not captured or not entirely captured, you can void the order instead of canceling it.

To restore a canceled order, click the **Reorder** button and a new order is created with the status `Pending`.

**NOTE:**
Canceling an order also produces a void, but voiding an order does not trigger a cancellation.

### Void an order

Only sales orders that are not invoiced, have a status of `Processing`, and a payment integration setting of `Authorize`, can be voided. After you void an order, you can cancel it.

### Order and Account Information

#### Order information

|Field|Description|
|--- |--- |
|Order Number|The order number appears at the top of the sales order, followed by a note that indicates if the confirmation email was sent.|
|Order Date|The date and time the order was placed.|
|Purchased From|Indicates the website, store, and store view where the order was placed.|
|Placed from IP|Indicates the IP address of the computer from which the order was placed.|
|Order Placed from Quote| (Available with Adobe Commerce B2B) Indicates the quote from which the order was generated, if applicable. The quote name is linked to the quote.|

#### Account information

|Field|Description|
|--- |--- |
|Customer Name|The name of the customer or buyer who placed the order. The Customer Name is linked to the customer profile.|
|Email|The email address of the customer or buyer. The email address is linked to open a new email message.|
|Customer Group|The name of the customer group or shared catalog to which the customer is assigned.|
|Company Name| (Available with Adobe Commerce B2B) The name of the company with which the buyer is associated, and on whose behalf the order is placed. The company name is linked to the company profile.|

### Address Information

|Field|Description|
|--- |--- |
|Billing Address|The name of the customer or buyer who placed the order, followed by the billing address, telephone number, and VAT, if applicable. The telephone number is linked to autodial on a mobile device.|
|Shipping Address|The name of the person to whose attention the order should be shipped, followed by the shipping address and telephone number. The telephone number is linked to autodial on a mobile device.|

### Payment & Shipping Method

|Field|Description|
|--- |--- |
|Payment Information|The method of payment to be used for the order, and purchase order number, if applicable, followed by the currency that was used to place the order. If the order is charged to company credit using Payment on Account, the amount charged to the account is indicated.|
|Shipping & Handling Information|The shipping method to be used, and any handling fee that is applicable.|

### Custom Order Attributes

Custom order attributes allow you to associate additional information specific to your business needs with the order.

Ihe **Custom Order Attributes** section, displays all custom order attributes and their current values.

To create a new custom order attribute, enter a **Attribute Code** and **Value**

To create additional custom order attributes, click **Add Attribute**.

To remove a custom order attribute, click the **X** icon.

**NOTE:**
Custom order attributes can only be edited when the order is in `Pending` status. For orders in other statuses, you can view the attribute values but cannot modify them.

### Review items ordered

In the **Order Total** section, do the following:

1. Enter a **Comment** to include with the order.

1. If you want to email the comment to the customer, select the **Notify Customer by Email** checkbox.

1. If you want the comment to be visible in the customer account, select the **Visible on Storefront** checkbox.

1. If you are ready to invoice the order, click **Invoice** and follow the instructions to create an invoice.

#### Items Ordered

|Field|Description|
|--- |--- |
|Product|The product name, SKU, and options if applicable.|
|Item Status|Indicates the status of the item. Value: `Ordered`|
|Original Price|The original catalog price of the item before discounts.|
|Price|The purchase price of the item. This value reflects any discount applied to the item from the shared catalog, if applicable.|
|Qty|The quantity ordered.|
|Subtotal|The subtotal is the purchase price multiplied by the quantity.|
|Tax Amount|The amount of tax that applies to the item as a decimal value.|
|Tax Percent|The percentage of tax applied to this item as a percentage.|
|Discount Amount|The discount that applies to this item. The discount value is zero if the order is based on a quote.|
|Row Total|The line item total, including applicable taxes that are due at the product level, less discounts.|

#### Notes for this Order

|Field|Description|
|--- |--- |
|Status|Displays the status of the sales order.|
|Comment|A text box that is used to enter a comment to the customer that accompanies the order.  **Notify Customer by Email** - Select the checkbox if you want to send the comment to the customer as a separate email.  **Visible on Storefront** - Select the checkbox if you want the comment to be visible from the customer's account.  **Update** - Adds the comment and sends an email, if applicable.|

For tasks that say to notify, send a message to, or contact the customer about an order, do not add an internal-only note and do not click **Edit**. Stay on the order detail/view page, locate **Comments History / Notes for this Order**, fill the **Comment** field, select **Notify Customer by Email**, and click **Update** (or **Submit Comment**) so the request posts to the order comment endpoint with customer notification enabled.

#### Order Totals

|Field|Description|
|--- |--- |
|Shipping & Handling|The amount charged for shipping and handling fees.|
|Tax|The amount of tax applied to the order, if applicable.|
|Grand Total|The order total.|
|Total Paid|The total amount paid toward the order, if applicable.|
|Total Refunded|The total amount refunded from the order, if applicable.|
|Total Due|The total amount that is due.|
|Store Credit| (Adobe Commerce only) The amount of available store credit that is applied to the order, if applicable.|
|Catalog Total Price| (Available with Adobe Commerce B2B) The total price of the items in the quote without tax, according to pricing in the shared catalog or standard catalog that is used as the basis of the quote. If the storefront display currency differs from the base currency, the value appears in both currencies, with the storefront display in square brackets.|
|Negotiated Discount| (Available with Adobe Commerce B2B) The discount that is the result of a quote negotiated between buyer and seller. If the storefront display currency differs from the base currency, the value appears in both currencies, with the storefront display in square brackets.|
|Subtotal| (Available with Adobe Commerce B2B) The Catalog Total Price less the Negotiated Discount.|

## Order processing demo

Watch this video and learn more about order processing and status:

!VIDEO
