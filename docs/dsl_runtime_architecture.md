# DSL Program Runtime 与 Statement Executor

> **架构演进说明**：本文记录当前 DSL Runtime 与 Agentic Statement Transition 的实现边界。
> 下一阶段的正式收口目标是
> [语义执行架构：Program、Interact 与 Data](semantic_execution_architecture.md)：Program 只保留
> `Interact / Data / Command / If / ForEach / Finish`，当前 Read/Query/Compute 和
> navigation/filter/action 分类将按执行器边界归并。两份文档冲突时，以目标架构文档描述的演进方向
> 为准；本文仍用于解释尚未退役的现有代码。

GUI 任务是一份混合程序：控制流、计算和数据查询可以确定性执行，只有交互 statement
需要进入非确定性的 GUI 执行循环。架构边界因此按 **statement 执行方式** 划分，而不是按
“读/写”或某个 benchmark 的任务形态划分。

WebArena 用来验证这些边界和假设。通过某个 WebArena 任务不是架构目标；一个失败只有在揭示
可复用的类型、契约、执行器或恢复问题时，才应推动共享代码变化。站点事实留在 knowledge，
不能进入 core prompt 或 runtime。

## 顶层模型

```text
Goal
  -> Compiler                 自然语言 -> DSL Program
  -> Interpreter              执行控制流，维护 env，分派 statement
       -> Compute             确定性标量求值
       -> Immediate dispatcher
            -> Read           当前 observation 读取
            -> Query          表格查询
            -> Direct nav     确定性设备命令 fast path
       -> Interactive Run     交给 Statement Executor
            -> Memory + Observation -> LLM Transition -> Guard -> action/outcome
  -> StatementOutcome
  -> Interpreter resume
```

不存在独立的调用帧调度层。Python generator 的 `yield Run` / `send(StatementOutcome)` 已经是
Interpreter 暂停和恢复的边界，再引入一个有状态、有恢复策略的“frame”只会与 Interpreter
和 Statement 重叠。

## Program Compiler

Compiler 负责决定任务的语义结构：

- 目标实体、范围、基数和选择条件；
- statement 顺序、分支、循环和函数；
- 计算、查询及其数据依赖；
- 独立业务事务和持久化副作用的边界；
- 每个交互 Run 的 FROM、目标终态和声明返回值；
- 运行时证据证明原路线不可行后，对剩余 Program 重新分解。

Compiler 不描述坐标、滚动、页签、accordion、控件焦点或 click/type 序列。这些是当前页面
才能决定的执行细节。

## Program Interpreter

Interpreter 是所有 DSL statement 的统一运行时：

- 解释 `If`、`ForEach`、`Call`、`Finish`；
- 执行 `Compute`；
- 将无需 Statement loop 的 statement 交给 immediate dispatcher；
- 将交互 `Run` yield 给 Statement executor；
- 接收统一且不可变的 `StatementOutcome`，绑定 `var` 和 `returns`；
- 保存 `env`、`run_log` 和 materialized tables；
- 根据类型化失败决定继续、重试、重编排剩余 Program 或诚实失败。

跨 statement 的恢复预算和 `RecoveryLedger` 属于 Program runtime。它们不能放进 Statement，
也不能隐藏在一个旁路调用对象里。

`RecoveryRouter` 是无状态决策表：它只把 `StatementOutcome` 归类为 `AdvanceProgram`、
`TightenReturn`、`Kickback` 或 `FailOrEscalate`。预算消费、Program 替换和 recovery 记账仍由
`ProgramRuntime` 独占；loop 只执行 Router 给出的控制动作。

## Statement Executors

不同 statement 共享最小结果协议，但不强行共享不适用的执行状态。

```text
StatementOutcome:
  phase: completed | failed | exhausted | infeasible | interrupted
  verification: confirmed | accepted_unverified  # 仅 completed 可写
  reads
  rows
  summary
  evidence
  kickback  # 仅 infeasible 使用
```

`completed`/`failed` 不再是可独立写入的 bool。所有 immediate 与 interactive statement 都产出
同一个 `StatementOutcome`；Interpreter 不接受其他终态协议。外层 `AgentResult` 是 frozen 的
Program 结果投影，使用相同的 `phase/summary/output` 词表，并只在 CLI/API 边界 dump 成 JSON。
其中 `summary` 是运行终止结论，`output` 是面向用户的执行答案；pre-runtime failure 尚无独立答案
时二者允许同文。Chat 可在生成对话回复后只覆盖持久化 `ProgramOutcome.output`，原始
`AgentResult.output` 仍代表执行层产物，两者不构成两份终态状态。

- `Compute` 返回标量；
- `Read` 返回当前帧的结构化字段；
- `Query` 返回标量字段或行集；SQL 语法/列名自修复留在 Query executor，数据源口径不符则升级；
- 具体 URL/back 导航仍是交互 Run，但可以走 immediate navigation fast path；
- Interactive Run 返回 Statement 的终态证据及完成帧读值。

通用结果构造属于 Interpreter runtime。返回字段的缺失、类型域和 canonicalization 属于
`orchestrator/contracts.py`。合同校验只报告违约，不操作 GUI，也不决定恢复路线。

## Statement Executor

Statement 是一个交互 Run 的闭环执行单元，不是一个原子动作，也不是整个 Program。

### 目标架构（Agentic Statement Transition）

交互 Statement **不是**业务相位状态机（prepare→write→commit 硬转移表），而是：

```text
EventJournal          事实权威（append-only 原始事件）
StatementMemoryView   决策上下文（按 statement_instance_id 从 Journal 投影 / 压缩）
LLM Transition        语义流转权威（正常每帧一次；Guard 否决时同帧最多重决策一次）
Runtime Guard         仅硬否决 / 合法性校验（不替 LLM 选路线）
StatementOutcome      唯一终态
```

目标每帧循环：

```text
observe
  → build StatementMemoryView(instance, journal, contract)
  → LLM Transition(Contract + MemoryView + CurrentObservation)
       → Act | Complete | Infeasible
  → Runtime Guard validate
  → dispatch 或 terminal
  → append journal
```

| 主体 | 拥有 | 禁止 |
|------|------|------|
| EventJournal | 全部原始事实 | 信念、路线选择 |
| StatementMemoryView | 只读决策上下文 | 可变 phase；发明事实 |
| LLM Transition | 语义下一步 | 改 Program；无证据宣称不可逆事实 |
| Runtime Guard | 终态证据、声明写入范围、结构性不可行、硬预算 | 替 LLM 选择下一个动作或业务相位 |
| StatementOutcome | 交给 ProgramRuntime 的终态 | 环内记账 |

**LLM Belief** 只存在于当帧决策载荷，**不得**无回执/信号路径就写入 Journal。

MemoryView **不可被 compaction 丢掉**的内容：全部已派发动作 receipts、write/commit receipts、
已确认 EffectSignal、off-target / dispatch failure 等硬反证、当前合同与 returns、
尚未关闭的提交或验证承诺。普通页面描述与更早动作的叙事只保留最近 K 步或压缩摘要；这些叙事
不能充当终态证据。只有不可压缩事实暴露的真实 `turn:N` 才允许被 Transition 引证。

**应删除的默认关系**（不得再作为硬路线）：

- `evidence pending → act`
- `effect unknown → recover`
- `persistence.terminal_ready → 强制下一动作只能 commit`

这些应进入 Memory 的 Facts / Contract hints，由 LLM 选择验证、寻找提交入口或完成；
Runtime 仅在动作越过合同、Complete 缺乏证据、Infeasible 缺乏完整结构证明或预算耗尽时否决。

所有 interactive kind（action/filter/navigation/collection/verification）都走同一个
`run_statement_transition`。Runtime 不再生成 action candidate，也不在 LLM 前按证据直接完成。
结构化 observation、Journal receipts、集合覆盖和预算只作为事实输入或终态 Guard；下一步具体
动作由 Transition 直接给出。

Transition 提议被 Guard 否决后，同一 observation 最多再调用一次 Transition，并把否决原因作为
输入；第二次仍不合法则返回 terminal `exhausted`，绝不由 Runtime 猜 fallback action，也不产生
无动作的 running turn。

Transition 的 `complete` 只负责提出语义完成并引用证据，不得自行声明 verification。Runtime
从 adapter / Journal 的权威结构化事实归约 `confirmed`；只有可靠 dispatch 或非权威语义证据时
最多产生 `accepted_unverified`。证据不足则 Guard 否决并触发同帧重决策。

### 职责边界

Statement 负责：

- 页面内 acquire、滚动、展开、页签和向导步骤（战术，非 Program 编排）；
- 控件绑定和原子动作生成；
- loading、派发失败和总 turn budget 等少量硬边界；
- 区分动作是否执行与业务效果是否出现（Journal 事实）；
- 产出唯一 `StatementOutcome`。

Statement 不能：

- 改变目标实体、写入值、基数或业务副作用定义；
- 代替 Program 任选第一条记录完成消歧；
- 增删或重排独立业务事务；
- 聚合表格或发明最终答案；
- 修改剩余 Program；
- 维护 `StatementMemory.phase` 一类业务相位状态机。

`core/run/statements/dispatch.py` 是唯一能为 immediate statement 推进 Interpreter generator
的组件；单条 Read/Query/Navigation executor 不知道下一条 statement。`recording.py` 统一写入
primitive turn、`StatementOutcomeEvent`、LLM 统计和 recovery ledger。

`core/run/interactive.py` 是一个薄适配器：把其余交互 Run 转成 Supervisor 使用的 Statement
结构，启动 Statement loop，并从终态 observation 提取声明返回值。它不拥有恢复策略。

`core/run/statement_memory.py` 从 Journal turns 投影 `StatementMemoryView`；不另建平行账本。

## 动作信号与完成状态

交互动作不能压缩成单一的“成功/失败”或“看见反馈/没看见反馈”。执行层分开记录三类事实：

```text
execution: not_attempted | dispatched | dispatch_failed
target:    on_target | off_target | unknown
response:  observed | none_observed | unobservable | unknown

effect.status:    satisfied | unmet | contradicted | unknown
effect.freshness: preexisting | current_run | unknown

persistence: clean | pending | submitted
```

其中 `execution` 回答“执行器是否可靠派发”，`target` 回答“动作是否落在预期对象”，
`response` 只描述页面是否给出响应。`effect` 回答声明的业务状态是否满足，`persistence`
回答本轮写入是否跨过声明的持久化边界。URL 变化、toast、列表刷新等只产生 response 或
effect 证据；它们不能自行证明动作派发、业务效果和持久化三者同时成立。

Statement 的完成状态由 `StatementOutcome` 表达：

```text
phase: completed | failed | exhausted | infeasible | interrupted
verification: confirmed | accepted_unverified  # 仅 completed
```

验收信号按任务和可用证据降级：

1. 目标业务状态或权威结构化状态已确认；
2. 新鲜效果证据，例如 URL、列表、字段值或成功记录发生变化；
3. 终端副作用已可靠 dispatch，且没有明确的拒绝证据；
4. 弱视觉推断只能辅助，不能覆盖相反的结构化证据。

第三档只能产生 `accepted_unverified`：Interpreter 可以继续执行后续 statement，但最终结果层
不得把它表述为“已确认成功”。`effect.status=contradicted` 时不能凭 dispatch 推进；没有 toast
也不能据此重复提交。带 `target_values` 的 action 是幂等终态合同：权威状态已满足时直接完成。

`persistence` 只投影 write/commit receipt 是否越过边界，不吸收业务效果。Statement 不再维护
跨帧 `ProgressMonitor` 或重复指令计数；动作历史由 MemoryView 提供给 LLM，硬预算由 Runtime 处理。

动作事实只有一条归档路径：executor 在 concrete primitive 产生后固定 `role/action_key`，各传感器
把 dispatch、target、visual/URL/DOM response 交给 `action_signals.py` 追加到同一 receipt；
`turns.py` 只落盘，`evidence.py` 只从 receipt 和当前 observation 投影 claim。后两者不得重新解释
动作，也不得直接读取跨帧 monitor 制造第二份响应事实。

`EventJournal` 是单一有序事实流。dispatch 后才能得到的 settle、target 和 response 结果，只允许
`action_signals.py` 在原 `PolicyTurn` 的 delivery envelope 上收尾；它们不能改写 terminal outcome，
也不能另建第二套回执账本。报告所需 `report_run_log` 仅在一次 loop 调用收尾时由 `AgentResult`
生成，不参与 checkpoint replay 或任何运行时决策。

`CompletionReducer` 只归约结构化证据并回答终态提议是 `satisfied | pending |
contradicted`；它没有 `next` 字段，也不能选择 act/commit/recover。实时控制流固定为：
MemoryView → LLM Transition → Guard → dispatch/terminal。

adapter 只提供 observation 与 action grounding，不能独立修改 Program，不能把 LLM 文本写成
Journal 事实，也不能用 prepare/write/commit 顺序表替代 Transition。

## 动作回执与持久边界

Planner 为每个原子动作声明 `atomic_role`：

```text
prepare | write | commit | iterate
```

- `prepare` 只负责展开、定位、切换入口等 acquire 过程；
- `write` 输入、选择或切换目标值，形成当前 mutation 的因果写入证据；
- `commit` 触发保存、提交、发送等不可安全重复的副作用；
- `iterate` 是允许重复并由边界/目标值终止的调节动作。

交互 Run 还声明 `target_controls`；input/select proposal 必须命中这些字段或控件，不能在目标控件
尚未渲染时改用相邻的全局搜索框。带 `target_values` 的 action 可由权威既有状态完成；
若本调用产生写入，`persistence=explicit_commit` 还要求同 scope 的写入
跨过终端 commit；只有滚动或保存不能证明目标状态，权威既有状态则可直接满足幂等合同。

持久化状态从当前 statement 的结构化 write/commit receipts 投影，不维护第二套可变事务对象。
首个可观测 surface 只是 entry/root 的 fallback；只有 entry surface 上的 commit 才是终端边界。
子表面的 in-place commit 只完成准备，URL 变化本身也不把它升级成终端提交。没有父级提交的工作流
必须由 DSL 声明 `persistence=immediate`，不能靠 URL 启发式猜测完成。

`terminal_ready`、write/commit receipt 和 entry surface 都作为事实进入 Memory 与证据包；
它们不强制下一转移必须 commit。是否观察、验证、寻找提交入口或改走页内路线由 LLM 决定。
Runtime 只否决越过合同的写入目标/值、结构性不成立的 infeasible 和无证据完成；具体 action
point 由 TargetBinding 记录，成功派发的结构化写入可形成 MutationReceipt，但不存在单独的
MutationAuthorization 状态。“保存后又重开编辑器”应由完整 Memory 避免，而不是再引入一条
隐藏的关闭 preparation 状态机。

执行器在平台 adapter 成功派发 concrete primitive 后，用
`execution_scope + statement_id + atomic_role` 形成语义动作键并写入同一 `ActionSignal` 回执。
动作键只标识历史事实，不在 dispatch 前充当第二套授权或重复提交门。持久化投影按 scope 读取
write/commit 回执；foreach 的不同 row identity 属于不同 scope，因此不会互相污染。

`executed`、`no_effect` 和 `target_verify` 是原子动作 delivery 事实，不能被拼成单一“业务成功”
结论。业务效果只由 EffectSignal、结构化 observation 与合同归约得到。

## 执行预算与终态事实

`max_turns` 是 interactive decision/action turn 的严格硬上限。运行器不得为了完成复杂 Program
静默提高它；DSL 静态复杂度估算只能通过显式 `--dynamic-max-turns` 启用。复杂任务需要调用方明确
提供足够预算，不能让估算器成为执行正确性的隐含依赖。

正常 statement 终态不记录为 `PolicyTurn`。运行器在 statement runtime 仍存活时，把完成帧、
Transition 诊断和补齐后的 reads 写成独立 `StatementOutcomeEvent`，持久化后再
`ProgramRuntime.send_outcome`。若下一条也是 interactive statement，它直接在同一 observation
上决定并产生下一个真实 action turn；Outcome event 不增加 turn index、不消耗预算，也不进入
noop 统计。

若最后一个额度恰好派发了动作，循环可重新观察一次并调用同一个 Transition，但进入
terminal-only 模式：只接受 `complete` 或 `infeasible`，任何 `act` 都直接变为 `exhausted`。
该观察不写无动作 `PolicyTurn`，也不能借预算外观察执行下一个 GUI 动作。

`atomic_role` 描述动作生命周期，`action_family` 描述执行机制。生命周期角色优先：commit 只能映射
到 commit/activate，write 只能映射到 input/select，iterate 只能映射到 iterate。协议归一化发生在
primitive 校验之前；不能通过扩大 input 等动作族的 primitive 白名单掩盖角色冲突。

## 恢复边界

恢复按问题发生层级归属：

| 问题 | 所有者 |
|---|---|
| 控件不可见、需要滚动、局部动作无效 | Statement loop |
| 返回字段缺失、类型域违约 | result contract 报告，Program runtime 有界重试 |
| Statement 证明页面路线不可行 | Program runtime 接收证据并 redecompose |
| data query SQL 语法/列名错误 | Query executor 局部自修复 |
| data query 数据源不充分或口径错误 | Query executor 报告，Program runtime 升级 |
| DSL 引用、作用域、控制流错误 | Compiler validator/preflight |

Statement 尽量在不改变 WHAT 的条件下修复 HOW；一旦修复需要改变实体、业务步骤或数据源，
必须返回类型化证据，由 Program runtime 处理。

## 渐进式编排

“渐进式”包含两种不同机制：

1. **Program 渐进展开**：`foreach body_goal` 在拿到真实行后生成子程序；kickback 只重编排
   尚未完成的 Program。
2. **Statement 渐进执行**：一个语义 Run 在实时 observation 上展开为多轮页面内动作。

前者可以改变剩余程序结构，后者只能改变当前 Run 的实现路线。两者通过 `StatementOutcome` 和类型化
失败通信，不共享隐式状态。

## Journal、checkpoint 与 replay

`EventJournal` v3 是 Program revision、PolicyTurn、StatementOutcomeEvent、content note 和
recovery fact 的唯一持久账本。`PolicyTurn` 只表达观察/决策/primitive 执行，schema 禁止在
`PolicyTurn.supervisor.outcome` 中持久化终态；所有 statement 的终态只允许写入
`StatementOutcomeEvent`。

Program cursor、env、run_log 和 recovery budget 由 `ProgramRuntime.resume()` 重放 Journal 重建；
交互 statement 的逻辑活态由最近一个 turn 携带的 `StatementRuntimeSnapshot` 恢复。

运行中 checkpoint 的提交边界是一个 turn 追加到 Journal；终态 checkpoint 的提交边界是
`StatementOutcomeEvent` 追加并写入 `context.json`，且必须发生在 Interpreter 推进和
statement runtime 销毁之前。活态快照只包含会影响后续逻辑决策的 retry、constraint、progress
和合同信息；截图、识别缓存、target acquire 临时探针及设备对象不进入快照。恢复后必须重新观察
真实界面，不能把历史 screenshot 当作当前设备状态。
`context.json` 使用原子替换避免半写文件；但外部动作可能先于 turn checkpoint 生效，因此当前
恢复语义不是 exactly-once。不可安全重复的 commit 仍必须依赖页面事实和 mutation receipt 仲裁。

replay 分为两种，不能混称：

1. **状态 replay**：只消费 Journal，重建 ProgramRuntime 与 StatementRuntime；必须是确定性的；
2. **决策 replay**：载入某 turn 的 observation snapshot，恢复其前序 statement 状态，再调用真实
   LLM Transition；默认不 dispatch 动作。

报告、完成状态和 content-note dedupe 都是 Journal 的投影，不得为 resume 再维护平行账本。

## 结构优先

可靠性改动优先级：

1. schema 上使错误形态不可表示；
2. 结构化谓词、类型和确定性 guard；
3. 统一结果合同、异常分类和恢复账本；
4. 最后才使用文本规则或站点知识。

文本规则必须有触发样例和清除率评估；站点选择器、页面名、字段名和业务状态只能进入 adapter
或 knowledge。WebArena bad case 应沉淀为验证通用不变量的 eval，而不是共享 runtime 的任务特调。
