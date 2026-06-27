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
- 数据：Products grid、Columns 控件、Quantity 范围筛选、产品编辑页属性字段。目标属性是否是网格可选列决定走哪条路：Products Columns 面板权威可选列（37 列，含 Color，**不含 Material/Size**）。读属性下拉的空值判定——select 未选中时 `selectedIndex=-1`，判为空，绝不把首项（如 Material 的 Burlap）当已选值。数据模型注记：Quantity 在简单变体上；**Size/Color 是配置型「区分属性」、设在每个变体自己身上**（变体名后缀 -SIZE-COLOR 即来源，下钻变体详情页可直接读到，如 1182=S、1478=XS）；但 **Material 不是区分属性、只挂在配置型父产品上**，按 qty 筛出的变体自身 Material 多为空（`selectedIndex=-1`），真值在父产品（SKU 去 -SIZE-COLOR 后缀）。所以走方案 B 前先分清：目标属性是 Size/Color → 变体详情页能直接读；是 Material → 需 variant→parent 多跳，无单网格 UI 路径（已知难题，勿堆通用多跳逻辑）。
- 步骤：
1. 进入 Catalog > Products，按 Quantity 精确筛选（From=To=N）
2. 判断目标属性是否为 Columns 面板可选列
3. 是网格列（如 Color）：启用该列后 foreach 网格直采
4. 非网格列（如 Material/Size）：foreach 逐行下钻详情页读该属性
5. data_query 过滤非空、去重，按 intent 输出

## skill：按电话号查客户
- 触发：phone number、电话号查客户、find customer with phone、customer name/email/与电话相关的客户查找
- 数据：Customers grid 顶部 **Search by keyword**（全文**子串**匹配），不是 Filters 面板的 **Phone 列**精确筛选。电话在 Magento 存为带分隔符格式如 `(555) 229-3326`（括号区号 + 空格），任务给的 `555-229-3326` 这类纯连字符整串**不是存储值的连续子串**，整串搜（无论 keyword 还是 Phone 列）都 0 命中。能稳定命中的是去掉区号的**本地号段**（后 7 位，如 `229-3326`），它在各种分隔符格式下都连续。
- 步骤：
1. 进入 Customers > All Customers，先清除残留筛选
2. 用顶部 Search by keyword 搜本地号段（去区号后 7 位，如 `229-3326`）
3. 命中行读所需字段（Name、Email 等）输出

## skill：最近/最旧某状态订单的商品明细
- 触发：most recent/latest/oldest order、最近一笔/最新订单、order 的 product name + price、订单的商品和价格、一笔订单里所有商品、return a list of products/price of an order
- 数据：判定「最近/最旧」**只看 Orders grid 的 `Purchase Date` 列**（内部 `purchase_date_ts`），**绝不用订单详情页的 `Order Date`**——两者不同，详情日期会误导（task-204 的 decoy order 89 详情日期看着新，但 grid Purchase Date 排名靠后）。流程：先 filter（清残留筛选 + 设 Status 口径），foreach 全量采 `Action_url` + `Purchase Date`，data_query 按 `purchase_date_ts DESC/ASC LIMIT 1` 选出目标单的 `Action_url`，**URL 直达钻取，绝不硬编码/猜 order_id，也绝不靠「列表第一行」**（残留排序失效就会错）。钻到详情页后，商品明细在 **`Items Ordered` 表**（table_reader 已捕获，表头含 `Product`/`Price`）。**关键：一张订单常含多个商品，必须读全部行——所以钻取和读商品是两步，不是一步。** 钻取那一步只是 navigation（打开 `{选出的 url}`），**该步 returns 留空、绝不在打开详情这一步上挂 `returns=['Product','Price']` 去读商品**（那是「单行标量详情读」的写法，只会读到第一个商品，漏掉其余）。打开详情后，**另起一个独立的 foreach（body=[], returns=`['Product','Price']`）**采这张详情表（`_best_table` 按 returns 自动从详情页 4 张表里选中 Items Ordered）。该表因 rowspan 会拆出 `Price` 为空的幻影行，且 `Product` 单元格形如 `Ida Workout Parachute Pant SKU: WP03-28-Blue ...`——data_query 里 `WHERE Price != ''` 过滤幻影行、商品名取 `Product` 列 `SKU:` 之前的部分、`Price`（如 `$45.00`）转数值，按 intent（low to high 等）排序后输出商品列表。
- 步骤：
1. 进 Orders，清筛 + 设 Status + 按 Purchase Date 排序
2. foreach 全量采行，data_query 按日期选目标单链接
3. navigation URL 直达钻到该订单详情页（此步 returns 留空，不读商品）
4. 另起独立 foreach 采 Items Ordered 表 Product+Price 读全部行
5. data_query 滤空价幻影行、剥 SKU、转价、排序，输出商品列表

## skill：Grid 数据导出或采集
- 触发：需要完整 grid 数据、导出 CSV/XML、跨分页统计
- 数据：目标 grid、所需列、筛选口径、分页范围
- 步骤：
1. 到达目标 grid 数据源
2. 明确所需列和筛选口径
3. 优先取得完整导出数据
4. 无法导出时分页采集可见行
5. 对完整数据执行统计或筛选
