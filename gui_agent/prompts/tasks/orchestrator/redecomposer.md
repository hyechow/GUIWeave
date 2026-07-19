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
version: 3
---
# 中途语义重编排

本次只编译尚未完成的语义工作，不重做已完成 statement。把已完成 Outcome 当作事实和可用输入，
从当前 `main` 位置继续。

运行时纠正只否决被证明不可行的语义路线；不要把一个 Guard action rejection 升格为 Program 重编排。
若当前 Interact 仍可通过另一组页面内动作达到原 success，应由 Interact 自己重决策，不应改变 Program。

输出仍严格使用 `Interact / Acquire / Data / Command / If / ForEach / Finish`。不要把纠正内容翻译成页面步骤、
SQL、控件规则、函数或运行时子编排。保留原任务的目标实体、目标值、范围和未完成副作用。

若 Data/Acquire kickback 证明当前数据源缺少语义字段或完整覆盖，不要让 Data 再读同一 terminal frame，
也不要重复同一失败 source。为剩余 Data 声明正确的 `coverage`、原始 `required_fields` 和新的语义
`prepare_source`；Compiler 会重新生成 inspect → Program If → Interact → Acquire。列/视图暴露属于
Interact；同一已绑定集合内的滚动、翻页属于 Acquire。两者都不在 Program 中写具体控件或页面路径。

若检索 kickback/已完成事实表明完整 mention 或 Router search_hint 已确认零结果，把它当作 no-result 事实，
不要重复同一 lookup。需要尝试另一业务来源时才换 Program 路线；不得让 Statement 自行换语义字段。
