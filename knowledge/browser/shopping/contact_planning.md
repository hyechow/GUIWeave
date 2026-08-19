---
id: knowledge.browser.shopping.contact_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: contact us customer service phone message refund coupon form draft ready review do not submit order SKU amount
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market Contact Us

Contact Us has Name, Email, Phone Number, What's on your mind?, and Submit. The storefront does not
publish a customer-service phone number; absence after checking Contact Us is a not-found result,
not permission to invent one.

For a message that needs an order number, SKU, amount, or purchased product identity, retrieve those
facts from My Orders and Items Ordered first. Retain them as structured values and bind them into the
requested message exactly once. Use line Subtotal for a purchased item's amount and order Grand
Total only when the requested amount is explicitly whole-order total.

"Ready for review", "keep it ready", and "do not submit" all define a draft boundary: fill Name,
Email, and the message (plus Phone Number only when requested or already populated), remain on
Contact Us, and do not activate Submit. The populated form is the final state. A request that
explicitly says send or submit crosses that boundary and requires Submit plus a confirmation.
