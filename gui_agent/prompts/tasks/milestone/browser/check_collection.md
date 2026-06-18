---
id: context.milestone.browser.check.collection
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.milestone
eval_suites:
  - evals/browser/checker
version: 1
---

## 内容读取（kind=collection）
- 如果当前页面有与用户目标相关的可提取内容，填写 read_instruction。
- in_progress 时 visible_evidence / missing_evidence 可留空。
