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
- 数据：评论数据源 = All Reviews，不要进入 Products List 或商品详情页找评论；All Reviews 评论行含 Product、**Title**、Review、行详情链接列 **Action_url**；**Detailed Rating**、**Nickname** 与 **Summary of Review** 在 Review Detail 详情页读取，不是 All Reviews grid/Columns 可直接启用的列；按产品查评论时，产品约束绑定 **Product** 字段/列；任务要 `title` 时优先用 All Reviews 的 **Title** 列；钻详情读 title 时才对应 **Summary of Review**，不是 Nickname，也不是页面标题 Edit Review。
- 步骤：
1. 到达 All Reviews 评论数据源。
2. Product 字段先精确值、再关键词筛候选。
3. row_fields: Product、Title、Action_url。
4. 打开 Action_url 读 Detailed Rating。
5. SQL: `title AS title`。
6. 按评分条件筛选，按任务输出 Nickname 或 title/rating。

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

## skill：改配置型商品某（些）变体的价格
- 触发：increase/reduce/change the price of [white/blue/size L/XS] [product] by N%/$N；改某颜色/尺寸变体的价格、给某商品某变体调价
- 数据：某个 size/color 变体是**独立的 Simple Product**（SKU=`基础SKU-SIZE-COLOR`），它自己有 **Price** 字段——改变体价 = 打开那个 Simple 变体的编辑页改 Price 并 Save。定位变体：Catalog > Products 用**基础商品名关键词**搜（**剥掉颜色/尺寸/类别词**，如 "white Ingrid Running"→搜 `Ingrid`、"blue running tshirt"→搜 `tshirt`），grid 里找 SKU/name 含目标 size+color 的 **Type=Simple Product** 变体行并打开；或从配置型父产品（`Type=Configurable Product`）的 **Configurations** 变体网格定位那一行。**关键纠正**：① "running"/"tshirt" 是名字/类目、**不是 `Type` 筛选**（`Type` 选项只有 Simple/Virtual/Bundle/Configurable/Grouped/Downloadable 商品类型，选 `Running` 必然 option not found）；② **颜色/尺寸是变体属性、绝不能进商品名搜索**（否则 0 records）；③ **绝不用变体页上的 Size/Pattern/Color 属性下拉去"切换变体"**——那不导航到别的变体，还会展开遮挡下方 Price 框。百分比调价按变体**现价**算（降 13.5% = 现价×0.865；涨 N% = 现价×(1+N/100)），$N 定额直接加减。**目标价依赖变体运行时的当前 Price、绝不能 upfront 定死或留空**——价格步骤必须写成「**先读该变体 Price 字段的当前值 → 按系数算新值 → 再把算出的新值填入 Price**」（如现价 75.00、降 13.5% → 填 64.88）；decompose 别把 Price 目标写成空或凭空猜一个数。批量（"size L 及以上"/"所有蓝色 XS"）：对每个匹配变体逐个打开、逐个读现价算新价改 Save。
- 步骤：
1. Catalog > Products，清残留筛选。
2. 按基础商品名关键词搜（剥颜色/尺寸/类别词）。
3. 定位目标 size/color 的 Simple 变体行并打开。
4. 读该变体 Price 当前值，按系数算新值，填入 Price。
5. Save；多个变体则逐个重复。

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

## skill：创建价格规则（先判 Cart vs Catalog，再按对应表单）
- 触发：create/new (marketing) price rule、价格规则、折扣规则、给某类客户 X% off / $N discount
- 数据：**先判规则类型**——折扣作用于购物车/结账/整单/一次购买（cart、checkout、purchase、on their cart）→ **Cart Price Rule**（Marketing > Cart Price Rules，存 `sales_rule/promo_quote/save`，用下面这套表单）；折扣作用于商品/目录（off on (all) products、on the catalog）→ **Catalog Price Rule**（Marketing > Catalog Price Rules，存 `catalog_rule/promo_catalog/save`，**另一套表单**：Conditions 按商品属性，别套 Cart 的 Coupon/Actions 字段）。判别关键：**"on all products" ≠ "on all their cart"**（前者 Catalog、后者 Cart）。**以下为 Cart Price Rule 表单**：Marketing > Cart Price Rules > Add New Rule。**必填字段有 Rule Name、Websites、Customer Groups、Coupon**，漏任一保存即校验失败。**Websites（`<select multiple>` name=website_ids）必填、默认空，必须选 `Main Website`（本数据集唯一站点）**。**Customer Groups（`<select multiple>` name=customer_group_ids）选项固定为 `NOT LOGGED IN`/`General`/`Wholesale`/`Retailer`，没有「All Customers」选项**——「all registered customers/所有已注册客户」= 逐个选中 `General`+`Wholesale`+`Retailer`（排除 `NOT LOGGED IN` 未登录访客），「all customers」= 4 组全选，绝不去选字面「All Customers」（不存在，会 `option not found` 打转）。Coupon 用默认 `No Coupon`。Rule Name 用任务原文；Active 设 Yes。**折扣配置在 `Actions` 折叠区（默认折叠、位于表单下方，`simple_action`/`discount_amount` 字段折叠时不在 DOM）——必须先点 `Actions` 区标题展开，字段才出现**：Apply(`simple_action`) 选 `Percent of product price discount`（X% off）或 `Fixed amount discount`/`Fixed amount discount for whole cart`（$N off），Discount Amount(`discount_amount`) 填数值（百分比填 15 不填 15%，定额填 40 不带 $）。
- 步骤：
1. 进 Marketing > Cart Price Rules，点 Add New Rule
2. 填 Rule Name、Active=Yes、Websites 选 Main Website
3. 在 Customer Groups 逐个选中对应客户组（组集见数据）
4. 展开 Actions 折叠区，设 Apply 折扣类型 + Discount Amount
5. Save

## skill：创建目录价格规则（Catalog Price Rule，作用于商品）
- 触发：X% off / $N off **on (all) products**、on the catalog、目录价格规则；即折扣作用于商品而非购物车（判别见上一条 Cart vs Catalog）
- 数据：Marketing > Catalog Price Rules > Add New Rule（存 `catalog_rule/promo_catalog/save`）。sections = Rule Information / Conditions / Actions。**必填：Rule Name、is_active、Websites、Customer Groups**（和 Cart 同款多选：`website_ids` 选 `Main Website`；`customer_group_ids` 的「all registered」= `General`+`Wholesale`+`Retailer`，无「All Customers」选项）。**没有 Coupon 字段**。**Conditions 区**：任务是「on all products / 全部商品」时**留空不加条件**（空条件=作用于全部商品）。**折扣在 `Actions` 折叠区（默认折叠，先点 Actions 区标题展开）**：Apply(`simple_action`) 选 `Apply as percentage of original`（X% off）或 `Apply as fixed amount`（$N off），Discount Amount(`discount_amount`) 填数值（45% 填 45）。
- 步骤：
1. 进 Marketing > Catalog Price Rules，点 Add New Rule
2. 填 Rule Name、Active=Yes、Websites 选 Main Website
3. 在 Customer Groups 逐个选中对应客户组
4. Conditions 留空（作用于全部商品）
5. 展开 Actions 折叠区，设 Apply（percentage/fixed）+ Discount Amount
6. Save

## skill：按订单号/客户定位订单（订单类改写的前置检索）
- 触发：order #N、update order #、notify … in their … order、订单 #、给某客户的订单做某操作
- 数据：Sales > Orders grid 顶部搜索框/筛选。**按订单号「#N」（如 `#304`）定位订单必须用 Filters 面板展开后的 `ID` 字段搜纯数字 `304`（去掉「#」），不要用顶部 `Search by keyword`**——increment id 存为零填充 `000000304`、顶部 keyword 是整词/fulltext 匹配，搜 `304` 或 `#304` 都 0 命中，会被误判成「订单不存在」而错误终止（499 实测：`#304`、keyword `304` 均空手而归，改用 **Filters → `ID` 字段搜 `304`** 才命中）。**订单号 0 命中不等于订单不存在**：MUTATE/改写类任务的目标订单几乎必然存在，先换 Filters→ID（或直达订单 view URL）确认，别据单次 keyword 0 命中就报 not found。找「某客户最近的 pending 订单」先按客户姓名检索 + Status 筛 `Pending`，再按 Purchase Date 取最新一笔，姓名精确 0 命中时退回姓/名关键词。**Orders grid 里不要写泛称「客户字段」或 `Customer Name` 作为筛选控件**：筛选面板实际可操作的是 `Bill-to Name`、`Ship-to Name`、`Customer Email`，顶部还有 `Search by keyword`；`Customer Name` 可能只是 Columns 里的可见列，不等于有文本筛选框。客户名定位优先用顶部 `Search by keyword` 搜完整姓名/关键词，或明确用 `Bill-to Name`/`Ship-to Name` 字段；不要通过 Columns 面板去“添加 Customer Name 筛选器”。**顶部 `Search by keyword` 的提交方式是按 Enter 或点击输入框内放大镜；Filters / Apply Filters 只提交展开面板里的列筛选，不能提交顶部 keyword。**
- 步骤：
1. 进 Sales > Orders
2. 按订单号时展开 Filters 面板、在 `ID` 字段填纯数字（去 #）后 Apply Filters；不要用顶部 keyword 搜订单号；0 命中先换法（Filters→ID / 直达 URL）再判不存在
3. 客户订单先搜完整姓名；0 命中再搜姓/名关键词
4. 追加 Status=Pending 时保留客户全名/关键词具体值
5. 按 Purchase Date 降序
6. 最近/最旧订单不要直接点当前第一行
7. foreach 采集客户名列、Status、Purchase Date
8. 同时采集 `Action_url` 作为详情入口
9. data_query 用客户关键词 + Status=Pending 过滤
10. 最近一笔用 `purchase_date_ts DESC LIMIT 1`
11. 打开 `Action_url` 执行改写（填单号 / 发通知）

## skill：给订单添加物流/追踪单号（update order with tracking number）
- 触发：update order # with (the) tracking number、add tracking (number)、USPS/UPS/FedEx/DHL tracking、给订单加物流单号/追踪号
- 数据：**追踪号挂在发货单（Shipment）上，不是订单 Comment**。提交后 POST `admin/order_shipment/save/order_id/<id>`，post_data 含 `tracking[k][carrier_code]` + `tracking[k][number]`。**绝不要把追踪号写进 Notes for this Order 的 Comment / 勾 Notify Customer**——那是客户通知（走 `sales/order/commentsHistory`），不产生 shipment，任务不算完成。路径（Method 1，订单尚无发货单，即有 **Ship** 按钮）：订单详情页 → 点顶部按钮栏 **Ship**（不是「Shipments 标签页」，也**没有**「Add New Shipment」按钮）→ New Shipment 页滚到 **Payment & Shipping Method** 区 → 点 **Add Tracking Number** 展开一行 → **Carrier** 选承运商全称（USPS=`United States Postal Service`、UPS=`United Parcel Service`、FedEx=`Federal Express`、`DHL`；任务写 "USPS" 时选 `United States Postal Service`，**别填字面 "USPS"**）→ 填 **Number**=追踪号（Title 可留承运商名默认）→ 点 **Submit Shipment** 保存。Method 2（订单已发货、无 Ship 按钮）：Sales > Shipments 打开该发货单编辑页 → Add Tracking Number → 选 Carrier + 填 Number → 点 **Add** → 保存。
- 步骤：
1. 先按订单号定位并打开订单详情（见上一条 skill：Filters→ID）
2. 有 Ship 按钮 → 点 **Ship** 进新建发货单页；无 Ship（已发货）→ 去 Sales > Shipments 打开该发货单编辑页
3. 滚到 Payment & Shipping Method 区，点 **Add Tracking Number**
4. Carrier 选承运商全称（USPS=United States Postal Service），Number 填追踪号
5. Ship 流点 **Submit Shipment**；已有发货单流点 **Add** 再保存
6. 验收：保存成功/跳转，且发货单里出现该追踪号——**不是** Comments History 里多一条备注
- 编排形态：这是**纯状态变更（无返回值诉求）**——意图只说「加追踪号」，没有「确认/返回/是否成功」这类取值要求。**Submit Shipment 这步不要绑 returns**（尤其别造 `submit_status`/`成功|失败` 这类验证返回：提交成功即跳回订单页，那里没有这种字段，只会读到订单 Status 列而出域打转）；成没成由该步的 durable 验收（保存成功/跳转+发货单出现追踪号）判定。**finish 用静态确认句**（如「已为订单 #N 添加追踪号 X」），不要写 `{...[submit_status]}` 之类读返回值的模板。仅当意图明确要求「确认是否成功/返回某值」时才另加 confirm-read。

## skill：给订单客户发送订单备注/通知
- 触发：notify/send/message customer in order、给订单客户发消息、通知某客户某订单
- 数据：订单详情页 view/detail 的 **Comments History / Notes for this Order** 区域包含写入表单；**不要点击 Edit 进入 `order_edit`**，Edit Order 是修改订单商品/地址/账号的页面，不是客户通知备注入口。`Comments History` 可作为定位入口/历史区；发给客户必须在 **Notes for this Order** 表单填写 **Comment**，勾选 **Notify Customer by Email**，然后点 **Update**（或 Submit Comment）。不要写“内部备注”，内部备注不会触发客户通知；任务说 notify/send/message customer 时必须产生客户通知。
- 步骤：
1. 打开目标订单详情页
2. 留在详情页，定位 **Comments History / Notes** 表单
3. **Comment** = 消息正文
4. **Notify Customer by Email** = yes
5. **Update** / **Submit Comment** 后出现成功提示
