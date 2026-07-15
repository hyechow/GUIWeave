---
id: context.statement.browser.check.converge
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.statement
eval_suites:
  - evals/browser/checker
version: 1
---

## 连续调值类子目标（completion_strategy=repeat_until_satisfied）
- 这类目标靠**多轮调整逐步逼近目标值**（如步进器加减、滑块拖动），未到目标是正常 in_progress，不是失败。
- done 仅当当前值与 success_condition 的目标值精确一致；in_progress 时在 missing_evidence 写出「当前值」「目标值」供规划器算步长。
- done 时 missing_evidence 必须为空。
