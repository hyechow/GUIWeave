---
id: context.milestone.android.check.action
source_type: context_block
platform: android
scope:
  - checker
owner: gui_agent.adapters.android.supervisor.milestone
eval_suites:
  - evals/android/checker
version: 1
---

## 动作类子目标（kind=action）
- done 仅当动作的预期效果已在界面上发生（成功提示/toast、目标值已改变、跳转到结果界面、弹窗关闭等）。
- ⚠️ 严格区分「可以提交」与「已提交」：按钮可见、表单已填、确认框弹出 → 仍是准备状态，判 in_progress；只有看到提交后的结果证据（成功提示、内容已出现、界面已跳转）才判 done。
- 对「动作已发出且界面给出响应/本步不判定结果取值」这类 dispatch 响应门，必须结合历史操作记录判断：如果当前子目标最近一轮已执行提交/发送/保存/创建等动作，且当前界面已经从编辑/表单/撰写页跳转到列表页、收件箱、详情页、主页、加载页或其他稳定页面，就视为动作已有响应，判 done；不要因为当前页不再显示原表单、看不到成功 toast、或无法重新确认原字段内容而判 in_progress。
