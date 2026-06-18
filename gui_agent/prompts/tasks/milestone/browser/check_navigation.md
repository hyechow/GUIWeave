---
id: context.milestone.browser.check.navigation
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.milestone
eval_suites:
  - evals/browser/checker
version: 1
---

## 导航类子目标（kind=navigation）
- done 仅当当前页面身份与目标页精确匹配（若有页面标题则据之匹配，或页内页头/主内容区/页面特有元素符合）。
- 判 done 时 reason 必须写清页面身份证据（若有则用页面标题，或页内页头/关键可见区块）。
- 仍在导航途中、页面不匹配、加载中，一律 in_progress。
