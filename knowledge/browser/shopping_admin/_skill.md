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
- 数据：订单数据源 = Sales > Orders。**口径判别看 intent 有没有 "completed"**：含 `completed`（`who completed the most/second/Nth number of orders`、`who completed N orders`、`completed orders`）→ 按 **`Status = Complete`** 计数，先清残留筛选、只筛 Status=Complete 后采全量 Complete 行；字面 `any state` / 只说 `have N orders` → **不筛 Status**，清掉所有残留筛选（含残留的 `Status: Complete`）后采全量。**计数单位是「每一笔订单」**：同一客户的多笔订单是多笔、不能并成一笔，因此要采到订单的唯一标识（Orders grid 的 **ID** 列）以及 **Customer Email**、**Status**，否则同客户多笔订单会被并行折叠、人均计数偏小、排名错乱。
- 步骤：
1. 进入 Sales > Orders 订单数据源
2. 按口径设状态约束：含 completed→清残留后只筛 Status=Complete；字面 any state→清残留后不筛状态
3. 采全量订单行，含 Order ID（逐笔唯一）、Customer Email、Status
4. 按 Customer Email 聚合订单数
5. 输出满足排名或数量条件的邮箱

## skill：订单支付金额/最近 N 订单聚合
- 触发：payment amount、Grand Total、last N orders、completed/canceled/cancelled orders、non-cancelled orders、payment difference
- 数据：Orders grid；可见列 `Status`、`Purchase Date`、`Grand Total (Purchased)`。Status 是单值筛选；non-cancelled 不用 UI Status 下拉，不用 Complete 近似。foreach returns 用可见列名，不用内部名 `created_at`；SQL 再用 `purchase_date_ts` 和 `grand_total_purchased_num`。
- 步骤：
1. 进入 Sales > Orders
2. non-cancelled 禁用 UI Status；清筛后采全量
3. foreach body=[] 采 Status、Date、Grand Total
4. data_query 用 purchase_date_ts LIMIT N 后聚合
5. 多口径用 CTE/ABS；SQL 排除 Canceled

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
- 数据：Products grid、Columns 控件、Quantity 范围筛选、产品编辑页属性字段。目标属性是否是 grid 可选列决定走哪条路——Products Columns 面板可选列含 **Color**，**不含 Material / Size**。Magento 产品属性可能挂在**产品自身**，也可能挂在其**父 Configurable 产品**上：Size/Color 是配置型「区分属性」、设在每个变体自身（变体名后缀 `-SIZE-COLOR` 即来源，变体编辑页可直接读到）；**Material 在本数据集常由 Configurable 父产品承载**，按 qty 筛出的简单变体自身 Material 多为空值。父产品的 identity 是 **SKU**（= 变体 SKU 去掉 `-SIZE-COLOR` 后缀，如 `WS08-XS-Blue → WS08`）且 **Type = Configurable Product**，不是产品名——按名称搜会命中同系列变体。**Material 是 multiselect 多选属性**，父产品常含多个材质（如 Cotton+Lycra®、Fleece+Polyester+Spandex）；当任务问「该产品的材质」（单数）时，答案取**主材质 = 首个已选材质**。
- 步骤：
1. 进入 Products，清除残留筛选，按 Quantity 精确筛选出候选产品
2. 目标属性是 grid 可选列（如 Color）→ 启用该列后直接从 grid 读
3. 目标属性不在 grid（如 Material）→ 逐个候选产品读取：先在**产品自身**编辑页读该属性；**仅当自身为空**才按父 SKU（去 `-SIZE-COLOR` 后缀）搜出 `Type=Configurable Product` 的父产品、读其属性（自身非空就用自身，不去父覆盖）
4. 汇总各产品属性，过滤空值、去重输出；Material 取主材质

## skill：按电话号查客户
- 触发：phone number、电话号查客户、find customer with phone、customer name/email/与电话相关的客户查找
- 数据：Customers grid 顶部 **Search by keyword**（全文**子串**匹配），不是 Filters 面板的 **Phone 列**精确筛选。电话在 Magento 存为带分隔符格式如 `(555) 229-3326`（括号区号 + 空格），任务给的 `555-229-3326` 这类纯连字符整串**不是存储值的连续子串**，整串搜（无论 keyword 还是 Phone 列）都 0 命中。能稳定命中的是去掉区号的**本地号段**（后 7 位，如 `229-3326`），它在各种分隔符格式下都连续。
- 步骤：
1. 进入 Customers > All Customers，先清除残留筛选
2. 用顶部 Search by keyword 搜本地号段（去区号后 7 位，如 `229-3326`）
3. 命中行读所需字段（Name、Email 等）输出

## skill：最近/最旧某状态订单的商品明细
- 触发：most recent/latest/oldest order、最近一笔/最新订单、order 的 product name + price、订单的商品和价格、一笔订单里所有商品、return a list of products/price of an order
- 数据：Orders grid（`Action_url`、`Purchase Date`）+ 订单详情页 `Items Ordered` 表（`Product`、`Price`）。约束：① 「最近/最旧」只看 grid `Purchase Date`，不看详情页 `Order Date`；② grid-collect foreach 必须含 `Purchase Date`，由 data_query `ORDER BY purchase_date_ts LIMIT 1` 选出 URL（不可直接用列表第一行）；③ 钻取是独立 navigation 步（run_kind=navigation、不是 action），name 写 `打开 {q[url]}`（运行时确定性导航）、returns 留空；④ 商品由第二个独立 foreach 采集（details 表含 rowspan 幻影行 Price=''；Product 格式为 `名称 SKU: ...`，用 `substr(product,1,instr(product,' SKU:')-1)` 取纯名称）；⑤ 价格数值运算用影子列 `price_num`（Price 带 `$` 前缀，不可 CAST）。
- 步骤：
1. 进 Orders，清筛 + 设 Status + 按 Purchase Date 排序
2. foreach limit=1 采 Action_url+日期，data_query LIMIT 1
3. `run_kind=navigation`、`name="打开 {q[url]}"`（name 里必须含 URL 模板 `{q[url]}`、运行时确定性导航）、returns 留空
4. 另起独立 foreach 采 Items Ordered 表 Product+Price 读全部行
5. data_query 用 price_num 滤幻影行(>0)、剥 SKU、排序输出

## skill：Grid 数据导出或采集
- 触发：需要完整 grid 数据、导出 CSV/XML、跨分页统计
- 数据：目标 grid、所需列、筛选口径、分页范围
- 步骤：
1. 到达目标 grid 数据源
2. 明确所需列和筛选口径
3. 优先取得完整导出数据
4. 无法导出时分页采集可见行
5. 对完整数据执行统计或筛选
