---
id: knowledge.browser.shopping.navigation
source_type: knowledge_navigation
platform: browser
app: shopping
scope:
  - worker
aliases:
  - One Stop Market
  - OneStopMarket
  - OneStopShopping
browser_origins:
  - http://localhost:7770
  - http://127.0.0.1:7770
source: manual_distilled
confidence: high
sensitivity: internal
ttl: session
version: 24
---
# One Stop Market storefront

## Global navigation

- Account menu links My Account, My Wish List, Sign Out; My Account contains My Orders,
  Address Book, Account Information, Product Reviews, Newsletter Subscriptions.
- Full cart: `http://localhost:7770/checkout/cart/`. Open it directly; `My Cart` and
  `View and Edit Cart` are previews, not navigation or authoritative full-cart state.
  Footer: Advanced Search, Contact Us, newsletter.
- For "the product on the current page", preserve the starting product; do not search.

## Catalog and search

- Header mini-search uses broad OR matching. For a handed-off exact Product Name it tolerates
  punctuation variants; activate the exact result card. It is not a bounded class source.
- Advanced Search > Product Name uses contiguous matching. Without an exact category, query the
  supplied use/problem phrase or unchanged base type and validate every result; titles may vary.
- Results are main-grid cards, never sidebar My Wish List entries; reveal a below-fold name first.
- Top navigation is hierarchical. Use the narrowest category only when it covers the full class.
  Earbuds: Electronics > Headphones > Earbud Headphones; in-ear/behind-neck models are earphones.
- Paths: Makeup Remover `/beauty-personal-care/makeup/makeup-remover.html`; Accent Furniture
  `/home-kitchen/furniture/accent-furniture.html`; Ceiling Lights
  `/tools-home-improvement/lighting-ceiling-fans/ceiling-lights.html`.
- Category and search-result pages offer Grid/List mode, Sort By, direction, per-page, and pagination.
  Cards expose stable Product Name, current price, and sometimes rating percentage/review count;
  hidden hrefs are action targets, not row data. Hand off Product Name; resolve the exact card via
  header mini-search. A struck-through old price is not the current sale price.
- `Women` and `Men` are path segments: leaf URLs use `/<top>/women/<leaf>.html` or
  `/<top>/men/<leaf>.html`; retain the segment when the target or history includes it.
- Sidebar choices may replace the prior chip and keep a `cat=<id>` alias on the parent path;
  canonical paths retain the top category and every choice as lowercase, hyphenated segments.
- Exact ranges bypass coarse visible facets: open the canonical URL with
  `price=<lower>-<upper>`; "under X" maps to `price=0-X`. Do not preserve a `cat=<id>` alias.
- Sort By includes Price but not customer rating. Direction labels name the next action, not state.
  For ascending, click `Set Ascending Direction`; `Set Descending Direction` means complete.
  For descending, click `Set Descending Direction`; `Set Ascending Direction` means complete.
- Product grids are row-major. For a price boundary, sort and take the first match. Back from a
  rejected detail preserves query/order; continue after it and never rerun search.
- Shoe-storage capacity: N boxes/slots = N pairs; N single-shoe pockets = N/2 pairs. Reject a
  below-minimum detail and Back to the ordered grid.

## Product pages and reviews

- A product page contains the product name, current price, SKU, availability, rating summary,
  review-count link, details, quantity, and purchase controls. Configurable products also expose
  required option controls such as size or color; select any available value only when the task
  permits any variant.
- Add to Cart and Add to Wish List act on the current product. A successful action produces a
  confirmation message and the destination collection contains the product.
- The Reviews link/tab reaches the current product's review list. Each review exposes a star rating,
  summary/title, review body, and reviewer nickname. Preserve duplicate reviews and their displayed
  order when the request says all reviews or same order.
- Add Your Review exposes Your Rating, Nickname, Summary, Review, and Submit Review. Selecting a star
  label is required; filling the text fields alone does not submit a review.

## Shopping cart and checkout

- Full Shopping Cart rows expose product, options, price, quantity, subtotal, and Remove. To discard
  existing items, remove every row; once the next frame is empty, begin product lookup and do not
  revisit until the target has been added.
- "Add" means stop after the item is in the requested cart or wish list. "Buy" means continue from
  the cart through checkout until the order-success page; reaching the cart alone is incomplete.
- Checkout proceeds through Shipping, then Review & Payments. The saved customer address may already
  populate shipping and billing. Place Order is the final commit and should be used only for an
  explicit purchase request.

## Orders

- My Account > My Orders lists Order #, Date, Order Total, Status, and View Order, newest first,
  ten per page. It has no date, product, category, or status filters.
- For a closed date interval, advance until the upper boundary appears. After the
  first row older than the lower boundary, later pages cannot qualify. The first page is
  authoritative for latest;
  the last reachable row is authoritative for first/oldest.
- View Order shows date/status, addresses, totals, and Items Ordered rows with Product Name, Price,
  Qty, and line Subtotal. Product Name embeds title, SKU, and selected option labels and values.
- If exhaustive pagination finds no detail-view order, use `Page 1` to return to canonical My Orders
  before reporting not found. Terminal reporting from later pages is invalid.
- The totals block separates Subtotal, Shipping & Handling, and Grand Total. An arrival date is not
  displayed. A placed order's delivery address cannot be edited from the storefront order detail.
- Reorder on an order detail adds that order's products to the cart. It does not itself complete a
  new checkout.

## Customer account

- Address Book separates default billing, default shipping, and additional address entries. Edit
  Address opens First Name, Last Name, Company, Phone Number, Street Address lines, City,
  State/Province, ZIP/Postal Code, Country, and default-address checkboxes.
- A general moved/address-update request refers to editing the existing account address, not a
  placed order's immutable shipping address. Preserve existing identity and phone fields unless the
  request changes them; put unit/suite/house information on Street Address line 2.
- My Wish List lists saved products and supports quantity updates, removal, and adding items to the
  cart. A catalog or product-page Add to Wish List is the shortest path for a requested addition.

## Contact and newsletter

- Contact Us contains Name, Email, Phone Number, What's on your mind?, and Submit. Signed-in customer
  details may be prefilled. A request to leave the form ready for review means fill the requested
  fields and remain on Contact Us without activating Submit.
- The footer newsletter Email field posts directly via Subscribe. The signed-in email is
  `emma.lopez@gmail.com`; use it unless the request supplies another email.
