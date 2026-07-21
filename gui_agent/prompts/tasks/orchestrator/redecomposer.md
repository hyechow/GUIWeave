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
version: 5
---
# 中途语义重编排

本次只编译尚未完成的语义工作，不重做已完成 statement。把已完成 Outcome 当作事实和可用输入，
从当前 `main` 位置继续。

已完成事实中的大型集合只以 `bind + count + fields + coverage` 描述。它们的完整值仍保存在 Program
env 中；需要继续计算时必须由一个 Compute 按给出的原变量名通过 `inputs` 引用，禁止要求把记录正文复制进
上下文，也禁止另造 `rows`、`records` 等未声明变量。Read 不能消费这些集合。

运行时纠正只否决被证明不可行的语义路线；不要把一个 Guard action rejection 升格为 Program 重编排。
若当前 Interact 仍可通过另一组页面内动作达到原 success，应由 Interact 自己重决策，不应改变 Program。

输出仍严格使用 `Interact / Acquire / Read / SourceCheck / Compute / Command / If / ForEach / Finish`。不要把纠正内容翻译成页面步骤、
SQL、控件规则、函数或运行时子编排。保留原任务的目标实体、目标值、范围和未完成副作用。

同一直线 block 禁止连续 Compute。已有集合需要继续处理时，通过一个 Compute 的 inputs 引用它，并在该
Compute 内部完成全部线性变换、直接返回剩余 Program 消费的最终结果；不要输出中间工作表后再接第二个 Compute。

Interact 只纠正 UI 后置条件，不声明业务 bind/returns。若需要读取纠正后的当前页面事实，紧接 Read；
可读且不满足由 Program If 再进入纠正 Interact，不可读则由 Read 返回带证据的 kickback。若需要继续对
已完成集合做筛选、分组、聚合、排序或投影，使用一个 Compute，不要让 Read 处理集合。

若 Read kickback 证明当前 observation 缺少事实，不要重复同一失败 source；为剩余 Read 安排新的
Interact 后置条件再重读。若 Compute kickback 证明集合缺少语义字段或完整覆盖，修正 Compute 的
`coverage`、`required_fields` 或 semantic FieldRef；Compiler 会重新生成 inspect → Program If →
Interact → Acquire。列/视图暴露属于 Interact；同一已绑定集合内的滚动、翻页属于 Acquire。两者都不在
Program 中写具体控件或页面路径。

知识若包含命中目标字段的 `field_ownership` 合同，只修正剩余 UI scope；Compiler 会重新生成 identity
Compute、显式 owner If/ForEach 和输出 Compute，Read 仍只绑定当前 observation。

若检索 kickback/已完成事实表明完整 mention 或 Router search_hint 已确认零结果，把它当作 no-result 事实，
不要重复同一 lookup。需要尝试另一业务来源时才换 Program 路线；不得让 Statement 自行换语义字段。
