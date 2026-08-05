---
id: knowledge.android.Taodian.interface
source_type: knowledge_interface
platform: android
app: Taodian
scope:
  - orchestrator
selector_when: Taodian TaoDian shopping cart expensive price total orders recent month expense 淘店 购物车 最近1个月 消费 花了多少钱
source: manual_verified
confidence: high
ttl: session
---
# Taodian interface

- **ShoppingCart** is a complete collection exposing only **price** (`money`) for price
  calculations. The three most expensive items are selected by sorting all queried prices before
  slicing three and summing them.
- **Orders** is a complete collection exposing **amount** (`money`), the order's total payment. Its
  source filter **order_time** accepts the exact relative value `近1个月`. Recent-one-month spending
  is the sum of every amount returned by that filtered query.
