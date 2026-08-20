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
version: 27
---
# One Stop Market storefront

## Global navigation

- Account menu links My Account, My Wish List, Sign Out. My Account contains My Orders, Address
  Book, Account Information, Product Reviews, Newsletter.
- Full cart: `http://localhost:7770/checkout/cart/`. Open it directly; `My Cart` and
  `View and Edit Cart` are previews, not navigation or authoritative full-cart state.
- For "the product on the current page", preserve the starting product; do not search.

## Catalog and search

- Canonical Nintendo Switch entry: `http://localhost:7770/video-games/nintendo-switch.html`.
  Open it directly with exact `price` and `product_list_limit`; search, menu/sidebar `cat` aliases,
  and coarse price facets are not equivalent sources.
- Header mini-search uses broad OR matching, not bounded class evidence. For a handed-off exact
  Product Name it tolerates punctuation variants; activate its title, verify the product-page
  title, then use that page's Add to Cart, never the result-card control.
- Advanced Search > Product Name uses contiguous matching. Without an exact category, query the
  supplied use/problem phrase or unchanged base type and validate every result; titles may vary.
- Results are main-grid cards, never sidebar My Wish List entries.
- Top navigation is hierarchical. Use the narrowest category only when it covers the full class.
  Earbuds: Electronics > Headphones > Earbud Headphones; in-ear/behind-neck models are earphones.
- Other paths: Makeup Remover `/beauty-personal-care/makeup/makeup-remover.html`; Accent Furniture
  `/home-kitchen/furniture/accent-furniture.html`; Ceiling Lights
  `/tools-home-improvement/lighting-ceiling-fans/ceiling-lights.html`.
- Category and search results offer Sort By, direction, Show up to 36, and pagination. For complete
  collection select Show 36 before paging. Cards expose Product Name, price, exact rating percent,
  and review count when present;
  hidden hrefs are action targets, not row data. Hand off Product Name; resolve the exact card via
  header mini-search. A struck-through old price is not the current sale price.
- `Women` and `Men` are path segments: leaf URLs use `/<top>/women/<leaf>.html` or
  `/<top>/men/<leaf>.html`; retain the segment when the target or history includes it.
- Men's Shoes: `/clothing-shoes-jewelry/men/shoes.html`.
- Canonical paths retain the top category and every choice as lowercase, hyphenated segments.
  Exact ranges use `price=<lower>-<upper>`; "under X" maps to `price=0-X`.
- Sort By includes Price but not customer rating. Direction labels name the next action, not state.
  For ascending, click `Set Ascending Direction`; `Set Descending Direction` means complete.
  For descending, click `Set Descending Direction`; `Set Ascending Direction` means complete.
- Product grids are row-major. For a price boundary, sort and take the first match. Back from a
  rejected detail preserves query/order; continue after it and never rerun search.
- Shoe-storage capacity: N boxes/slots = N pairs; N single-shoe pockets = N/2 pairs. Reject a
  below-minimum detail and Back to the ordered grid.

## Product pages and reviews

- Configurable product pages expose required options such as size or color; select any available
  value only when the task permits any variant.
- Add to Cart and Add to Wish List act on the current product. A successful action produces a
  confirmation message and the destination collection contains the product.
- The Reviews link/tab reaches the current product's review list. Each review exposes a star rating,
  summary/title, review body, and reviewer nickname. Preserve duplicate reviews and their displayed
  order when the request says all reviews or same order.
- Add Your Review exposes Your Rating, Nickname, Summary, Review, and Submit Review. Selecting a star
  label is required; filling the text fields alone does not submit a review.

## Shopping cart and checkout

- Full Shopping Cart rows expose product, options, price, quantity, subtotal, and Remove. Cleanup is
  pre-add only: remove inherited rows and confirm empty, then add the target. After Add success, its
  matching cart row is intended; proceed to checkout and never remove it or repeat cleanup.
- "Add" means stop after the item is in the requested cart or wish list. "Buy" means continue from
  the cart through checkout until the order-success page; reaching the cart alone is incomplete.
- Checkout proceeds through Shipping, then Review & Payments. The saved customer address may already
  populate shipping and billing. Place Order is the final commit and should be used only for an
  explicit purchase request.

## Orders

- My Orders: `http://localhost:7770/sales/order/history/`; open directly for the
  unpaginated list.
- My Orders lists Order #, Date, Total, Status, View Order, newest first, ten per page, without date,
  product, category, or status filters.
- For a closed date interval, advance until the upper boundary appears; after the
  first row older than the lower boundary, later pages cannot qualify. Page 1 is authoritative for latest; the last
  reachable row is authoritative for first/oldest.
- View Order shows date/status, addresses, totals, and Items Ordered rows with Product Name, Price,
  Qty, and line Subtotal. Product Name embeds title, SKU, and selected option labels and values.
- If exhaustive pagination finds no detail order, return via `Page 1` before reporting not found.
  Terminal reporting from later pages is invalid.
- Totals show Subtotal, Shipping, Grand Total; no arrival date or delivery edit exists. Retain this,
  never reopen the order, return to canonical unpaginated My Orders, then report unavailable.
- Reorder on an order detail adds that order's products to the cart. It does not itself complete a
  new checkout.

## Customer account

- Address Book separates default billing, shipping, and additional entries. Edit Address opens First
  Name, Last Name, Company, Phone Number, Street Address lines, City,
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
