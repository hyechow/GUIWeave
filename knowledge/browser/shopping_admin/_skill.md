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

## skill：按库存数量筛选产品并取某个属性（颜色/材质/尺码）
- 触发：产品的颜色/材质/尺码、color/material/size of products、name and color、products with N units left 取某属性；凡是「按库存数量筛选产品、再取该产品某个属性」的任务
- 数据：Products grid、Columns 控件、Quantity 范围筛选、产品编辑页属性字段。目标属性是否是网格可选列决定走哪条路：Products Columns 面板权威可选列（37 列，含 Color，**不含 Material/Size**）。读属性下拉的空值判定——select 未选中时 `selectedIndex=-1`，判为空，绝不把首项（如 Material 的 Burlap）当已选值。数据模型注记：Quantity 在简单变体上；**Size/Color 是配置型「区分属性」、设在每个变体自己身上**（变体名后缀 -SIZE-COLOR 即来源，下钻变体详情页可直接读到，如 1182=S、1478=XS）；但 **Material 不是区分属性、只挂在配置型父产品上**，按 qty 筛出的变体自身 Material 多为空（`selectedIndex=-1`），真值在父产品（SKU 去 -SIZE-COLOR 后缀）。所以走方案 B 前先分清：目标属性是 Size/Color → 变体详情页能直接读；是 Material → 需 variant→parent 多跳，无单网格 UI 路径（已知难题，勿堆通用多跳逻辑）。
- 步骤：
1. 进入 Catalog > Products，按 Quantity 精确筛选（From=To=N）
2. 判断目标属性是否为 Columns 面板可选列
3. 是网格列（如 Color）：启用该列后 foreach 网格直采
4. 非网格列（如 Material/Size）：foreach 逐行下钻详情页读该属性
5. data_query 过滤非空、去重，按 intent 输出

## skill：Grid 数据导出或采集
- 触发：需要完整 grid 数据、导出 CSV/XML、跨分页统计
- 数据：目标 grid、所需列、筛选口径、分页范围
- 步骤：
1. 到达目标 grid 数据源
2. 明确所需列和筛选口径
3. 优先取得完整导出数据
4. 无法导出时分页采集可见行
5. 对完整数据执行统计或筛选
