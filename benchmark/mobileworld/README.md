# MobileWorld 跑测结果

通过 `bin/mobileworld <task>` 跑 MobileWorld 任务后,把整份 run(`report.html` + 截图 + `context.json`)归集到这里,按子集分目录:

- `gui/` — GUI-only 子集(默认目标,`bin/mobileworld --list` 列出的 117 个任务)
- `mcp/` — Agent-MCP 子集(`agent-mcp` 标签)
- `user-interaction/` — Agent-User-Interaction 子集(`agent-user-interaction` 标签)

每个任务一个子目录(以任务名命名,无时间戳;重跑覆盖)。打开 `report.html` 看 MobileWorld 卡片(`score` / `SUCCESS|FAIL` / Goal / Reason),评分来自后端 `/task/eval` 的纯系统状态判定。

## GUI-only

| 任务 | score | 结果 | 目标 | 报告 |
| --- | --- | --- | --- | --- |
| OpenFlightModeTask | 1.0 | ✅ SUCCESS | Turn on device flight mode | [report](gui/OpenFlightModeTask/report.html) |

<!-- 新增一行: | <TaskName> | <score> | ✅/❌ | <goal> | [report](gui/<TaskName>/report.html) | -->
