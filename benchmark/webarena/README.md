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
| 185 | 0.0 | ❌ | Give me the material of the products that have 3 units left | [report](reports/185.html) |
| 186 | 1.0 | ✅ | Give me the product names and the sizes of the products that have 2-3 units lef… | [report](reports/186.html) |
| 193 | 1.0 | ✅ | Get the total payment amount of the last 2 completed orders | [report](reports/193.html) |
| 196 | 1.0 | ✅ | Get the payment difference between the last 4 cancelled orders and the last 4 c… | [report](reports/196.html) |
| 197 | 1.0 | ✅ | Get the total payment amount of the last 5 non-cancelled orders | [report](reports/197.html) |
| 345 | 1.0 | ✅ | How many reviews did our shop receive in Apr 2023? | [report](reports/345.html) |

## 已知问题/局限

- **task 185 "Give me the material of the products that have 3 units left"：筛选维度与目标属性分挂在变体/父产品，无单网格 UI 路径（已知难题，非我们 agent 可干净走到；[report](reports/185.html)）。**
  CDP 直查 ground truth 确认：Magento 把 **Quantity 存在简单变体上、Material 存在配置型父产品上**。qty=3 命中两个简单变体（1478 `WS08-XS-Blue`、1182 `WH11-S-Blue`），它们**自身 Material 字段为空**（`selectedIndex=-1`）；期望答案 `Cotton/Fleece` 实为其配置型父产品（1492 `WS08` Minerva LumaTech V-Tee=Cotton、1194 `WH11` Eos V-Neck Hoodie=Fleece）的材质。没有任何一个产品同时满足"qty=3"且"material 有值"，WebArena 的 ground truth 本质是一条 DB join（variant.qty → parent.material），admin 后台**没有干净的单网格路径**能得到。需 variant→parent 多跳（按 SKU 去 `-SIZE-COLOR` 后缀定位父产品），但该能力窄而脆（基本只服务这一 `{{Attribute}} of products with {{N}} units left` 模板族、且易误泛化），暂不为它堆通用多跳逻辑；skill 里仅留一行数据模型注记。附带修复了一个通用读值 bug：属性下拉未选中时（`selectedIndex=-1`）误把首项 `Burlap` 当已选值。对照组 task 184（同模板，属性=Color）走通 score 1.0，因为 Color 是 Products 网格可选列、无需跨变体/父产品。
- **task_type=navigate + AJAX 驱动的状态变更（如 679 "Go to the list of orders that are completed"）：评测器结构性盲区，非我们可修。**
  `NetworkEventEvaluator._filter_events_by_criteria`（`webarena-verified/src/.../network_event_evaluator.py`）对 `task.is_navigate_task`（数据集里固定的 ground truth，不受我们提交的 `agent_response.task_type` 影响）+ `expected.http_method=="GET"`（默认值）的任务，只认"最后一次真实整页文档导航"（`NetworkEvent.is_navigation_event`，要求 `Accept: text/html` 或 Sec-Fetch 三件套）。但 Magento 后台的 grid 筛选（如 sales_order_grid 设 `filters[status]=complete`）天生是纯 AJAX（`mui/index/render`，`Accept: application/json`），永远不满足 `is_navigation_event`，于是这条 evaluator 拿到的候选事件永远是页面本身的导航，跟 `expected.url`（指向 AJAX 端点）必然不匹配，跟我们筛选条件设没设对、清没清残留状态都无关。凡是"Magento 后台需要先设置筛选/排序才能到达某视图"且被标成 navigate 的任务，理论上都会撞同一个墙。
- **task 345 "How many reviews in Apr 2023"：已修复，score 1.0（4 turns）。**
  原 score 0.0 根因：① 跨任务残留筛选（task 15 `keyword=best` 叠在日期筛选上 → 0条）；② Magento 日期 picker 拦截 ISO 格式 → 写成今天日期；③ checker 靠 LLM 判断 "Search 是否提交" → 全量计数（351）被误认为未提交。修复：`active_filters_block` + `_type_intercept`/datepicker API + dispatch gate 从 LLM checker 中拆出（确定性：url_changed OR dom_changed → done）。答案：351。
- **无关筛选残留污染 — 已在 decomposer + checker 两侧收紧（提示层，2026-06-26）。**
  Magento 后台的 grid 筛选状态按管理员账号持久化在服务端，跨任务运行会互相污染（task 186 首跑就带着上一题残留的 `Keyword: WS08`，结果只剩 1/2 产品、数据被污染却报 done；679 复测也曾带 `created_at` 残留）。收紧方式：① **decomposer**（`decomposer.md` 规则 4「这条不限于 any/all 任务」段）——任何用页面筛选准备数据源的任务，filter 步骤的 name/success_condition 都必须包含"清除任务未要求的残留筛选/搜索/关键词/范围，使可见 active filters 恰好等于任务要求的集合"，不再只对 any/all 任务生效；② **checker**（`checker.md`）——当子目标 success_condition 含"无残留/恰好等于"措辞时，必须核验可见 active-filters chip 里没有任务未要求的额外筛选，有则判 in_progress。eval 锁：`evals/browser/orchestrator` 的 task 186 case + `filter_step_clears_residual_filters` 断言（×3 稳定通过）。**仍是提示层约束**（非确定性兜底）：`SingleCheck`/`PreExisting` 这类跳过判定本身没改、`ui_facts`/`active_filters` 感知路径仍是 dormant；若要再硬化，方向是让 PreExisting 确定性比对"当前活跃筛选集合 vs 任务要求集合"。
- **task 193 "last 2 completed orders total payment"：已修复，score 1.0（9 turns）。**
  连续失败根因已覆盖：① foreach 产出的 complete `completed_orders` 表与当前 DOM partial 表同时存在时，旧 `data_query` 完整性检查会被未引用的 partial sibling 阻塞；② `Grand Total (Purchased)` 是 `$106.00` 这类 UI 金额文本，直接 `SUM(grand_total_purchased)` 在 SQLite 中得到 `0.0`；③ `Purchase Date` 是 `Feb 3, 2023 6:08:03 PM` 这类人类日期文本，直接 `ORDER BY purchase_date` 是字典序。修复后运行时为可解析列暴露 typed shadows：`grand_total_purchased_num` 用于金额聚合，`purchase_date_ts` 用于日期排序；最终 SQL 为 `SELECT SUM(grand_total_purchased_num) AS total FROM (SELECT grand_total_purchased_num FROM completed_orders ORDER BY purchase_date_ts DESC LIMIT 2)`。本次官方 eval：answer/response 均为 `182.4`。
- **task 196 "last 4 cancelled vs completed payment difference"：已修复，score 1.0（11 turns，headed run `logs/gui_agent/webarena/browser/20260626_215131`）。**
  历史失败根因：① `SELECT SUM(...) FROM table LIMIT 4` 把 `LIMIT` 放在聚合之后，实际求了全状态全表总额；② `finish`/SQL 曾尝试用 `{var[field]}` 或 `a-b` 做差，产生负值或不可执行模板；③ foreach returns 写内部名 `created_at`，collect_fn 读不到日期列。修复后统一走：分别筛 `Status=Canceled`/`Complete` 并按 `Purchase Date` 降序，foreach body=[] 采集可见列 `Purchase Date` + `Grand Total (Purchased)`，最终 data_query 用 `purchase_date_ts` 子查询 `LIMIT 4` 后 `SUM(grand_total_purchased_num)`，再 `ABS(cancelled-completed)`。新增泛化兜底：data_query SQL 禁止 `{...}` 模板、禁止把前序 var 当 SQL 表名；聚合类任务禁止让 filter/action/read 目测读取当前可见网格行字段或手工相加。官方 eval：answer/response 均为 `194.25`。
- **task 197 "last 5 non-cancelled orders total payment"：已修复，score 1.0（3 turns，headed run `logs/gui_agent/webarena/browser/20260626_221207`）。**
  首跑失败 `logs/gui_agent/webarena/browser/20260626_220626`：decomposer 把 non-cancelled 写成 UI 负筛选「Status 不为 Canceled」，planner 只能在单值下拉里选择 `Complete`，导致数据源只剩完成订单；`data_query_repair` 正确拒绝口径不一致并返回 unknown_error。修复：prompt 增加通用否定约束规则（non-X/not X/excluding X 不可用单值下拉近似；未知是否有负筛选控件时采完整行后 SQL 排除），shopping_admin skill 明确 Status 是单值筛选、non-cancelled 不用 UI Status 下拉。正确路径是清除 active filters、foreach 采全量 Orders 的 `Status`/`Purchase Date`/`Grand Total (Purchased)`，SQL `WHERE lower(status) NOT LIKE '%cancel%' ORDER BY purchase_date_ts DESC LIMIT 5` 后外层 `SUM(grand_total_purchased_num)`。官方 eval：answer/response 均为 `778.2`。
