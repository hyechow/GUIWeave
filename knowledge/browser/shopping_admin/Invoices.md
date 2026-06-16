---
when: 当需要查看订单发票、管理发票状态或生成销售用 PDF 发票时查阅本节
---
# Invoices

An invoice is a record of the record of payment for an order. Multiple invoices can be created for a single order, and each can include as many or as few of the purchased products that you specify. You can also create print-ready PDF invoices as sales documents for your customers.

On the _Admin_ sidebar, go to **Sales** > _Operations_ > **Invoices** to open the _Invoices_ grid and access your created invoices.

## Column descriptions

|Column|Description|
|--- |--- |
|Select|Select the checkboxes for the quotes to be subject to an action, or use the selection control in the column header. Options: `Select All` / `Deselect All`|
|Invoice|A unique numeric identifier that is assigned when an invoice is submitted from the Admin. When viewing the invoice detail, this number appears at the top of the page, instead of the quote name.|
|Invoice Date|The date and time the administrator first submitted the invoice.|
|Order#|A unique numeric identifier that is assigned when an order is placed by a buyer. When viewing the invoice details, this number appears as a link in the Order & Account Information block.|
|Order Date|The date and time the customer first successfully placed an order.|
|Bill-to Name|The name of the person who is responsible to pay for the order.|
|Status|Indicates the current state of an invoice. The status can be changed only by action on the part of either the buyer or seller.|
|Grand Total (Base)|The total price of products to be purchased. The total amount appears in the base currency of the website and in the currency of the storefront.|
|Grand Total (purchase)|The grand total of products purchased in the order. The total amount appears in the base currency of the website and in the currency of the storefront.|
|Purchased From|The website/store/store view from which the invoice was created.|
|Billing Address|The billing address of the customer who placed the order.|
|Shipping Address|The address where the order is to be shipped.|
|Customer Name|The first and last name of the customer receiving the invoice.|
|Email|The email address of the customer receiving the invoice.|
|Customer Group|The customer group assigned to customer receiving the invoice.|
|Payment Method|The method of payment to be used for the payment.|
|Shipping Information|The method to be used to ship the order.|
|Subtotal|The order subtotal, without shipping and handling, and tax.|
|Shipping and Handling|The amount charged for shipping and handling.|
|Action|**View** - opens the invoice in edit mode.|

## Create an invoice

Creating an invoice for an order moves it to a state in which it cannot be canceled or changed. A new invoice page looks similar to a completed order, with some additional fields. Every activity that is related to an order is noted in the Comments section of the invoice.

Normally, orders are invoiced and captured when the shipping process starts. If the method of payment is a purchase order, or if the payment action is set to `Authorize and Capture`, the order is invoiced and payment is captured during checkout. You can generate an invoice with a packing slip, and also print shipping labels from your carrier account. A single order can be divided into partial shipments, which are invoiced separately, if necessary.

When the state of new orders is set to `Processing`, the option to _Automatically Invoice All Items_ becomes available in the configuration. Some credit card payment methods complete the invoicing step as part of the process when the payment action is set to `Authorize and Capture`. In such a case, the Invoice button does not appear, and the order is ready to ship.

**NOTE:**
Invoices are not created automatically for orders placed by using `Gift Card`, `Store Credit`, `Reward Points`, or other offline payment methods.

An invoice for the order must be generated before it can be printed. To view or print the PDF, first download and install a PDF reader such as Adobe Acrobat Reader.

**_To invoice an order:_**

1. On the _Admin_ sidebar, go to **Sales** > _Operations_ > **Orders**.

1. Find the sales order with the status of `Processing` in the grid. Then, do the following:

1. In the _Action_ column, click **View**.

1. In the header of the sales order, choose the **Invoice** option.

   **NOTE:**
   >
   >The _Invoice_ option does not appear when the payment action for your specific payment method is set to `Authorize and Capture`, which auto-generates an invoice. This is also the case if the order is placed and the payment action for your payment method is set to `Authorize` and the order is invoiced.

   The new invoice page looks similar to a completed order page, with additional fields that can be edited.

1. If the items are ready to ship, generate a packing slip for the shipment at the same time that you create the invoice:

   - In the _Shipping Information_ section, click the **Create Shipment** checkbox to select it.

      The shipment record is created at the same time that the invoice is generated.

   - Include a tracking number:

      - Click **Add Tracking Number**.
      - Enter the tracking information: _Carrier_, _Title_, and _Number_

   - Optionally, generate a partial invoice:

      - In the _Items to Invoice_ section, update the **Qty to Invoice** column to include only specific items on the invoice.
      - Then, click **Update Qty's**.

1. If an online payment method was used for the order, set **Amount** to the appropriate option.

1. To notify customers by email when the invoice is generated, do the following:

   - Select the **Email Copy of Invoice** checkbox.

   - Enter any **Invoice Comments**. To include the comments in the notification email, mark the **Append Comments** checkbox.

1. When complete, click **Submit Invoice** at the bottom of the page.

   **_Online payment method:_**

   **_Offline payment method:_**

   The status of the order changes from `Pending` to `Complete`.

## Print invoices

Invoices can be printed individually or as a batch. However, before an invoice can be printed, it must first be generated for the order. You can upload a high-resolution logo for a print-ready PDF invoice, and include the Order ID in the header. To customize the invoice template with your logo and address, see PDF Logo Requirements.

**NOTE:**
To view or print the PDF, you must have a PDF reader. You can download Adobe Reader at no charge.

### Print a single invoice

1. On the _Admin_ sidebar, go to **Sales** > _Operations_ > **Invoices**.

1. In the _Invoices_ grid, locate the invoice and click **View** in the _Action_ column.

1. At the top of the invoice, click **Print** to generate a PDF of the invoice.

1. Save the generated PDF to a file or print it.

### Print multiple invoices

1. On the _Admin_ sidebar, go to **Sales** > _Operations_ > **Invoices**.

1. In the _Invoices_ grid, select the checkbox for each invoice to be printed.

1. Set the **Actions** control to `PDF Invoices`.

The invoices are saved in a single PDF file that can be sent to a printer or saved.

## Custom capture amounts

To provide merchants with greater flexibility for partial captures and specialized payment scenarios, the Invoice API supports custom capture amounts using extension attributes.

You can make REST calls to capture a custom amount when creating an invoice.  Use the `POST V1/order/:orderId/invoice` REST endpoint and specify the custom amount in the `extension_attributes.custom_capture_amount` field of the payload.

**NOTE:**
Contact your support representative to enable this feature.
Due to legal restrictions, the custom capture amount is only available in the North American (NA) region and other regions where payment overcapture is permitted.