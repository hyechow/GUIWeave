---
id: context.milestone.iphone.check.action
source_type: context_block
platform: iphone
scope:
  - checker
owner: gui_agent.adapters.iphone.supervisor.milestone
eval_suites:
  - evals/iphone/checker
version: 1
---

## 动作类子目标（kind=action）
- done 仅当动作的预期效果已在屏幕上发生（弹窗关闭、目标值已改变、出现成功提示等）。
- in_progress 时 visible_evidence / missing_evidence 可留空。

## 发送/分享类（验收条件含「发送」「分享」「消息」）
⚠️ 严格区分「可以发送」与「已发送」：
- 发送按钮可见、联系人已选中、分享界面显示 → 仍是准备状态，必须判 in_progress
- 只有看到以下证据才能判 done：消息气泡出现在聊天记录中、发送成功 Toast 提示、"已发送"文字标识
- 不能将「发送按钮就绪」「界面已就绪」「视为发送状态」等推断作为 done 依据
