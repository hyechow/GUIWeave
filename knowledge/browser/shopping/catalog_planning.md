---
id: knowledge.browser.shopping.catalog_planning
source_type: knowledge_section
platform: browser
app: shopping
scope:
  - orchestrator
selector_when: One Stop Market product catalog search price range category
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 1
---
# One Stop Market product search and price range

The mini-search uses broad OR matching. Advanced Search > Product Name performs contiguous
substring matching and is the bounded recall source when no category covers the full product class.
Keep the full requested class unchanged in collector filters and use Price ordering for boundaries.
A category is authoritative recall scope only when its taxonomy directly covers the full class.
