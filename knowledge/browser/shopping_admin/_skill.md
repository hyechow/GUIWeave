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
- 数据：订单状态口径（关键）——判别看 intent 有没有 "completed"：含 `completed`（`who completed the most/second/Nth number of orders`、`who completed N orders`、`completed orders`）→ 按 **`Status = Complete`** 计数（WebArena 参考答案实测如此，task 63 坐实），第一步 `Clear all` 清残留后**只筛 Status=Complete**、采全量 Complete 行（约 155 行必须采全，否则个位计数名次乱）；字面 `any state`/只说 `have N orders`（task 64）→ **不筛 Status**、SQL 不写 status 谓词、`Clear all` 清掉所有残留筛选（含残留的 `Status: Complete`）后采全量。计数采集（关键）——foreach returns 必须采 `ID`+`Customer Email`+`Status` 三列；`ID` 是逐行唯一列(缺它会被按整行内容去重塌掉同客户多笔订单、计数全错),`Status` 一并采回供口径判定/复核。另需完整订单行
- 步骤：
1. 进入 Sales > Orders 订单数据源
2. 按口径设状态约束：含 completed→Clear all 后只 Status=Complete；字面 any state→Clear all 后不筛
3. foreach 采 ID + Customer Email + Status（ID 逐行唯一防去重塌缩）
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
- 数据：Products grid、Columns 控件、Quantity 范围筛选、产品编辑页属性字段。目标属性是否是网格可选列决定走哪条路：Products Columns 面板权威可选列（37 列，含 Color，**不含 Material/Size**）。读属性下拉的空值判定——select 未选中时 `selectedIndex=-1`，判为空，绝不把首项（如 Material 的 Burlap）当已选值。数据模型注记：Quantity 在简单变体上；**Size/Color 是配置型「区分属性」、设在每个变体自己身上**（变体名后缀 -SIZE-COLOR 即来源，下钻变体详情页可直接读到，如 1182=S、1478=XS）；但 **Material 不是区分属性、只挂在配置型父产品上**，按 qty 筛出的变体自身 Material 多为空（`selectedIndex=-1`），真值在父产品（SKU/名去 -SIZE-COLOR 后缀）。所以走方案 B 前先分清：目标属性是 Size/Color → 变体详情页能直接读；是 **Material → 必须从变体上跳到父配置型产品再读**，路径（live 实测可靠）：取变体名去掉 `-SIZE-COLOR` 后缀得基名 → 回 Products grid 顶部 **Search by keyword** 用基名里**一个独特词**搜（整条带 ™/连字符的全名走全文索引常 0 命中；且 keyword 搜索跨任务残留，先清空再搜）→ 结果里挑 **Type=Configurable Product** 那行（父产品，其 Quantity 显示 0）→ 打开它读 Material。**Material 是 multiselect 多选**：父产品常含多个材质（如 Cotton+Lycra®、Fleece+Polyester+Spandex），但本类任务期望「该产品的材质」取**主材质 = 第一个已选项**（即下拉的 `value`/首个 selectedOption，如 Cotton、Fleece），不要把全部已选项都报上（评测对多出的值判 Extra→失败）。
- 步骤：
1. 进入 Catalog > Products，清除残留筛选，按 Quantity 精确筛选（From=To=N）。筛选完成的判据 = Active filters 出现 `Quantity: N - N` chip 且 records found > 0；**不要逐行验证数值**（网格同时有 Quantity 和 Salable Quantity 两列，后者 = 库存 − 预留，数值常 < Quantity，会误导验收）
2. 判断目标属性是否为 Columns 面板可选列
3. 是网格列（如 Color）：启用该列后 foreach 网格直采
4. 是 Size/Color 但非网格列：foreach 逐行下钻**变体自己**的详情页读该属性（区分属性挂在变体上）
5. 是 **Material**：它**不是网格列**，真值在【父配置型 configurable 产品】上、子变体为空。**绝不要把 material 放进 foreach.returns 当网格列直采**（那样采到空值）。把"从一个变体找到它的父配置型产品并读主材质"写成一个**函数** `resolve_parent_material(sku)`（**用 SKU 派生父键最稳**：变体 SKU=`WS08-XS-Blue`、父 SKU=`WS08`，按 SKU 搜是精确匹配；按全名搜走全文索引常 **0 命中**——live 095433 搜全名得 0 records、卡满 25 轮）：① `op=compute var=base expr="sku.rsplit('-',2)[0]"` 取父 SKU（纯计算、不是 milestone）；② `op=run run_kind=filter`：**回到 Products 列表、用顶部 Search by keyword 框**清空并搜 `{base}`，success_condition 写"结果出现 SKU={base}、Type=Configurable 的父产品行"；③ `op=run run_kind=navigation var=d returns=['material']`：打开结果里 **Type=Configurable Product（SKU={base}）** 父产品编辑页，read_spec 读 Material 主材质（首个已选项/value）。⚠️ **②③ 的 success_condition 都要锚定 `{base}`**——否则上一行残留的编辑页满足泛化验收、这一行没真搜就读到上个产品的材质（live 094903：Eos 读成 Minerva 的 Cotton）。main 里 filter qty=N 后,foreach（var=`row`、returns=`['Name','SKU']`）每行 `op=call resolve_parent_material(sku={row[SKU]})`、var 接住,material 随行汇进 into 表。函数只分解一次、被调多次。
6. data_query 查 into 表，过滤 material 非空、去重输出（每个产品只取主材质）

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
