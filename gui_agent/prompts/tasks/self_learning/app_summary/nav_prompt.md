---
id: task.self_learning.app_summary.nav_prompt
source_type: task_template
platform: shared
scope:
  - self_learning
owner: gui_agent.core.self_learning.app_summary
eval_suites:
rendered: true
version: 1
---
以下是「{app}」应用的 {n} 个页面知识文档：

{pages_text}

请生成应用级导航概览（仅页面结构和导航关系，不含 UI 元素细节）。
