# WebArena 跑测结果

用 `bin/webarena` 跑 WebArena-Verified 任务后,把结果归档为两部分:

- `reports/<task_id>.html` —— **单个自包含可视化报告**(截图 WebP base64 内联,可直接进 git、浏览器打开即看图),用 `uv run python -m gui_agent.reports.inline <run_dir> -o benchmark/webarena/reports/<task_id>.html` 生成。**提交**。
- `runs/<task_id>/` —— 那次跑测的**完整日志**(原始截图 + `context.json` + 日志),从 `logs/` 拷过来。本地保留、**不提交**(已 gitignore,可随时从 `logs/` 重生)。

评分来自 WebArena-Verified 官方评测器(`eval_result.json` 的 `score`),报告里以 WebArena 卡片展示。文件以 task_id 命名,无时间戳,重跑覆盖。

## shopping_admin

| task | score | 结果 | 目标 | 报告 |
| --- | --- | --- | --- | --- |
| 11 | 1.0 | ✅ | Get the total number of reviews that our store received so far that mention term… | [report](reports/11.html) |
| 15 | 1.0 | ✅ | Get the total number of reviews that our store received so far that mention term… | [report](reports/15.html) |
| 42 | 1.0 | ✅ | Get the top 2 search term(s) in my store | [report](reports/42.html) |
| 63 | 1.0 | ✅ | Get customer email(s) who completed the second most number of orders in the enti… | [report](reports/63.html) |
| 64 | 1.0 | ✅ | Get customer email(s) who have 2 orders in any state in the entire history | [report](reports/64.html) |
| 65 | 1.0 | ✅ | Get customer email(s) who completed the fifth most number of orders in the entir… | [report](reports/65.html) |
| 108 | 1.0 | ✅ | Get the monthly count of completed orders from January 2023 through May 2023, in… | [report](reports/108.html) |
| 110 | 1.0 | ✅ | Get the monthly count of completed orders from Jan 2022 through Nov 2022, inclus… | [report](reports/110.html) |
| 111 | 1.0 | ✅ | Get the monthly count of completed orders from Feb 2022 through Nov 2022, inclus… | [report](reports/111.html) |
| 113 | 1.0 | ✅ | Return the customer nickname(s) who gave a rating of 3 stars or below for Olivia… | [report](reports/113.html) |
| 116 | 1.0 | ✅ | Return the customer nickname(s) who gave a rating of 3 stars or below for tanks p… | [report](reports/116.html) |
| 127 | 1.0 | ✅ | Get the top 3 search terms that match available products in the store | [report](reports/127.html) |
| 157 | 1.0 | ✅ | View the details of all customers | [report](reports/157.html) |
| 184 | 1.0 | ✅ | Give me the name and color of the products that have 0 units left | [report](reports/184.html) |
| 185 | 1.0 | ✅ | Give me the material of the products that have 3 units left | [report](reports/185.html) |
| 186 | 1.0 | ✅ | Give me the product names and the sizes of the products that have 2-3 units lef… | [report](reports/186.html) |
| 193 | 1.0 | ✅ | Get the total payment amount of the last 2 completed orders | [report](reports/193.html) |
| 196 | 1.0 | ✅ | Get the payment difference between the last 4 cancelled orders and the last 4 c… | [report](reports/196.html) |
| 197 | 1.0 | ✅ | Get the total payment amount of the last 5 non-cancelled orders | [report](reports/197.html) |
| 200 | 1.0 | ✅ | Get the billing name of the oldest complete order | [report](reports/200.html) |
| 204 | 1.0 | ✅ | Get the product name and final price (low to high) of the most recent completed… | [report](reports/204.html) |
| 212 | 1.0 | ✅ | Get the customer name and email with phone number 555-229-3326 | [report](reports/212.html) |
| 345 | 1.0 | ✅ | How many reviews did our shop receive in Apr 2023? | [report](reports/345.html) |
| 375 | 1.0 | ✅ | Go to the Magento Luma theme settings page | [report](reports/375.html) |
| 701 | 1.0 | ✅ | Draft a new marketing price rule for Mother's day sale that offers $15 discount o… | [report](reports/701.html) |
| 702 | 1.0 | ✅ | Draft a new marketing price rule for Pride Month that offers 45% off on all produ… | [report](reports/702.html) |
| 703 | 1.0 | ✅ | Draft a new marketing price rule for Thanks giving sale that offers $40 discount … | [report](reports/703.html) |
| 707 | 1.0 | ✅ | Show the sales order report for last year (today is March 15, 2023) | [report](reports/707.html) |
| 708 | 1.0 | ✅ | Show the tax report for this year (today is March 15, 2023) | [report](reports/708.html) |
| 709 | 1.0 | ✅ | Show the orders report from May 1, 2021 to March 31, 2022 | [report](reports/709.html) |

## shopping（customer-facing）

客户侧 Magento 商店（端口 7770）。与 shopping_admin 的差异：**登录走 `X-M2-Customer-Auto-Login` header 自动登录**（`emma.lopez@gmail.com:Password.123`），跑前需先建立带登录态的 `--storage-state`（或 headed 模式手动登录）；**`--reset-instance` 不支持 shopping**（`_RESETTABLE_CONTAINERS` 仅含 shopping_admin），跑前需手工起好容器：

```bash
docker run -d --name webarena_verified_shopping \
  -p 7770:80 -p 7771:8877 \
  -e "WA_ENV_CTRL_EXTERNAL_SITE_URL=http://192.168.1.103:7770/" \
  am1n3e/webarena-verified-shopping:latest
docker exec webarena_verified_shopping env-ctrl init --base-url http://192.168.1.103:7770/
```

tasks 文件：`webarena-verified/output/shopping_hard_tasks.json`（56 个纯 shopping 任务；混合 reddit 的 671–675 已过滤）。跑法：

```bash
TASKS_FILE=webarena-verified/output/shopping_hard_tasks.json \
WA_HOST=192.168.1.103 bin/webarena 21
```

**运行顺序（按 P1→P6 难度递进，同模板聚类）**：

| 层 | task | 结果 | 目标 | 报告 |
| --- | --- | --- | --- | --- |
| P1 页面内只读 | 21 | | Get reviewer names mentioning small ear cups on current page | |
| P1 页面内只读 | 25 | | Get reviewer names mentioning print quality with ≤3 stars | |
| P1 页面内只读 | 163 | | Get review titles with 2 stars or below (current page) | |
| P1 页面内只读 | 165 | | Get review titles with 2 stars or below (current page) | |
| P1 页面内只读 | 166 | | Get review titles with 2 stars or below (current page) | |
| P1 页面内只读 | 124 | | Price range of wireless earphone (min/max) | |
| P1 页面内只读 | 125 | | Price range of teeth grinding mouth guard (min/max) | |
| P1 页面内只读 | 226 | | Price range of Amazon basic products (min/max) | |
| P1 页面内只读 | 387 | | Who gave 4–5 stars for EYZUTAK phone cases | |
| P1 页面内只读 | 388 | | Who gave 1–2 stars for EYZUTAK phone cases | |
| P2 导航/检索 | 269 | | Open women shoes category filtered under $25 | |
| P2 导航/检索 | 271 | | Open makeup remover category filtered under $46.99 | |
| P2 导航/检索 | 273 | | Open furniture with accent category filtered under $199 | |
| P2 导航/检索 | 325 | | All mouth night guard listings sorted by desc price | |
| P2 导航/检索 | 327 | | All iphone 12 phone case listings sorted by name | |
| P2 导航/检索 | 328 | | All iphone 12 phone case listings sorted by price | |
| P2 导航/检索 | 284 | | Product page: least expensive shoe storage ≥12 pairs | |
| P2 导航/检索 | 286 | | Product page: least expensive ssd hard drive ≥1TB | |
| P2 导航/检索 | 240 | | Product page: most expensive men's Uniforms, Work & Safety | |
| P3 订单历史 | 50 | | Complete orders past year: count + total (incl. shipping) | |
| P3 订单历史 | 96 | | Latest order status + arrival date | |
| P3 订单历史 | 191 | | Total cost of latest order marked "processing" | |
| P3 订单历史 | 235 | | Order number of most recent under-delivery order | |
| P3 订单历史 | 301 | | Open order details for most recent processing order | |
| P3 订单历史 | 142 | | Spent on hair care Jan 2023 (excl. shipping) | |
| P3 订单历史 | 143 | | Spent on home decoration Jan 2023 (excl. shipping) | |
| P3 订单历史 | 147 | | Size (width/height) of picture frame bought in 2022 | |
| P3 订单历史 | 148 | | Color of picture frame bought Sep 2022 | |
| P3 订单历史 | 149 | | Color of artificial plants bought Feb 2023 | |
| P3 订单历史 | 320 | | Refund from canceled orders Feb 2023 (incl. shipping) | |
| P3 订单历史 | 321 | | Refund from canceled orders in 2022 (incl. shipping) | |
| P3 订单历史 | 323 | | Refund from Mar 2022 canceled order (excl. shipping fee) | |
| P3 订单历史 | 335 | | Date I last ordered body butter | |
| P3 订单历史 | 337 | | Date I last ordered olive bread | |
| P3 订单历史 | 338 | | Date I last ordered toothpaste | |
| P4 轻写 | 519 | | Add current-page product to wishlist | |
| P4 轻写 | 520 | | Add current-page product to wishlist | |
| P4 轻写 | 466 | | Add 2× Hawaiian Bamboo Orchid Roots to wishlist | |
| P4 轻写 | 521 | | Subscribe to OneStopMarket newsletter | |
| P4 轻写 | 431 | | Add lowest per-unit-price product from open tabs to cart | |
| P4 轻写 | 432 | | Add lowest per-unit-price product from open tabs to cart | |
| P4 轻写 | 435 | | Add lowest per-unit-price product from open tabs to cart | |
| P5 表单/评价 | 528 | | Fill contact-us refund form (screen protector, don't submit) | |
| P5 表单/评价 | 529 | | Fill contact-us refund form (stereo system, don't submit) | |
| P5 表单/评价 | 530 | | Fill contact-us refund form (kitchen organizer, don't submit) | |
| P5 表单/评价 | 585 | | Rate floor lamp 5★ as Emma Lopez | |
| P5 表单/评价 | 586 | | Rate Jiffy Mix 4★ as ShoppingEmma | |
| P5 表单/评价 | 587 | | Rate PS3 accessory 3★ as GamingEmma | |
| P6 重写·购买 | 507 | | Buy highest-rated ceiling light (budget >1000, empty cart) | |
| P6 重写·购买 | 508 | | Buy highest-rated NS switch (budget <60, empty cart) | |
| P6 重写·购买 | 509 | | Buy best-rated men's shoe ≥5 reviews, least expensive | |
| P6 重写·改地址 | 571 | | Update my address to 231 Willow Way, Chicago | |
| P6 重写·改地址 | 572 | | Update my address to 654 Aspen Road, Boston | |
| P6 重写·改地址 | 795 | | Change 2nd-most-recent order delivery address | |
| P6 重写·改地址 | 797 | | Change first-order-ever delivery address | |
| P6 重写·改地址 | 798 | | Change most recent non-canceled order delivery address | |

排序理由：P1 纯读当前页（验证登录/感知基线）→ P2 导航+检索（筛选/排序机制）→ P3 账户订单历史（数据网格+SQL 聚合）→ P4 轻写（心愿单/订阅/加购）→ P5 表单/评价（填表+提交语义）→ P6 重写（购买流+地址变更，状态敏感，放最后）。同模板任务相邻，跑通一个即可复用机制。

## 已知问题/局限

- **task 185 "Give me the material of the products that have 3 units left"：已修复（端到端跑通），score 1.0（run `logs/gui_agent/webarena/browser/20260630_181240`）。** 官方 eval `retrieved_data = ['Fleece', 'Cotton']`。
  数据模型(CDP 直查 ground truth)：Magento 把 **Quantity 存在简单变体上、Material 存在配置型父产品上**。qty=3 命中两个简单变体（1478 `WS08-XS-Blue`、1182 `WH11-S-Blue`），它们**自身 Material 字段为空**（`selectedIndex=-1`）；期望答案 `Cotton/Fleece` 实为其配置型父产品（1492 `WS08` Minerva LumaTech V-Tee=Cotton、1194 `WH11` Eos V-Neck Hoodie=Fleece）的材质。没有任何一个产品同时满足"qty=3"且"material 有值"，WebArena 的 ground truth 本质是一条 DB join（variant.qty → parent.material）。曾被判为"admin 后台无干净单网格路径、不为它堆多跳逻辑"的难题——现已由 join 机制跑通：filter→foreach 采变体行 + 按 SKU 去 `-SIZE-COLOR` 后缀派生父 SKU + drill 父产品读 Material + 入口归一化到达步（机制见 [[webarena-185-material-multiselect-read]] 记忆条目，run 143407 首次端到端、181240 答案扁平且 eval 1.0）。附带修复两个通用 bug：① 属性下拉未选中时（`selectedIndex=-1`）误把首项 `Burlap` 当已选值（native_select DOM 权威，离线测 `tests/test_material_multiselect_read.py`）；② 答案 dict-wrap 未扁平。对照组 task 184（同模板，属性=Color）走通 score 1.0，因 Color 是 Products 网格可选列、无需跨变体/父产品。
- **task_type=navigate + AJAX 驱动的状态变更（如 679 "Go to the list of orders that are completed"）：评测器结构性盲区，非我们可修。**
  `NetworkEventEvaluator._filter_events_by_criteria`（`webarena-verified/src/.../network_event_evaluator.py`）对 `task.is_navigate_task`（数据集里固定的 ground truth，不受我们提交的 `agent_response.task_type` 影响）+ `expected.http_method=="GET"`（默认值）的任务，只认"最后一次真实整页文档导航"（`NetworkEvent.is_navigation_event`，要求 `Accept: text/html` 或 Sec-Fetch 三件套）。但 Magento 后台的 grid 筛选（如 sales_order_grid 设 `filters[status]=complete`）天生是纯 AJAX（`mui/index/render`，`Accept: application/json`），永远不满足 `is_navigation_event`，于是这条 evaluator 拿到的候选事件永远是页面本身的导航，跟 `expected.url`（指向 AJAX 端点）必然不匹配，跟我们筛选条件设没设对、清没清残留状态都无关。凡是"Magento 后台需要先设置筛选/排序才能到达某视图"且被标成 navigate 的任务，理论上都会撞同一个墙。
- **task 345 "How many reviews in Apr 2023"：已修复，score 1.0（4 turns）。**
  原 score 0.0 根因：① 跨任务残留筛选（task 15 `keyword=best` 叠在日期筛选上 → 0条）；② Magento 日期 picker 拦截 ISO 格式 → 写成今天日期；③ checker 靠 LLM 判断 "Search 是否提交" → 全量计数（351）被误认为未提交。修复：`active_filters_block` + `_type_intercept`/datepicker API + dispatch gate 从 LLM checker 中拆出（确定性：url_changed OR dom_changed → done）。答案：351。
- **无关筛选残留污染。**
  当前由 `FilterPredicateSet` 与 adapter 的 `AppliedFilterState` 做精确集合比较；缺少状态证据或存在额外谓词都不能通过 `constrain_collection`。回归见 `tests/test_constrain_gate.py`。
- **task 193 "last 2 completed orders total payment"：已修复，score 1.0（9 turns）。**
  连续失败根因已覆盖：① foreach 产出的 complete `completed_orders` 表与当前 DOM partial 表同时存在时，旧 `data_query` 完整性检查会被未引用的 partial sibling 阻塞；② `Grand Total (Purchased)` 是 `$106.00` 这类 UI 金额文本，直接 `SUM(grand_total_purchased)` 在 SQLite 中得到 `0.0`；③ `Purchase Date` 是 `Feb 3, 2023 6:08:03 PM` 这类人类日期文本，直接 `ORDER BY purchase_date` 是字典序。修复后运行时为可解析列暴露 typed shadows：`grand_total_purchased_num` 用于金额聚合，`purchase_date_ts` 用于日期排序；最终 SQL 为 `SELECT SUM(grand_total_purchased_num) AS total FROM (SELECT grand_total_purchased_num FROM completed_orders ORDER BY purchase_date_ts DESC LIMIT 2)`。本次官方 eval：answer/response 均为 `182.4`。
- **task 196 "last 4 cancelled vs completed payment difference"：已修复，score 1.0（11 turns，headed run `logs/gui_agent/webarena/browser/20260626_215131`）。**
  历史失败根因：① `SELECT SUM(...) FROM table LIMIT 4` 把 `LIMIT` 放在聚合之后，实际求了全状态全表总额；② `finish`/SQL 曾尝试用 `{var[field]}` 或 `a-b` 做差，产生负值或不可执行模板；③ foreach returns 写内部名 `created_at`，collect_fn 读不到日期列。修复后统一走：分别筛 `Status=Canceled`/`Complete` 并按 `Purchase Date` 降序，foreach body=[] 采集可见列 `Purchase Date` + `Grand Total (Purchased)`，最终 data_query 用 `purchase_date_ts` 子查询 `LIMIT 4` 后 `SUM(grand_total_purchased_num)`，再 `ABS(cancelled-completed)`。新增泛化兜底：data_query SQL 禁止 `{...}` 模板、禁止把前序 var 当 SQL 表名；聚合类任务禁止让 filter/action/read 目测读取当前可见网格行字段或手工相加。官方 eval：answer/response 均为 `194.25`。
- **task 197 "last 5 non-cancelled orders total payment"：已修复，score 1.0（3 turns，headed run `logs/gui_agent/webarena/browser/20260626_221207`）。**
  首跑失败 `logs/gui_agent/webarena/browser/20260626_220626`：decomposer 把 non-cancelled 写成 UI 负筛选「Status 不为 Canceled」，planner 只能在单值下拉里选择 `Complete`，导致数据源只剩完成订单；`data_query_repair` 正确拒绝口径不一致并返回 unknown_error。修复：prompt 增加通用否定约束规则（non-X/not X/excluding X 不可用单值下拉近似；未知是否有负筛选控件时采完整行后 SQL 排除），shopping_admin skill 明确 Status 是单值筛选、non-cancelled 不用 UI Status 下拉。正确路径是清除 active filters、foreach 采全量 Orders 的 `Status`/`Purchase Date`/`Grand Total (Purchased)`，SQL `WHERE lower(status) NOT LIKE '%cancel%' ORDER BY purchase_date_ts DESC LIMIT 5` 后外层 `SUM(grand_total_purchased_num)`。官方 eval：answer/response 均为 `778.2`。
- **task 200 "oldest complete order billing name"：score 1.0。**
  与 204 同族（订单详情查询），受益于同一批修复。
- **task 204 "most recent completed order product name + price"：已修复，score 1.0（5 turns）。**
  连续失败根因三层：① **SQLite CAST 静默归零**——`CAST('$45.00' AS REAL)` = 0.0（`$` 前缀导致），decomposer 生成的 `CAST(Price AS REAL)` 或 `CAST(REPLACE(Price,'$','') AS REAL)` 都得到错误结果。修复：decomposer.md 通用规则强制用 `_num` 影子列（已剥 `$`/`,`/`%` 并转 REAL），禁止对 UI 文本做 CAST/REPLACE 手动转换；`data_query_repair.py` 的 `_tables_profile` 改为显示影子列（repair LLM 可见 `price_num=45.0`）；skill 约束 ⑤ 明确 `price_num`。② **导航步 `kind=action`**——decomposer 把 URL 直达步生成为 `action`（应为 `navigation`），`_direct_nav_url()` 不触发，走 checker 路径时 PreExisting 误判（hostname 匹配但非详情页）。修复：decomposer.md run_kind 定义明确"凡 name 含 URL 模板一律 navigation"；skill 约束 ③ 明确 `run_kind=navigation` + `name="打开 {q[url]}"`。③ **`limit` 短路非首页行**——`read_grid_complete` 的 `limit=1` 在 viewport 检查前短路返回，grid 若停在第 5 页则采到第 5 页首行（错误订单）。修复：先检查 viewport page_index/has_prev_page，非首页时禁用 limit 让 TraversalController rewind + 全量采集，由 data_query ORDER BY 选正确行。
- **task 212 "customer name/email by phone number"：已修复，score 1.0。**
  失败根因：电话号存储为 `(555) 229-3326`（括号区号+空格），任务给的 `555-229-3326` 整串搜索 0 命中。修复：skill 明确用去区号后的本地号段（`229-3326`）做 keyword 子串搜索。
- **task 701/702/703 价格规则族（首批 MUTATE 写入任务）：全部 score 1.0。** 由 `NetworkEventEvaluator` 校验保存 POST（HAR 里的 `promo_*/save`），不再是读取类。这一族踩通了多条**通用**修复：
  - **Cart vs Catalog 判别**：`701`（$15 off **on checkout**）、`703`（$40 off **on checkout**）是 **Cart** Price Rule（`sales_rule/promo_quote/save`）；`702`（45% off **on all products**）是 **Catalog** Price Rule（`catalog_rule/promo_catalog/save`，另一套表单）。按 intent 关键词判别（"cart/checkout/purchase"→Cart、"on products/catalog"→Catalog），知识里补了两套 skill。
  - **多选控件**：`<select multiple>`（Customer Groups / Websites）选中态由 DOM `selected_text` 权威（native_select 常开列表框≠未选中）；"all registered customers"=逐个选 `General`+`Wholesale`+`Retailer`（无字面 "All Customers" 选项）。选择改"按 option 反查 select"+离屏控件标**滚动方向**（几何 rect），修长表单里 Websites 滚出视口选不上的打转。
  - **Feasibility 不得质疑规则类型**：折扣控件在默认收起的 `Actions` 折叠区（未展开不进 DOM 清单），判官曾据"控件不在清单"误判"在错的规则类型→踢去 Cart"，翻转正确进展。规则 7/8：类型 decompose 定死、判官不得改道；控件不在清单归因"折叠区/视口外"而非缺失（回归 case `evals/browser/feasibility` + `evals/browser/checker`）。
  - **HAR 保存 POST 捕获**：HarRecorder 曾被 302 重定向的目标 GET 覆盖掉 save POST（同 requestId），解锁整个 MUTATE 类的 NetworkEvent 评分。
  - **慢保存挂死（Catalog 特有）**：`catalog_rule/save` 触发 ~25-60s 同步重索引、期间 render 进程钉死。device 曾用 SIGALRM 给 raw CDP send 加墙钟帽——但信号打断 in-flight Playwright sync 调用会损坏 greenlet↔asyncio 桥、**后续调用永久挂**（playwright-python #1150，官方已知限制）。移除帽后 CDP 自然 block ~27s、重索引结束干净恢复（见 memory `playwright-never-signal-interrupt`；慢导航正确姿势=自然 block 或 `wait_for_url`/`wait_for_load_state` 原生等待，绝不用信号）。
