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
version: 12
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
Catalog money has cent precision: the logical predicate for strict "under X" is `max=X-0.01`
(`min=X+0.01` for strict "over X") even though the UI range endpoint remains X.
Nintendo Switch is `/video-games/nintendo-switch.html`. For complete catalog collection, use the
largest available Show value (36, or `product_list_limit=36`) before traversing pages; this changes
only acquisition volume, not product scope or order. Its visible collection caption is
`Nintendo Switch`; use that exact `target_label` to exclude broader catalogs.

For a price boundary, apply every predicate, sort by Price in the needed direction, and use
`coverage="first_match"`; the grid is row-major. From a rejected product detail, Back preserves the
ordered grid: continue after that product and never rerun the search. A direction label names the
next action. For ascending, click `Set Ascending Direction` and complete on `Set Descending
Direction`; for descending, do the inverse.

Sort By does not offer customer rating. Rating Percentage has a known upper bound of 100. For a
highest-rated request whose stated tie-break is minimum price, sort Price ascending and request one
record with the exact `rating_percentage: 100` predicate plus every other stated predicate, using
`coverage="first_match"`. The first match is the global lexicographic optimum; exhaustive acquisition
and post-hoc ranking are invalid for this bounded case; hand off the selected name. Otherwise collect
the bounded candidate set with Product Name, exact Rating Percentage, current Price, and Reviews only
when review count is a stated predicate or ranking key; never invent review count or price as a
tie-breaker, and do not treat a missing rating or review count as zero.

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
