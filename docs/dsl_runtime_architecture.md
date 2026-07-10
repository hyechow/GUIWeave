# DSL Program Runtime 与 Milestone Executor

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
       -> Interactive Run     交给 Milestone Executor
            -> observe/check/plan/action loop
  -> StatementOutcome
  -> Interpreter resume
```

不存在独立的调用帧架构层。Python generator 的 `yield Run` / `send(RunResult)` 已经是
Interpreter 暂停和恢复的边界，再引入一个有状态、有恢复策略的“frame”只会与 Interpreter
和 Milestone 重叠。

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
- 将无需 Milestone loop 的 statement 交给 immediate dispatcher；
- 将交互 `Run` yield 给 Milestone executor；
- 接收统一的 `RunResult`，绑定 `var` 和 `returns`；
- 保存 `env`、`run_log` 和 materialized tables；
- 根据类型化失败决定继续、重试、重编排剩余 Program 或诚实失败。

跨 statement 的恢复预算和 `RecoveryLedger` 属于 Program runtime。它们不能放进 Milestone，
也不能隐藏在一个旁路调用对象里。

## Statement Executors

不同 statement 共享最小结果协议，但不强行共享不适用的执行状态。

```text
RunResult:
  completed
  failed
  reads
  rows
  summary
  evidence
```

- `Compute` 返回标量；
- `Read` 返回当前帧的结构化字段；
- `Query` 返回标量字段或行集；SQL 语法/列名自修复留在 Query executor，数据源口径不符则升级；
- 具体 URL/back 导航仍是交互 Run，但可以走 immediate navigation fast path；
- Interactive Run 返回 Milestone 的终态证据及完成帧读值。

通用结果构造属于 Interpreter runtime。返回字段的缺失、类型域和 canonicalization 属于
`orchestrator/contracts.py`。合同校验只报告违约，不操作 GUI，也不决定恢复路线。

## Milestone Executor

Milestone 是一个交互 Run 的闭环执行单元，不是一个原子动作，也不是整个 Program。

```text
semantic Run
  -> observe
  -> check
  -> acquire / plan
  -> one atomic action
  -> observe again
  -> ...
  -> completed | infeasible | exhausted | aborted
```

Milestone 负责：

- 页面内 acquire、滚动、展开、页签和向导步骤；
- 控件绑定和原子动作生成；
- 页面内 no-effect、stuck 和替代路线；
- 区分动作是否执行与动作效果是否出现；
- 按证据优先级判断当前 Run 是否达到终态。

Milestone 不能：

- 改变目标实体、写入值、基数或业务副作用；
- 代替 Program 任选第一条记录完成消歧；
- 增删或重排独立业务事务；
- 聚合表格或发明最终答案；
- 修改剩余 Program。

`core/run/statements/dispatch.py` 是唯一能为 immediate statement 推进 Interpreter generator
的组件；单条 Read/Query/Navigation executor 不知道下一条 statement。`recording.py` 统一写入
turn、LLM 统计和 recovery ledger。

`core/run/interactive.py` 是一个薄适配器：把其余交互 Run 转成 Supervisor 使用的 Milestone
结构，启动 Milestone loop，并从终态 observation 提取声明返回值。它不拥有恢复策略。

## 执行信号与效果信号

交互完成不能压缩成单一的“看见反馈/没看见反馈”。Milestone 必须区分：

```text
execution: not_attempted | dispatched | dispatch_failed
effect:    confirmed | contradicted | unobservable | unknown
```

验收信号按任务和可用证据降级：

1. 目标业务状态或权威结构化状态已确认；
2. 新鲜效果证据，例如 URL、列表、字段值或成功记录发生变化；
3. 动作已可靠 dispatch，且页面明确没有效果反馈通道；
4. 弱视觉推断只能辅助，不能覆盖相反的结构化证据。

`effect=contradicted` 时不能凭 dispatch 宣告成功；反馈通道不存在时，也不能因没有 toast
而重复提交。`require_fresh_action` 的写操作不能被 PreExisting 状态吞掉。

## 恢复边界

恢复按问题发生层级归属：

| 问题 | 所有者 |
|---|---|
| 控件不可见、需要滚动、局部动作无效 | Milestone loop |
| 返回字段缺失、类型域违约 | result contract 报告，Program runtime 有界重试 |
| Milestone 证明页面路线不可行 | Program runtime 接收证据并 redecompose |
| data query SQL 语法/列名错误 | Query executor 局部自修复 |
| data query 数据源不充分或口径错误 | Query executor 报告，Program runtime 升级 |
| DSL 引用、作用域、控制流错误 | Compiler validator/preflight |

Milestone 尽量在不改变 WHAT 的条件下修复 HOW；一旦修复需要改变实体、业务步骤或数据源，
必须返回类型化证据，由 Program runtime 处理。

## 渐进式编排

“渐进式”包含两种不同机制：

1. **Program 渐进展开**：`foreach body_goal` 在拿到真实行后生成子程序；kickback 只重编排
   尚未完成的 Program。
2. **Milestone 渐进执行**：一个语义 Run 在实时 observation 上展开为多轮页面内动作。

前者可以改变剩余程序结构，后者只能改变当前 Run 的实现路线。两者通过 `RunResult` 和类型化
失败通信，不共享隐式状态。

## 结构优先

可靠性改动优先级：

1. schema 上使错误形态不可表示；
2. 结构化谓词、类型和确定性 guard；
3. 统一结果合同、异常分类和恢复账本；
4. 最后才使用文本规则或站点知识。

文本规则必须有触发样例和清除率评估；站点选择器、页面名、字段名和业务状态只能进入 adapter
或 knowledge。WebArena bad case 应沉淀为验证通用不变量的 eval，而不是共享 runtime 的任务特调。
