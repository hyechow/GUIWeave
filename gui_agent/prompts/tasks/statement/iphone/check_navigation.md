---
id: context.statement.iphone.check.navigation
source_type: context_block
platform: iphone
scope:
  - checker
owner: gui_agent.adapters.iphone.supervisor.statement
eval_suites:
  - evals/iphone/checker
version: 1
---

## 导航类子目标（kind=navigation）
- done 仅当当前页面身份与目标页精确匹配（标题文字匹配、目标 tab 高亮选中）。
- 判 done 时，reason 必须写清页面身份证据（如标题文字、高亮 tab、关键分组名），不能空泛；visible_evidence 可附证据条目（可选）。
- 仍在导航途中、页面不匹配、加载中，一律 in_progress。
- 仅 in_progress 时 visible_evidence / missing_evidence 可留空，无需逐条列证据。
