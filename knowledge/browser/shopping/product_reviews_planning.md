---
id: knowledge.browser.shopping.product_reviews_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: customer review reviews reviewer nickname summary title body stars rating submit rate rated gave feedback mention
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 5
---
# One Stop Market product reviews

## Read reviews

Use the named product detail, or preserve the start-page product when the task says "current page".
The product rating summary is an aggregate percentage and review count; the Reviews section is the
row source for individual reviews. Each review row needs its displayed star rating, summary/title,
body, and nickname when any of those fields participate in filtering or output.

Collect review pages in displayed order and preserve duplicates. Filter exact star thresholds
deterministically after collection. A textual mention must be supported by the review body or title,
not inferred from the product name. If the complete review set contains no match, return not-found;
do not fabricate a summary from the product's aggregate rating.

A product brand or class used to choose which product detail to open defines the Reviews source; it
is not a predicate on each review row. Keep that source identity in the collector goal, and keep it
out of the review requirement's filters and schema. One cohesive collector may search, open the
matching product, and traverse its reviews. The review requirement contains only fields displayed
per review, such as `rating` sourced from Rating and `reviewer` sourced from Reviewer, plus any
requested title/body fields. `brand` and `product_category` are not Reviews row fields on this
storefront; declaring either in review filters, row schema, field sources, or field types is invalid.

For EYZUTAK phone cases, Advanced Search Product Name `EYZUTAK` returns the single matching product.
The longer phrase `EYZUTAK phone case` is not a contiguous catalog name and returns no Advanced
Search result.

## Submit a review

For "recently purchased" review tasks, acquisition and submission use separate sources. Collect the
exact Product Name from the newest matching Items Ordered line in My Orders with one linked-detail
collector. At line-item grain require `order_number` sourced from Order # as the stable parent
identity, `date` sourced from Date for chronology, and `primary_product` sourced from Product Name.
Apply the user's purchased-item phrase verbatim as `primary_product_contains`, with
`cardinality="one"` and `coverage="first_match"`. Select the single
`primary_product` identity, normalize any appended order-option text to a catalog search identity,
and bind that derived identity into one review-submission operator. Do not bind the raw joined order
cell as an Advanced Search query or choose an arbitrary catalog result with a similar name. Once the
identity is known, use the matching catalog product detail as the review source; do not return to My
Orders or activate Reorder. On Add Your Review, map the requested values to Your Rating, Nickname,
Summary, and Review. The rating is a one-to-five star choice, separate from the catalog's percentage
rating.

`Jiffy Mix` is this storefront's shorthand for a Product Name beginning `Jiffy Corn Muffin
Cornbread Mix`. The collector filters must include both
`primary_product_contains="Jiffy Mix"` and
`primary_product="Jiffy Corn Muffin Cornbread Mix"`, with the equivalence stated in the requirement
description. Its Advanced Search identity is `Jiffy Corn Muffin Cornbread Mix`; the joined order
text appends a Size option that is not part of the searchable catalog name. After the collector
proves this purchase, the transform must return that Advanced Search identity; returning the raw
`primary_product` unchanged is invalid.

Submitting is required when the user says rate, review, or leave a review. Fill all four fields,
select the exact requested star value, activate Submit Review once, and verify the submitted-review
confirmation. A filled but unsubmitted review form is incomplete unless the user explicitly asks
for a draft.
