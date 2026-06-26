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
| 186 | 1.0 | ✅ | Give me the product names and the sizes of the products that have 2-3 units lef… | [report](reports/186.html) |

## 已知问题/局限

- **task 185 "Give me the material of the products that have 3 units left"：筛选维度与目标属性分挂在变体/父产品，无单网格 UI 路径（已知难题，非我们 agent 可干净走到）。**
  CDP 直查 ground truth 确认：Magento 把 **Quantity 存在简单变体上、Material 存在配置型父产品上**。qty=3 命中两个简单变体（1478 `WS08-XS-Blue`、1182 `WH11-S-Blue`），它们**自身 Material 字段为空**（`selectedIndex=-1`）；期望答案 `Cotton/Fleece` 实为其配置型父产品（1492 `WS08` Minerva LumaTech V-Tee=Cotton、1194 `WH11` Eos V-Neck Hoodie=Fleece）的材质。没有任何一个产品同时满足"qty=3"且"material 有值"，WebArena 的 ground truth 本质是一条 DB join（variant.qty → parent.material），admin 后台**没有干净的单网格路径**能得到。需 variant→parent 多跳（按 SKU 去 `-SIZE-COLOR` 后缀定位父产品），但该能力窄而脆（基本只服务这一 `{{Attribute}} of products with {{N}} units left` 模板族、且易误泛化），暂不为它堆通用多跳逻辑；skill 里仅留一行数据模型注记。附带修复了一个通用读值 bug：属性下拉未选中时（`selectedIndex=-1`）误把首项 `Burlap` 当已选值。对照组 task 184（同模板，属性=Color）走通 score 1.0，因为 Color 是 Products 网格可选列、无需跨变体/父产品。
- **task_type=navigate + AJAX 驱动的状态变更（如 679 "Go to the list of orders that are completed"）：评测器结构性盲区，非我们可修。**
  `NetworkEventEvaluator._filter_events_by_criteria`（`webarena-verified/src/.../network_event_evaluator.py`）对 `task.is_navigate_task`（数据集里固定的 ground truth，不受我们提交的 `agent_response.task_type` 影响）+ `expected.http_method=="GET"`（默认值）的任务，只认"最后一次真实整页文档导航"（`NetworkEvent.is_navigation_event`，要求 `Accept: text/html` 或 Sec-Fetch 三件套）。但 Magento 后台的 grid 筛选（如 sales_order_grid 设 `filters[status]=complete`）天生是纯 AJAX（`mui/index/render`，`Accept: application/json`），永远不满足 `is_navigation_event`，于是这条 evaluator 拿到的候选事件永远是页面本身的导航，跟 `expected.url`（指向 AJAX 端点）必然不匹配，跟我们筛选条件设没设对、清没清残留状态都无关。凡是"Magento 后台需要先设置筛选/排序才能到达某视图"且被标成 navigate 的任务，理论上都会撞同一个墙。
- **无关筛选残留污染 — 已在 decomposer + checker 两侧收紧（提示层，2026-06-26）。**
  Magento 后台的 grid 筛选状态按管理员账号持久化在服务端，跨任务运行会互相污染（task 186 首跑就带着上一题残留的 `Keyword: WS08`，结果只剩 1/2 产品、数据被污染却报 done；679 复测也曾带 `created_at` 残留）。收紧方式：① **decomposer**（`decomposer.md` 规则 4「这条不限于 any/all 任务」段）——任何用页面筛选准备数据源的任务，filter 步骤的 name/success_condition 都必须包含"清除任务未要求的残留筛选/搜索/关键词/范围，使可见 active filters 恰好等于任务要求的集合"，不再只对 any/all 任务生效；② **checker**（`checker.md`）——当子目标 success_condition 含"无残留/恰好等于"措辞时，必须核验可见 active-filters chip 里没有任务未要求的额外筛选，有则判 in_progress。eval 锁：`evals/browser/orchestrator` 的 task 186 case + `filter_step_clears_residual_filters` 断言（×3 稳定通过）。**仍是提示层约束**（非确定性兜底）：`SingleCheck`/`PreExisting` 这类跳过判定本身没改、`ui_facts`/`active_filters` 感知路径仍是 dormant；若要再硬化，方向是让 PreExisting 确定性比对"当前活跃筛选集合 vs 任务要求集合"。
