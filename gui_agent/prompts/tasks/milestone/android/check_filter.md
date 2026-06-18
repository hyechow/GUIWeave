---
id: context.milestone.android.check.filter
source_type: context_block
platform: android
scope:
  - checker
owner: gui_agent.adapters.android.supervisor.milestone
eval_suites:
  - evals/android/checker
version: 1
---

## 筛选/搜索类子目标（kind=filter）
判 done 必须同时满足：
1. 当前是结果页，不是自动补全/建议页、历史页或加载页
2. 搜索框或界面显示完整的目标查询/筛选条件
3. 界面显示与查询对应的结果列表或内容
⚠️ 搜索建议页 vs 结果页：若搜索框仍处于输入/激活状态（软键盘还弹着）、下方是自动补全建议（带放大镜/历史图标、无详情元素），即使出现目标词也判 in_progress；只有已提交（回车或点搜索）进入独立结果页才判 done。
⚠️ 即使 in_progress，也在 missing_evidence 写出「当前值」与「目标值」，供规划器调整。
