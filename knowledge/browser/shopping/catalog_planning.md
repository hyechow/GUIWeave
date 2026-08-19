---
id: knowledge.browser.shopping.catalog_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: catalog search category price range cheapest expensive highest rated best rating capacity sale product budget go view navigate storage suitability problem
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 2
---
# One Stop Market catalog data

## Select the recall surface

Use a category as the recall source only when its taxonomy covers the full requested product class.
Otherwise use Advanced Search > Product Name, whose matching is contiguous substring matching. The
header mini-search uses broad OR matching and is suitable for discovery, not as proof of a complete
class. Keep the full requested class unchanged in collector filters even when the search query must
be shortened.

Catalog rows can provide `product_name`, current `price`, rating percentage, review count, and the
product-detail link. Treat the current/special price as `price`; do not use a crossed-out regular
price for budget or ranking decisions. Rating shown on a product card is a percentage, while an
individual customer review uses a one-to-five star rating. Do not interchange the two scales.

## Price boundaries and ranking

For minimum, maximum, cheapest, or most-expensive requests, apply the complete product-class and
budget predicates, sort by Price in the needed direction, and use `coverage="first_match"`. A
category grid has row-major display order. The direction control's accessible label describes the
next action, not the current state.

Sort By does not offer customer rating. For highest/best-rated requests, collect the bounded
candidate set with rating, review count, current price, and detail link, then rank deterministically.
Apply any minimum review-count and budget predicates before rating and price tie-breakers. A missing
rating or review count is not equivalent to zero unless the request explicitly defines it that way.

## Content-derived product attributes

Storage capacity, compatibility, dimensions, pack count, and problem/use suitability are product
content rather than catalog sort fields. Collect enough Product Name/detail content to validate the
requested attribute, reject accessories or bundles that do not satisfy the primary product class,
and rank only the validated candidates. When the task asks for a product page, the final action is
navigation to the selected product detail, not merely returning its name.

For a price range, use the same bounded recall source for both ends. Do not take a minimum from one
query/category and a maximum from another. Return not-found only after the bounded source is
exhausted and no row satisfies the full class predicate.
