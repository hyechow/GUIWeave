---
id: knowledge.android.Taodian.interface
source_type: knowledge_interface
platform: android
app: Taodian
scope:
  - orchestrator
selector_when: Taodian TaoDian shopping cart expensive price total orders recent month expense 淘店 购物车 最近1个月 消费 花了多少钱 删除 短袖 T恤 衬衫 cart delete short-sleeve shirt remove item
source: manual_verified
confidence: high
ttl: session
---
# Taodian interface

- **ShoppingCart** is a complete collection exposing **name** (`text`) and **price** (`money`).
  **name** is the item's product title and its row identity. Price reads sort and slice the full
  price list (the three most expensive items, then sum). Identifying an item class (e.g. whether
  a title is a short-sleeve T-shirt) is a semantic read over the visible row, not a source filter.
- In this catalog, the cart request `短袖T恤衬衫` denotes the union of visible title markers:
  select a row whose title contains **any of** `短袖`, `T恤`, or `衬衫`. It does not require
  `短袖` to co-occur with the other markers.
- Deleting identified cart items is a durable grouped mutation. For a condition that can match
  multiple rows, use the cart's `管理` multi-select editor, select every matching title while
  traversing the cart, then activate `删除选中` once and confirm. This removes only the selected
  rows; it is not a cart-clearing action.
- **Orders** is a complete collection exposing **amount** (`money`), the order's total payment. Its
  source filter **order_time** accepts the exact relative value `近1个月`. Recent-one-month spending
  is the sum of every amount returned by that filtered query.
