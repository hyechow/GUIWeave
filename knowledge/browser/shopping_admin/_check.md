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

- **Admin 网格「Columns 面板启用列」子目标的验收规则** ⚠️ 高频陷阱：
  - **唯一可靠判据**：主网格**表头出现目标列名**（如列标题行出现「Color」）。
  - **`current='on'` 不可信**：Columns 面板内 checkbox 的 DOM `current='on'` 可能是上次会话残留，本次会话可能从未实际点击过——不能作为「列已启用」的直接证据。
  - **⚠️ Cancel 会撤销所有改动**（通用规则）：点 Cancel = 取消自面板打开以来的所有勾选改动，之前勾的列全部恢复原状。**正确关闭方式：点击面板外的空白区域**（关闭面板同时保留已勾选的改动）。
  - **正确反馈**：若主网格无目标列 → 说明「需在面板内找到目标 checkbox 并点击（网格立即更新）；确认后点击面板外空白区域关闭面板」。若 checkbox 不在视口 → 说明「需先在面板内向下滚动找到 checkbox 再点击」。
- **Admin 网格「设置过滤条件」子目标的验收规则**：
  - **判断 filter 是否已提交**：唯一可靠的判据是页面出现「Active filters:」标签，并列出与子目标一致的条件及其值。没有该标签 → filter 未提交，无论可见行值是否恰好符合条件，验收必须是 `in_progress`。典型陷阱：网格列按某列升/降序排列时，第一页恰好显示符合条件的值（如 qty=0），但 records found 仍是全量、且无 Active filters 标签，说明排序巧合而非过滤。
  - **数值 From/To 筛选面板开着时（Apply Filters 之前）**：数值列的过滤有 From 和 To 两个输入框；若任务要精确匹配某值（如恰好等于 0），必须两个框都填（From=X, To=X）。若只填了 From 而 To 仍为空，验收必须是 `in_progress`，反馈说明「只填 From 表示 ≥ X，会匹配所有满足下界的行；必须同时填 To=X 才是精确匹配」，不要在 To 为空时催促点 Apply Filters。
  - **判断数值 filter 范围是否正确**（Active filters 已显示后）：若 Active filters 标签显示 `某列: X - ...`（省略号代表上界缺失），说明**只设了 From=X，没有设 To**，相当于「≥ X」= 可能匹配所有行，records 可能仍是全量。此时 filter 已提交但范围错误，验收是 `in_progress`，正确做法是**重新打开 Filters 面板补充 To=X**（From 可保留），而不是 Clear all 重来。`某列: X - X` 才表示精确匹配 X，此时 records 应明显缩减，验收可判 done。
  - **Quantity 筛选验收只看 Active filters chip，不逐行比数值** ⚠️：Products 网格同时有 **Quantity**（库存）和 **Salable Quantity**（可售 = 库存 − 已预留）两列，后者数值常小于 Quantity。按 Quantity 筛选后，Active filters 出现 `Quantity: N - N` 且 records > 0 即筛选成功（`done`）。**不要因为某行 Salable Quantity < N 就否定筛选结果**——Salable Quantity 不是筛选条件，其值与 Quantity 不同是正常的（有预留订单时 Salable < Quantity）。
  - **数值筛选 chip 格式等价**：Magento 数值范围筛选的 Active filters chip 始终显示区间格式 `列名: X - X`（如 `Quantity: 3 - 3`），即使验收条件写作 `Quantity: 3`（单值写法）。`Quantity: N - N` 等同于 `Quantity: N` 精确匹配，两种写法都满足验收。
- Orders grid 的 any-state / all orders 口径要求没有状态过滤。若可见 Active filters 中仍有 Status: Complete、Status: Pending 等状态过滤，不能判定为已准备好全状态数据源。
- completed orders 口径必须有明确状态约束：界面过滤显示 Status = Complete，或后续 data_query 明确使用 `lower(status) = 'complete'`。仅在子目标名称里写 completed 不等于过滤已生效。
- Dashboard 中 Top Search Terms 与 Last Search Terms 不是同一口径。询问 top / most-used search terms 时，只有标题为 Top Search Terms 的区块满足验收；Last Search Terms 只表示最近搜索词。
- 订单邮箱数量聚合的最终证据必须包含 Customer Email 与订单状态/订单行数据。Customers grid 或 Customer Reports 若没有 Customer Email + 完整订单行，不能单独作为邮箱聚合任务的完成证据。
