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
- 动作类验收先分两种信号，不要混用：
  - `execution_signal`：动作是否已经派发/执行（运行时 block 权威）。
  - `effect_signal`：派发后页面是否给出 URL/DOM/视觉反馈（toast、目标值改变、跳转、弹窗关闭等）。
- `outcome_status` 必须独立填写：目标业务状态已有直接证据填 `confirmed`；页面明确显示错误、拒绝、校验失败或目标值不符填 `contradicted`；其余填 `unverified`。动作已派发、URL/DOM 有变化都不能单独把它写成 `confirmed`。
- 默认：若 success_condition 明确要求一个可见/可读的目标状态（例如字段值已经变成 X、列表出现某行、弹窗关闭、跳转到结果页），done 需要该目标状态已经发生。
- 终端派发动作例外：提交/保存/发送/发布/确认/应用/添加备注这类动作，若运行时 block 只证明 `execution_signal=dispatched`，当前页面又没有错误、校验失败、必填失败、权限失败等负反馈，则填 `outcome_status=unverified`，不要填 `contradicted/stuck`，也不要要求重复点击；是否以“已派发但未验证”推进由运行时决定。只有看到业务后置状态证据才能填 `confirmed`。
- ⚠️ 严格区分「可以提交」与「已提交」：按钮可见、表单已填、确认框弹出 → 仍是准备状态，判 in_progress；只有看到提交后的结果证据（成功提示、内容已出现、页面已跳转）才判 done。
- ⚠️ 搜索/筛选提交类 action 也遵守同一规则：若历史操作记录显示本子目标最近只是 type/填入关键词，之后没有点击 Search/Apply/Filter/Submit 或按回车提交，则输入框有目标值、页面仍有旧列表/旧计数都只能说明“已填写”，不能说明“已执行搜索/筛选”。此时判 in_progress，missing_evidence 写“需要提交搜索/应用筛选”。
