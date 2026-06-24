# MobileWorld 跑测结果

通过 `bin/mobileworld <task>` 跑 MobileWorld 任务后,把结果归档为两部分:

- `reports/<TaskName>.html` —— **单个自包含可视化报告**(截图 WebP base64 内联,可直接进 git、浏览器打开即看图),用 `uv run python -m gui_agent.reports.inline <run_dir> -o benchmark/mobileworld/reports/<TaskName>.html` 生成。**提交**。
- `runs/<TaskName>/` —— 那次跑测的**完整日志**(原始截图 + `context.json` + 日志),从 `logs/` 拷过来。本地保留、**不提交**(已 gitignore,可随时从 `logs/` 重生)。

文件以任务名命名,无时间戳,重跑覆盖。GUI-only 子集见下表(默认目标,`bin/mobileworld --list` 列出 117 个任务;另有 `agent-mcp` / `agent-user-interaction` 两个子集)。打开报告看 MobileWorld 卡片(`score` / `SUCCESS|FAIL` / Goal / Reason),评分来自后端 `/task/eval` 的纯系统状态判定。

## GUI-only

| 任务 | score | 结果 | 目标 | 报告 |
| --- | --- | --- | --- | --- |
| OpenFlightModeTask | 1.0 | ✅ SUCCESS | Turn on device flight mode | [report](reports/OpenFlightModeTask.html) |

<!-- 新增一行: | <TaskName> | <score> | ✅/❌ | <goal> | [report](reports/<TaskName>.html) | -->
