---
id: knowledge.android.Taodian.navigation
source_type: knowledge_navigation
platform: android
app: Taodian
scope:
  - decompose
  - planner
  - replanner
source: manual_verified
confidence: high
sensitivity: internal
ttl: session
version: 3
---
# Taodian on Android

- The application may show a splash advertisement after launch; close it before using
  the application's bottom navigation.
- `ShoppingCart` is the signed-in cart reached through the bottom-navigation item
  `购物车`. Its page title is `购物车` and the item count may be shown beside it.
- Cart item prices are rendered in each item row, sometimes as adjacent currency,
  integer, and decimal text nodes. They are one logical monetary value.
- Each cart item row also carries the item's product title. Rows are individually
  actionable: the row exposes a per-item delete control, so removing one item targets
  that row and does not clear or empty the whole cart.
- `Orders` is the signed-in order history reached through bottom navigation
  `我的 → 我的订单` and the explicit `全部 >` link at the right of that section.
  The section body itself is not an activation target. Each order record displays one
  `合计` monetary value.
- If this route opens `用户登录` and no password was supplied, use its `短信登录`
  tab instead of guessing a password. The phone number is already populated. Activate
  `获取验证码`; the generated six-digit code is delivered as the newest SMS in the
  Messages app. Before focusing or filling the verification-code field, press Home,
  open Messages, and read that exact code from the newest message. Then reopen Taodian
  (the SMS form remains populated), enter the observed code, accept the agreements, and
  submit the login.
- The order-list filter opens an `订单筛选` sheet. Under `下单时间` it provides the
  exact relative values `近1个月`, `近3个月`, and `近6个月`; the selected value takes
  effect after activating `确认`.

## Interface contract

- The cart collection entity is exactly `ShoppingCart`.
- `ShoppingCart` exposes the query field `price`, typed as `money`. Query the complete
  collection before ranking or aggregation; cart selection controls and quantities are
  not needed for a price-only read.
- The order-history collection entity is exactly `Orders`. It exposes query field
  `amount`, typed as `money`, corresponding to the order's `合计` rather than an
  individual product price.
- Its source filter field is `order_time`. The application's exact one-month filter
  value is `近1个月`.
