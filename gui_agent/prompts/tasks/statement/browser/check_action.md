---
id: context.statement.browser.check.action
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.statement
eval_suites:
  - evals/browser/checker
version: 3
---

## 动作类子目标（kind=action）
- 动作类验收先分两种信号，不要混用：
  - `execution_signal`：动作是否已经派发/执行（运行时 block 权威）。
  - `effect_signal`：派发后页面是否给出 URL/DOM/视觉反馈（toast、目标值改变、跳转、弹窗关闭等）。
- `effect_status` 必须独立填写：目标业务状态已有直接证据填 `confirmed`；目标当前尚未满足、仍在中间步骤、缺少目标成员或外层 Save/Submit 尚未执行填 `unmet`；本子目标动作派发后页面明确显示错误、拒绝、校验失败或可靠后置状态证明操作失败填 `rejected`；当前帧没有可判读的目标结果通道填 `unverified`。
- `unmet` 是正常执行状态，不表示动作失败，也不得触发换路线：首次进入子目标、滚动/展开/填写等过程动作之后，最终目标仍未出现都属于 `unmet`。`rejected` 必须能点名本子目标中已经派发的动作及其失败证据；没有本子目标动作历史时禁止填写 `rejected`。
- 默认：若 success_condition 明确要求一个可见/可读的目标状态（例如字段值已经变成 X、列表出现某行、弹窗关闭、跳转到结果页），done 需要该目标状态已经发生。
- 终端派发动作例外：提交/保存/发送/发布/确认/应用/添加备注这类动作，若运行时 block 只证明 `execution_signal=dispatched`，且当前帧**没有提供目标结果通道或结果仍不可判读**，则填 `effect_status=unverified`，不要仅因缺少 toast 要求重复点击；是否以“已派发但未验证”推进由运行时决定。提交后若权威目标通道明确显示目标仍不成立，或页面显示错误/拒绝/校验失败，填 `rejected`。只有看到业务后置状态证据才能填 `confirmed`。
- ⚠️ 严格区分「可以提交」与「已提交」：按钮可见、表单已填、确认框弹出 → 仍是准备状态，判 in_progress；只有看到提交后的结果证据（成功提示、内容已出现、页面已跳转）才判 done。
- ⚠️ 搜索/筛选提交类 action 也遵守同一规则：若历史操作记录显示本子目标最近只是 type/填入关键词，之后没有点击 Search/Apply/Filter/Submit 或按回车提交，则输入框有目标值、页面仍有旧列表/旧计数都只能说明“已填写”，不能说明“已执行搜索/筛选”。此时判 in_progress，missing_evidence 写“需要提交搜索/应用筛选”。
