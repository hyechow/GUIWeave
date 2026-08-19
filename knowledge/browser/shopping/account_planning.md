---
id: knowledge.browser.shopping.account_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: account address address book moved update information delivery shipping billing customer street city state postcode
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market customer account

Use My Account > Address Book for a general move or account-address update. Edit the existing address
rather than creating a second entry. Map the main street to Street Address line 1 and a suite, unit,
apartment, or house designator to line 2. Preserve the existing first name, last name, country, and
phone when the request does not replace them; select the requested State/Province rather than typing
an abbreviation into another field.

Default billing and default shipping are distinct roles. Preserve their existing role checkboxes
unless the request changes a role. Completion requires saving the address and observing the updated
address in Address Book.

A placed order's Shipping Address is historical and has no edit action on this storefront. For a
request to change the delivery address of an existing order, inspect the qualifying order detail to
confirm this limitation, then report that the action is not supported. Do not edit Address Book as a
substitute because that changes future checkout defaults, not the placed order.
