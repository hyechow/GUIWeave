# WebArena 跑测结果

用 `bin/webarena` 跑 WebArena-Verified 任务后,把结果归档为两部分:

- `reports/<task_id>.html` —— **单个自包含可视化报告**(截图 WebP base64 内联,可直接进 git、浏览器打开即看图),用 `uv run python -m gui_agent.reports.inline <run_dir> -o benchmark/webarena/reports/<task_id>.html` 生成。**提交**。
- `runs/<task_id>/` —— 那次跑测的**完整日志**(原始截图 + `context.json` + 日志),从 `logs/` 拷过来。本地保留、**不提交**(已 gitignore,可随时从 `logs/` 重生)。

评分来自 WebArena-Verified 官方评测器(`eval_result.json` 的 `score`),报告里以 WebArena 卡片展示。文件以 task_id 命名,无时间戳,重跑覆盖。

## shopping_admin

| task | score | 结果 | 目标 | 报告 |
| --- | --- | --- | --- | --- |
| 11 | 1.0 | ✅ | Get the total number of reviews that our store received so far that mention term… | [report](reports/11.html) |
| 42 | 1.0 | ✅ | Get the top 2 search term(s) in my store | [report](reports/42.html) |
| 63 | 1.0 | ✅ | Get customer email(s) who completed the second most number of orders in the enti… | [report](reports/63.html) |
| 64 | 1.0 | ✅ | Get customer email(s) who have 2 orders in any state in the entire history | [report](reports/64.html) |
| 65 | 1.0 | ✅ | Get customer email(s) who completed the fifth most number of orders in the entir… | [report](reports/65.html) |
| 108 | 1.0 | ✅ | Get the monthly count of completed orders from January 2023 through May 2023, in… | [report](reports/108.html) |
| 110 | 1.0 | ✅ | Get the monthly count of completed orders from Jan 2022 through Nov 2022, inclus… | [report](reports/110.html) |
| 111 | 1.0 | ✅ | Get the monthly count of completed orders from Feb 2022 through Nov 2022, inclus… | [report](reports/111.html) |
| 113 | 1.0 | ✅ | Return the customer nickname(s) who gave a rating of 3 stars or below for Olivia… | [report](reports/113.html) |
| 116 | 1.0 | ✅ | Return the customer nickname(s) who gave a rating of 3 stars or below for tanks p… | [report](reports/116.html) |
| 157 | 1.0 | ✅ | View the details of all customers | [report](reports/157.html) |

## 已知问题/局限

- **task_type=navigate + AJAX 驱动的状态变更（如 679 "Go to the list of orders that are completed"）：评测器结构性盲区，非我们可修。**
  `NetworkEventEvaluator._filter_events_by_criteria`（`webarena-verified/src/.../network_event_evaluator.py`）对 `task.is_navigate_task`（数据集里固定的 ground truth，不受我们提交的 `agent_response.task_type` 影响）+ `expected.http_method=="GET"`（默认值）的任务，只认"最后一次真实整页文档导航"（`NetworkEvent.is_navigation_event`，要求 `Accept: text/html` 或 Sec-Fetch 三件套）。但 Magento 后台的 grid 筛选（如 sales_order_grid 设 `filters[status]=complete`）天生是纯 AJAX（`mui/index/render`，`Accept: application/json`），永远不满足 `is_navigation_event`，于是这条 evaluator 拿到的候选事件永远是页面本身的导航，跟 `expected.url`（指向 AJAX 端点）必然不匹配，跟我们筛选条件设没设对、清没清残留状态都无关。凡是"Magento 后台需要先设置筛选/排序才能到达某视图"且被标成 navigate 的任务，理论上都会撞同一个墙。
- **`PreExisting` 判定逻辑不检查无关筛选残留 — 待收紧（checker 设计缺口，可修）。**
  `SingleCheck`/`PreExisting` 这类"目标条件已经满足，跳过动作"的判定，目前只验证"任务要求的筛选条件存在"，不验证"除此之外没有任务未要求的筛选/排序条件也在生效"。Magento 后台的 grid 筛选状态按管理员账号持久化在服务端，跨任务运行会互相污染（679 复测时就曾带着上一条任务残留的 `created_at` 日期筛选）；即使最终截图看起来"对"，agent 据此读出的记录数/数据可能已被无关筛选污染而不自知。后续如果要收紧，方向是让 PreExisting 类判定额外比对"当前活跃筛选集合"与"任务要求的筛选集合"是否完全一致，而不只是"要求的那条存在"。
