---
id: task.orchestrator.redecomposer
source_type: task_template
platform: shared
scope:
  - orchestrator_redecomposer
owner: gui_agent.core.orchestrator.decomposer
schema: _PlanDraft
eval_suites:
  - evals/browser/redecompose
version: 2
---
# 中途语义重编排

本次只编译尚未完成的语义工作，不重做已完成 statement。把已完成 Outcome 当作事实和可用输入，
从当前 `main` 位置继续。

运行时纠正只否决被证明不可行的语义路线；不要把一个 Guard action rejection 升格为 Program 重编排。
若当前 Interact 仍可通过另一组页面内动作达到原 success，应由 Interact 自己重决策，不应改变 Program。

输出仍严格使用 `Interact / Data / Command / If / ForEach / Finish`。不要把纠正内容翻译成页面步骤、
SQL、控件规则、函数或运行时子编排。保留原任务的目标实体、目标值、范围和未完成副作用。
