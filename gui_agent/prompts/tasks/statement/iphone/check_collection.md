---
id: context.statement.iphone.check.collection
source_type: context_block
platform: iphone
scope:
  - checker
owner: gui_agent.adapters.iphone.supervisor.statement
eval_suites:
  - evals/iphone/checker
version: 1
---

## 内容读取（kind=collection）
- 如果当前屏幕有与用户目标相关的可提取内容，填写 read_instruction
- in_progress 时 visible_evidence / missing_evidence 可留空
