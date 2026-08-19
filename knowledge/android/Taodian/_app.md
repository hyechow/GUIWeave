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
version: 4
---
# Taodian on Android

- The application may show a splash advertisement after launch; close it before using
  the application's bottom navigation.
- `ShoppingCart` is the signed-in cart reached through the bottom-navigation item
  `购物车`. Its page title is `购物车` and the item count may be shown beside it.
- Cart item prices are rendered in each item row, sometimes as adjacent currency,
  integer, and decimal text nodes. They are one logical monetary value.
- Each cart item row carries the product title. `管理` enters the cart's multi-select
  editor; select the intended rows, activate `删除选中`, and confirm. Use this grouped
  path when one deletion request targets multiple items. A row-level delete surface
  can remain hidden behind row content until explicitly revealed and is not a visible
  action target merely because UIAutomator reports its bounds.
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
- Checkout flow (search → buy): search the item, open its detail page from the
  results, then use `立即购买` (not `加入购物车`) to start checkout. A spec-selection
  sheet may appear; confirm the required spec, then the order-confirmation page shows
  a `提交订单` button. Activate `提交订单` to enter the payment page — do NOT complete
  on the confirmation page. The payment page (payment-method selection, total shown)
  is the terminal surface a "let me pay / stay on the payment page" task wants to reach
  and remain on.
