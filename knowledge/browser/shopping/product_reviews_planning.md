---
id: knowledge.browser.shopping.product_reviews_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: customer review reviews reviewer nickname summary title body stars rating submit rate feedback mention
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
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

## Submit a review

For "recently purchased" review tasks, identify the newest matching Items Ordered line from My
Orders before navigating to that product. Do not choose an arbitrary catalog result with a similar
name. On Add Your Review, map the requested values to Your Rating, Nickname, Summary, and Review.
The rating is a one-to-five star choice, separate from the catalog's percentage rating.

Submitting is required when the user says rate, review, or leave a review. Fill all four fields,
select the exact requested star value, activate Submit Review once, and verify the submitted-review
confirmation. A filled but unsubmitted review form is incomplete unless the user explicitly asks
for a draft.
