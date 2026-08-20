---
id: knowledge.browser.shopping.cart_checkout_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: buy purchase checkout cart add discard remove empty highest rated best rating budget variant reorder previously ordered canceled cancelled
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 2
---
# One Stop Market cart and checkout

Distinguish the commit boundary from the verb. "Add to cart" completes when the exact product,
quantity, and requested options are visible in the cart. "Buy" requires a completed checkout and
the order-success page. Place Order is authorized only by an explicit purchase request.

When the task says to discard existing items, make `source: Full Shopping Cart and Checkout flow`
the operator's initial approach. Open the canonical full-cart URL from application knowledge before
adding the target, remove every row, and verify the empty state. Do not assume the mini-cart count
is current or leave an unrelated row because the target was added successfully. Treat the observed
empty frame as satisfying that precondition; do not revisit the cart before adding exactly one
target unless another quantity is requested.

For a product selected by category/rating/price criteria, finish selection from the catalog data
before mutating the cart. Configurable products require every mandatory option; when any available
variant is allowed, choose a visibly enabled value rather than inventing a preferred size/color.
Verify the cart has exactly the intended item count and product identity before checkout.

Checkout flows through Shipping and Review & Payments. Use the saved customer address unless the
purchase request supplies a different checkout address. Continue through payment review, activate
Place Order once, and finish only when the order-success page is visible.

Reorder is different from Buy: the order-detail Reorder action adds the old order's products to the
cart and satisfies a plain reorder request at that mutation boundary. Do not place a second order
unless the user also explicitly asks to buy or check out.
