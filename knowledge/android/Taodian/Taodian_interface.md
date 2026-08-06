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
- Deleting a cart item is a durable per-row mutation of that identified item. Reach the exact
  item row (target-bound), then `commit(target=row, values={"deleted": True})`. Cart deletion is
  per-item and consumes the item row; it is not a bulk or cart-clearing action.
- **Orders** is a complete collection exposing **amount** (`money`), the order's total payment. Its
  source filter **order_time** accepts the exact relative value `近1个月`. Recent-one-month spending
  is the sum of every amount returned by that filtered query.
