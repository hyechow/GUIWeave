---
id: context.milestone.browser.check.action
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.milestone
eval_suites:
  - evals/browser/checker
version: 1
---

## 动作类子目标（kind=action）
- done 仅当动作的预期效果已在页面上发生（成功提示/toast、目标值已改变、跳转到结果页、弹窗关闭等）。
- ⚠️ 严格区分「可以提交」与「已提交」：按钮可见、表单已填、确认框弹出 → 仍是准备状态，判 in_progress；只有看到提交后的结果证据（成功提示、内容已出现、页面已跳转）才判 done。
- ⚠️ 搜索/筛选提交类 action 也遵守同一规则：若历史操作记录显示本子目标最近只是 type/填入关键词，之后没有点击 Search/Apply/Filter/Submit 或按回车提交，则输入框有目标值、页面仍有旧列表/旧计数都只能说明“已填写”，不能说明“已执行搜索/筛选”。此时判 in_progress，missing_evidence 写“需要提交搜索/应用筛选”。
