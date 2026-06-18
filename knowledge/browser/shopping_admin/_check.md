---
id: knowledge.browser.shopping_admin.check_rules
source_type: knowledge_check_rules
platform: browser
app: shopping_admin
scope:
  - checker
source: manual_curated
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# 验收观察规则：shopping_admin

- Orders grid 的 any-state / all orders 口径要求没有状态过滤。若可见 Active filters 中仍有 Status: Complete、Status: Pending 等状态过滤，不能判定为已准备好全状态数据源。
- completed orders 口径必须有明确状态约束：界面过滤显示 Status = Complete，或后续 data_query 明确使用 `lower(status) = 'complete'`。仅在子目标名称里写 completed 不等于过滤已生效。
- Dashboard 中 Top Search Terms 与 Last Search Terms 不是同一口径。询问 top / most-used search terms 时，只有标题为 Top Search Terms 的区块满足验收；Last Search Terms 只表示最近搜索词。
- 订单邮箱数量聚合的最终证据必须包含 Customer Email 与订单状态/订单行数据。Customers grid 或 Customer Reports 若没有 Customer Email + 完整订单行，不能单独作为邮箱聚合任务的完成证据。
