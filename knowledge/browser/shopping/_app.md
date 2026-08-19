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
version: 7
---
# One Stop Market storefront

## Global navigation

- The signed-in account menu exposes My Account, My Wish List, and Sign Out. My Account has
  sidebar links for My Orders, My Wish List, Address Book, Account Information, My Product Reviews,
  and Newsletter Subscriptions.
- The header cart opens a mini-cart; View and Edit Cart opens the full Shopping Cart. Footer links
  include Advanced Search and Contact Us, and the footer newsletter form has an Email field and
  Subscribe button.
- If a task starts on a product page and says "the product on the current page", keep that product
  identity. Do not replace it by searching for another product.

## Catalog and search

- The mini-search uses broad OR term matching, so it is not a bounded product-class source.
- Advanced Search > Product Name performs contiguous substring matching. When no catalog category
  covers the full class, use this recall source. Equivalent titles may preserve use/problem wording
  while varying the base-type label. Search with an exact noun-plus-gerund use/problem phrase when
  supplied, or the base type without modifiers; validate every result against the full class.
- The top navigation is a category hierarchy. Use the narrowest category only when its taxonomy
  directly covers the full requested class. Earbud products are under Electronics > Headphones >
  Earbud Headphones; in this catalog, in-ear or behind-neck headphones/headsets are earphones.
  Makeup Remover's canonical path is `/beauty-personal-care/makeup/makeup-remover.html`.
- Category and search-result pages offer Grid/List mode, Sort By, a separate direction link, a
  per-page selector, and pagination. Product cards expose name and current price, and may expose
  rating percentage and review count. A struck-through old price is not the current sale price.
- `Women` and `Men` are path-bearing categories. Leaf URLs are
  `/<top>/women/<leaf>.html` or `/<top>/men/<leaf>.html`; retain the matching segment whenever the
  target or history contains it.
- Sidebar category choices may replace the prior chip and leave a `cat=<id>` alias on the parent
  path. A canonical path instead preserves the top category and every successive category choice as
  lowercase, hyphenated path segments.
- Visible price facets are coarse. For an exact range, navigate directly to the canonical category
  URL with `price=<lower>-<upper>`; "under X" maps to `price=0-X`. Do not first apply a wider facet
  or preserve a `cat=<id>` alias.
- Sort By includes Price but not customer rating. The direction link label names the action it will
  perform: `Set Ascending Direction` means the current order is descending, while
  `Set Descending Direction` means it is ascending.
- Product grids preserve the selected order from left to right across each row, then top to bottom.
  For a price boundary, sort in the needed direction and take the first row that satisfies all
  remaining predicates; do not traverse the whole category.

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

- The full Shopping Cart shows each product, selected options, item price, quantity, subtotal, and
  a Remove item control. To empty it, remove every existing row and verify the empty-cart state
  before adding the requested product.
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
- View Order opens a detail page with Order Date and Status, Items Ordered, Shipping Address,
  Shipping Method, Billing Address, and order totals. Items Ordered rows expose Product Name, Price,
  Qty, and line Subtotal. Product Name includes the purchased title and SKU plus selected option
  labels and values, such as size or color; options are not separate table columns.
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
- The footer newsletter Email field posts a subscription directly with Subscribe. The signed-in
  customer's account email is the subscription identity unless the request supplies another email.
