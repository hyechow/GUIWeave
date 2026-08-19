---
id: knowledge.browser.shopping.check_rules
source_type: knowledge_check_rules
platform: browser
app: shopping
scope:
  - checker
source: manual_curated
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market completion evidence

- A catalog sort is established by the selected Sort By value and actual first rows. The direction
  link label is the action it would perform next, not evidence of the current direction.
- Product-page navigation is complete only when the selected product's title/detail page is visible;
  a search or category row is not the requested product page.
- Add to Cart or Add to Wish List requires a success message or the exact product in the destination.
  Buy requires the checkout success page, not merely a nonempty cart.
- Empty-cart preparation requires the empty Shopping Cart state before the target is added.
- A submitted product review requires the review-submission confirmation. A filled review form alone
  is incomplete unless the user requested a draft.
- A Contact Us draft is complete when required fields retain the requested content on Contact Us and
  Submit has not been activated. A success/thank-you page is evidence that the draft was wrongly sent.
- An address update requires a saved confirmation or the new address rendered in Address Book.
- A placed order has no storefront delivery-address edit capability. Reaching the qualifying order
  detail and observing no edit action supports action-not-allowed; changing Address Book does not.
