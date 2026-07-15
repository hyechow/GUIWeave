---
id: context.statement.android.check.navigation
source_type: context_block
platform: android
scope:
  - checker
owner: gui_agent.adapters.android.supervisor.statement
eval_suites:
  - evals/android/checker
version: 1
---

## 导航类子目标（kind=navigation）
- done 仅当当前界面身份与目标界面精确匹配（顶部标题/当前 App 匹配、主内容区符合）。
- 判 done 时 reason 必须写清界面身份证据（标题文字、当前 App、关键区块）。
- 仍在导航途中、界面不匹配、加载中，一律 in_progress。
