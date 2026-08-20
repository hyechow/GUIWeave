---
id: knowledge.browser.shopping.catalog_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: catalog search category price range under over cheapest expensive highest rated best rating capacity sale product budget go view navigate storage suitability problem
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 9
---
# One Stop Market catalog data

## Select the recall surface

Use a category as the recall source only when its taxonomy covers the full requested product class.
Otherwise use Advanced Search > Product Name, whose matching is contiguous substring matching. The
header mini-search uses broad OR matching and is suitable for discovery or resolving a handed-off
exact identity, not as proof of a complete class. Keep the full requested class unchanged in
collector filters even when the search query must be shortened.

Catalog rows provide `product_name`, current `price`, and sometimes rating percentage/review count.
A title's hidden href is an action target, not source data; use stable visible `product_name` for a
typed handoff and resolve its exact matching card through header mini-search. Treat current/special
price as `price`, not a crossed-out regular price. Card rating is a percentage; customer reviews
use one-to-five stars.

## Price boundaries and ranking

`Women` and `Men` are category levels, not product modifiers. Their leaf paths are
`/<top-category>/women/<leaf>.html` and `/<top-category>/men/<leaf>.html`. Sidebar choices may replace
the prior chip and leave a `cat=<id>` alias on the parent path. A target or navigation history containing
either level is invalid unless its canonical URL contains `/women/` or `/men/`; otherwise preserve the
top category and every successive category choice as lowercase, hyphenated segments.

For an exact range, navigate directly to the canonical category URL with
`price=<lower>-<upper>`; "under X" maps to `price=0-X`. Do not first apply a wider visible facet or
preserve a `cat=<id>` alias; visible price facets are coarse alternatives, not a nested hierarchy.

For a price boundary, apply every predicate, sort by Price in the needed direction, and use
`coverage="first_match"`; the grid is row-major. From a rejected product detail, Back preserves the
ordered grid: continue after that product and never rerun the search. A direction label names the
next action. For ascending, click `Set Ascending Direction` and complete on `Set Descending
Direction`; for descending, do the inverse.

Sort By does not offer customer rating. For highest/best-rated requests, collect the bounded
candidate set with product name, rating, review count, and current price, then rank deterministically
and hand off the selected name. Apply review-count and budget predicates before rating/price
tie-breakers. A missing rating or review count is not zero unless the request defines it that way.

## Content-derived product attributes

Storage capacity, compatibility, dimensions, pack count, and problem/use suitability are product
content rather than catalog sort fields. Collect enough Product Name/detail content to validate the
requested attribute, reject accessories or bundles that do not satisfy the primary product class,
and rank only the validated candidates. When the task asks for a product page, the final action is
navigation to the selected product detail, not merely returning its name.

For shoe storage, N dedicated boxes/slots = N pairs and N single-shoe pockets = N/2 pairs. Reject a
below-minimum detail and Back to the ordered grid.

For a price range, use the same bounded recall source for both ends. Do not take a minimum from one
query/category and a maximum from another. Return not-found only after the bounded source is
exhausted and no row satisfies the full class predicate.
