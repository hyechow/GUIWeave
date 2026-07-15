---
id: context.statement.android.check.action
source_type: context_block
platform: android
scope:
  - checker
owner: gui_agent.adapters.android.supervisor.statement
eval_suites:
  - evals/android/checker
version: 1
---

## 动作类子目标（kind=action）
- `effect_status`：目标业务状态已有直接证据填 `confirmed`；目标当前尚未满足或仍在中间步骤填 `unmet`；本子目标动作派发后出现明确错误/拒绝填 `rejected`；缺少可判读反馈时填 `unverified`。
- done 仅当动作的预期效果已在界面上发生（成功提示/toast、目标值已改变、跳转到结果界面、弹窗关闭等）。
- ⚠️ 严格区分「可以提交」与「已提交」：按钮可见、表单已填、确认框弹出 → 仍是准备状态，判 in_progress；只有看到提交后的结果证据（成功提示、内容已出现、界面已跳转）才判 done。
