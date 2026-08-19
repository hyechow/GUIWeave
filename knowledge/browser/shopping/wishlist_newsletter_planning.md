---
id: knowledge.browser.shopping.wishlist_newsletter_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: wish wishlist wish list newsletter subscribe subscription save favorite product
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market wish list and newsletter

For a named product, resolve the exact catalog product first and activate Add to Wish List on its
card or detail page. For "the product on the current page", preserve the start-page identity and use
that page's action directly. A plain addition uses the product's default quantity; only set a larger
quantity when requested. Verify the confirmation and the product in My Wish List.

Use the footer Email + Subscribe form for a direct newsletter subscription. The account email is
the subscription identity when no other email is supplied. My Account > Newsletter Subscriptions is
useful for checking or managing the stored subscription, but merely reaching it does not complete a
new Subscribe request. Finish after the subscription confirmation, without logging out or changing
accounts.
