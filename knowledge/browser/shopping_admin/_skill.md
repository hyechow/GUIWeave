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
- 数据：Orders grid；可见列含 `Status`、`Purchase Date`、`Grand Total (Purchased)`。Status 是**单值**筛选。**non-cancelled / 非取消口径** = 排除 Status=Canceled 的订单：单值下拉选不出「非某值」，所以**不要**用 UI Status 下拉去近似，也**不要**用 Complete 近似 non-cancelled——清掉状态筛选采全量后，分析时排除 Canceled。
- 步骤：
1. 进入 Sales > Orders
2. non-cancelled：不用 UI Status 单值下拉，清筛后采全量、分析时排除 Canceled
3. 采集订单的 Status、Purchase Date、Grand Total
4. 按 Purchase Date 选出口径要求的订单（如最近 N 笔），再对 Grand Total 做求和/平均/差值
5. 多个口径对比（差值/比例）时分别备好各口径数据再统一比较

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
- 数据：All Reviews 评论行含 Product、Review ID、行详情链接列 **Action_url**；**Detailed Rating** 与 **Nickname** 在 Review Detail 详情页读取，不是 All Reviews grid/Columns 可直接启用的列；按产品查评论时，产品约束绑定 **Product** 字段/列。
- 步骤：
1. 到达 All Reviews 评论数据源。
2. Product 字段先精确值、再关键词筛候选。
3. 候选行采 ID、Product、Action_url。
4. 打开 Action_url 读 Detailed Rating、Nickname。
5. 按评分条件筛选并输出 Nickname。

## skill：按库存数量筛选产品并取某个属性（颜色/材质/尺码）
- 触发：产品的颜色/材质/尺码、color/material/size of products、name and color、products with N units left 取某属性；凡是「按库存数量筛选产品、再取该产品某个属性」的任务
- 数据：Products grid 行可采 **SKU** 与行详情链接列 **Action_url**；Products Columns 面板可选列含 **Color**，**不含 Material / Size**；Size/Color 是变体自身属性，变体名后缀 `-SIZE-COLOR` 即来源；**Material** 常由父 **Configurable Product** 承载，简单变体自身常为空；父产品 identity 是 **SKU**：变体 SKU 去 `-SIZE-COLOR` 后缀，如 `WS08-XS-Blue → WS08`；找父产品必须验证 **SKU=父SKU** 且 **Type=Configurable Product**；Material 是 multiselect，任务问单数材质时取首个已选值。
- 步骤：
1. 进入 Products；清残留；按 Quantity 筛候选。
2. Color：启用 Columns/Color 后从 grid 读。
3. Material：候选 foreach 采 SKU + Action_url。
4. Material：必须打开 `{row[Action_url]}` 读自身，禁止按 SKU 点行。
5. 父 SKU = `sku.rsplit('-', 2)[0]`，去掉尺码和颜色两段。
6. 搜父 SKU 后必须选 SKU=父SKU 且 Type=Configurable Product。
7. 不得只读自身后过滤空值；Material 空值必须回退父产品。
8. foreach/call 后必须汇总 material，过滤空值、去重输出。

## skill：按电话号查客户
- 触发：phone number、电话号查客户、find customer with phone、customer name/email/与电话相关的客户查找
- 数据：Customers grid 顶部 **Search by keyword**（全文**子串**匹配），不是 Filters 面板的 **Phone 列**精确筛选。电话在 Magento 存为带分隔符格式如 `(555) 229-3326`（括号区号 + 空格），任务给的 `555-229-3326` 这类纯连字符整串**不是存储值的连续子串**，整串搜（无论 keyword 还是 Phone 列）都 0 命中。能稳定命中的是去掉区号的**本地号段**（后 7 位，如 `229-3326`），它在各种分隔符格式下都连续。
- 步骤：
1. 进入 Customers > All Customers，先清除残留筛选
2. 用顶部 Search by keyword 搜本地号段（去区号后 7 位，如 `229-3326`）
3. 命中行读所需字段（Name、Email 等）输出

## skill：最近/最旧某状态订单的商品明细
- 触发：most recent/latest/oldest order、最近一笔/最新订单、order 的 product name + price、订单的商品和价格、一笔订单里所有商品、return a list of products/price of an order
- 数据：订单列表 grid（含每行 `Action_url`、`Purchase Date`）+ 订单详情页 `Items Ordered` 表（含 `Product`、`Price`）。约束：① 「最近/最旧」以 grid 的 **Purchase Date** 为准，不看详情页的 `Order Date`；按 Purchase Date 排序选出目标那一笔，**不要直接取列表第一行**（列表默认排序未必是日期序）；② 订单详情 `Items Ordered` 表含合计/小计类**无价格的幻影行**（Price 为空），统计商品时要剔除；`Product` 单元格是「商品名 + `SKU: …`」格式，取纯商品名要去掉 `SKU:` 及其后内容。
- 步骤：
1. 进 Orders，清筛、设 Status、按 Purchase Date 排序
2. 用 Purchase Date 排序选出目标订单（最近/最旧那一笔），拿到其详情页入口
3. 打开该订单详情页
4. 采集 Items Ordered 表的全部商品行（Product + Price）
5. 剔除无价格的幻影行、Product 取纯名称，按需排序输出商品与价格

## skill：Grid 数据导出或采集
- 触发：需要完整 grid 数据、导出 CSV/XML、跨分页统计
- 数据：目标 grid、所需列、筛选口径、分页范围
- 步骤：
1. 到达目标 grid 数据源
2. 明确所需列和筛选口径
3. 优先取得完整导出数据
4. 无法导出时分页采集可见行
5. 对完整数据执行统计或筛选

## skill：创建购物车价格规则（Cart Price Rule）
- 触发：create/new (marketing/cart) price rule、价格规则、折扣规则、给某类客户 X% off / $N discount
- 数据：Marketing > Cart Price Rules > Add New Rule 表单。**Customer Groups 是多选列表框（`<select multiple>`，name=customer_group_ids），选项固定为 `NOT LOGGED IN` / `General` / `Wholesale` / `Retailer`，没有名为「All Customers」的选项**——「all registered customers/所有已注册客户」= 逐个选中 `General`+`Wholesale`+`Retailer`（排除 `NOT LOGGED IN` 未登录访客），「all customers/所有客户」= 4 组全选，绝不去选字面「All Customers」（不存在，会 `option not found` 打转）。Rule Name 用任务原文；Active 设 Yes；折扣在 Actions 区：Apply 选 `Percent of Product Price Discount`（X% off）或 `Fixed Amount Discount`/`Fixed Amount Discount for Whole Cart`（$N off），Discount Amount 填数值（百分比填 15 不填 15%，定额填 40 不带 $）。
- 步骤：
1. 进 Marketing > Cart Price Rules，点 Add New Rule
2. 填 Rule Name、Active=Yes
3. 在 Customer Groups 逐个选中对应客户组（组集见数据）
4. 设 Apply 折扣类型 + Discount Amount
5. Save

## skill：按订单号/客户定位订单（订单类改写的前置检索）
- 触发：order #N、update order #、notify … in their … order、订单 #、给某客户的订单做某操作
- 数据：Sales > Orders grid 顶部搜索框/筛选。**订单引用「#N」（如 `#304`）搜索时必须去掉「#」，直接搜数字 `304`**（Orders grid 按订单号 increment id 匹配，带「#」会 0 命中，499 就是搜 `#304` 空手而归）；找「某客户最近的 pending 订单」先按客户姓名检索 + Status 筛 `Pending`，再按 Purchase Date 取最新一笔，姓名精确 0 命中时退回姓/名关键词。
- 步骤：
1. 进 Sales > Orders
2. 按订单号（去掉 #）或客户姓名 + Status=Pending 检索定位目标订单
3. 打开该订单执行改写（填 USPS 单号 / 发通知等）
