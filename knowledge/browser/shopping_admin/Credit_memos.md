---
when: 当需要生成、打印或查看 Credit memos 并处理 Account credit 与 Online refund 等退款方式时查阅本节
---
# Credit memos

A _credit memo_ is a document that shows the amount that is due the customer for a full or partial refund. The amount can be applied toward a purchase or refunded to the customer. You can print a credit memo for a single order, or for multiple orders as a batch. Before a credit memo can be printed, it must first be generated for the order. The _Credit Memos_ page lists the credit memos that have been issued to customers.

## Refund method

The payment method for the order determines, to an extent, the method by which you refund an order.

You can refund orders in three ways:

- Account credit---Orders paid using a credit account can be refunded as an account credit:
    -  (Adobe Commerce only) Store Credit
    -  (Available with Adobe Commerce B2B) Payment on Account (offline method)
    -  (Available with Adobe Commerce B2B) Company Credit
- Online refund---Orders paid by credit card through a payment gateway, such as PayPal or Braintree, are refunded online via the payment processor.
- Offline refund---Orders that are paid by Cash on Delivery (COD) or by check or money order are refunded offline.

You can issue an offline refund or account credit (if enabled) for any payment method.

An order that was paid by Cash on Delivery (COD) or by check or money order is refunded offline.

## Refund workflow

1. **Payment action** - If the Payment Action configuration is set to `Authorize`, you must generate an invoice before creating a credit memo---proceed to step 2. If set to `Authorize and Capture`, an invoice has already been generated---proceed to step 3.

1. **Generate invoice** - Create an invoice for the order, so that you can send a refund to the customer via credit memo.

1. **Create credit memo** - Issue a credit memo in the Admin for a credit purchase, or a check or money order.

## Column descriptions

|Column|Description|
|--- |--- |
|Select|Select the checkboxes for the credit memo items to be subject to an action, or use the selection control in the column header. Options: `Select All` / `Deselect All`|
|Credit Memo|A unique numeric identifier that is assigned when a request for a credit memo is submitted.|
|Created|The date and time the buyer first submitted the request for a credit memo.|
|Order#|Order ID of the order whose products are being returned.|
|Order Date|The date and time the buyer placed an order.|
|Bill-to Name|The name of the person who is responsible to pay for the order.|
|Status|Indicates the current state of a credit memo request.|
|Refunded|The total amount refunded from the order.|
|Actions|**View** - Opens the request for a credit memo and maintains a record of the negotiation between buyer and seller.|
|Order Status|Indicates the order status.|
|Purchased From|Indicates the website, store, and store view where the order was placed.|
|Billing Address|The billing address of the customer who placed the order.|
|Shipping Address|The address where the order is to be shipped.|
|Customer Name|The first and last name of the customer who placed the order.|
|Email|The email address of the person who placed the order.|
|Customer Group|The customer group to which the customer is assigned.|
|Payment Method|The method of payment to be used for the payment.|
|Shipping Information|The method to be used to ship the order.|
|Subtotal|The order subtotal, without shipping and handling, and tax.|
|Shipping & Handling|The amount charged for shipping and handling.|
|Adjustment Refund|The amount that is added to the total amount refunded as an extra refund.|
|Adjustment Fee|The amount that is subtracted from the total amount refunded.|
|Grand Total|The order total.|