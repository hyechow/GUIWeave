---
id: knowledge.browser.shopping_admin.skills
source_type: knowledge_skill
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
source: manual_curated
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# 编排技能：shopping_admin

## skill：订单邮箱数量聚合
- 触发：customer email(s)、completed orders、any-state orders、most/second/fifth number of orders、have N orders
- 数据：订单状态口径、Customer Email、Status、完整订单行
- 步骤：
1. 进入 Sales > Orders 订单数据源
2. 根据口径建立状态约束
3. 采集完整订单行
4. 按 Customer Email 聚合计数
5. 输出满足排名或数量条件的邮箱

## skill：Dashboard 搜索词读取
- 触发：top search terms、most-used search terms、recent search terms、dashboard search terms
- 数据：Dashboard 搜索词区块、请求口径、排名或词项
- 步骤：
1. 到达 Admin Dashboard 数据源
2. 根据口径选择对应搜索词区块
3. 读取请求排名或词项
4. 输出区块标题和读数

## skill：产品评论评分查询
- 触发：rating/stars、customer nickname(s)、reviews for product、product reviews、低评分评论
- 数据：All Reviews 评论行、Product、Review ID、Detailed Rating、Nickname
- 步骤：
1. 到达 All Reviews 评论数据源
2. 采集候选评论行标识
3. 逐条进入评论详情补齐评分与昵称
4. 按评分条件筛选并输出昵称

## skill：Products 网格含非默认列采集（如颜色、Color）
- 触发：产品颜色、color of products、name and color、products with color、哪些产品的颜色
- 数据：Products grid、Columns 控件、Color 列（非默认须启用）、Filters 数值范围控件
- 步骤：
1. 进入 Catalog > Products 产品列表
2. 按任务条件设置 Filters（数值列精确匹配须同时填 From=X 和 To=X）
3. 通过 Columns 控件启用 Color 列（Color 不在默认列，否则网格无颜色数据；启用即结束，不需关闭面板——详见 Admin_grid_controls 章节）
4. **foreach（body 留空）** 采集过滤后的全量网格：`returns: ["Name", "Color"]`，`into: products`，`body: []`——运行时 collect_fn 通过 AX 树自动翻全部分页（产品可能跨 8 页），into 产出 complete 表供 data_query 查询；简单产品 Color 有值，可配置父产品 Color 为空
5. data_query 过滤 color 非空行，输出 name 与 color

## skill：Grid 数据导出或采集
- 触发：需要完整 grid 数据、导出 CSV/XML、跨分页统计
- 数据：目标 grid、所需列、筛选口径、分页范围
- 步骤：
1. 到达目标 grid 数据源
2. 明确所需列和筛选口径
3. 优先取得完整导出数据
4. 无法导出时分页采集可见行
5. 对完整数据执行统计或筛选
