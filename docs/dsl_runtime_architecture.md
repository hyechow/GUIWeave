# GUIWeave Runtime Architecture

本文描述当前生产实现。GUIWeave 只有一种执行入口：Compiler 生成语义 Program，
Interpreter 解释显式控制流，三个 Statement executor 在运行时完成 UI、数据与平台能力。

滚动、分页采集与数值/集合处理的能力边界及当前落地缺口，见
[运行时数据采集与处理设计](data_acquisition_and_processing_design.md)。

## 第一性原理

GUI 任务不是单纯的“看图并点击”。一个完整任务同时包含：

- 对真实界面的观察与交互；
- 确定性的导航、返回或应用启动；
- 对运行时数据的筛选、关联与派生；
- 条件分支和对真实集合的逐项处理。

因此 Program 只表达业务目标、数据依赖和显式控制流，不提前猜页面路径、控件、数据执行计划、
Python 表达式或 Statement 内部相位。界面与数据细节由 executor 面对真实上下文时决定。

## 唯一 Program IR

Program 只包含六种节点：

| 节点 | 职责 | 执行者 |
| --- | --- | --- |
| `Interact` | 在当前 `main` surface 上达成一个线性 UI 后置条件 | Transition 驱动的交互 executor |
| `Data` | 从已绑定输入和当前观察中派生类型化数据 | Data executor |
| `Command` | 调用 `open_url/back/launch_app` 等确定性能力 | 平台 capability executor |
| `If` | 根据已绑定值选择显式分支 | Interpreter |
| `ForEach` | 对已物化列表执行固定 body | Interpreter |
| `Finish` | 绑定最终输出并结束 Program | Interpreter |

所有节点共享单一 `main` surface。当前版本不持久化多标签页或多窗口身份；切换活动页面后，
`main` 就表示新的活动交互表面。

旧的 Read、Query、Compute、Function/Call、动态循环体和渐进式子编排没有生产入口。

## 权限边界

| 决策 | 唯一所有者 |
| --- | --- |
| 任务拆分、显式分支、循环结构、类型化依赖 | Compiler |
| Program 游标、环境、循环帧、分支选择 | Interpreter / ProgramRuntime |
| 当前 Statement 处于什么状态、下一步在哪里做什么 | Transition |
| 一个语义动作如何落到平台原子动作 | Action policy / adapter |
| 数据计划及其一次局部修复 | Data executor |
| 确定性导航和应用能力 | Command executor |
| 已经发生的动作、观察和终态 | EventJournal |

Compiler 不编译页面内部操作路径。Statement executor 不修改 Program 游标、控制流或任务目标。
Action policy 不决定停止、完成或不可行。

## Interact：有记忆的 React 执行

一个 `Interact` 是线性目标，不是预定义业务相位机。它可以跨页面执行多步操作，只要目标本身
不包含“若 A 则 B，否则 C”这类显式分支。显式分支必须放进 Program 的 `If`。

每个非 loading 帧：

1. `StatementMemory` 从 Journal 投影本 invocation 已发生的事实；
2. 当前截图提供必需的视觉观察，URL、DOM、accessibility、表格和控件是可选增强；
3. Transition 同时判断当前任务状态，并提出一个“在哪里做什么”的下一动作，或终态；
4. 机械校验只检查结构、证据引用、当前能力和预算；
5. 合法动作交给 Action policy grounding 和 adapter 派发；非法提议将拒绝原因反馈给同帧
   Transition，最多重决策一次。

Transition 的输入由以下部分组成：

- 不可变 Statement contract：goal、success、required values、inputs、typed returns；
- Journal memory：事实、最近动作和回执、失败约束、已有输出；
- 当前截图及当前观察摘要；
- 平台可选提供的 URL、标题、控件、表格、affordance；
- 与当前应用相关的知识。

视觉是跨浏览器与移动端的基线。缺少 DOM 或 accessibility 不能被解释成目标不存在。

Runtime 可以否决越权、伪造证据、无能力、超预算或不满足完成合同的提议，但不依据业务词表
补正则、不替模型选择路线，也不把一次动作拒绝升级成 Statement 不可行。

## Data：运行时语义数据执行

`Data` 在 Program 中只声明 goal、inputs 和 typed returns。运行时 Data executor 查看真实输入
与当前 observation，由一次 LLM 调用生成最多六步的小计划，再由受限 kernel 执行。

允许的计划操作只有：

- `read_observation`：读取当前表格、表单、URL、标题或视觉字段；
- `sql`：在真实 record/list/table 上做受限查询；
- `emit`：按 declared returns 输出最终值。

计划必须以唯一 `emit` 结束。执行失败时可携带确定性错误再规划一次；第二次失败即返回失败，
不在 Program 内持久化半成品数据计划。

## Command：确定性能力

已知 URL、返回动作或应用名由 Program 编译成 `Command`，直接调用 adapter 能力：

- `open_url(url)`；
- `back()`；
- `launch_app(app)`。

Command 参数可以是字面量，也可以引用已绑定输出。未知路线、需要观察才能确定的入口或页面内
导航仍属于 `Interact`，Compiler 不猜测它们。

## If 与 ForEach

`If` 只读取一个已绑定值并执行一个显式分支。条件运算是确定性的，不调用 LLM。

`ForEach` 只接受已物化的 list：

- `items` 指向列表；
- `item` 与可选 `index` 是词法局部变量；
- `body` 在编译时固定；
- 可选 `collect + into` 在每次 body 完成后收集一个显式值。

成员筛选、排序、去重或集合派生必须先由 `Data` 完成。循环不会采集 UI 行、生成新 body、按样本
重新分解或调用隐藏函数。

## 类型化数据流

三个 executor 都只返回 `StatementOutcome`。完成态可携带：

```text
outputs: dict[str, JsonValue]
```

每个输出必须在 Statement `returns` 中声明 `OutputSpec`。Interpreter 拒绝缺失、类型错误和额外
字段，随后才把整个 outputs record 绑定到 `bind`。后续节点只能通过 `ValueRef(var, path)` 读取。

`StatementOutcome` 是 Statement 终态唯一权威；`ProgramOutcome` 是任务终态唯一权威。报告、
持久化和外层结果都是它们与 Journal 的只读投影。

## 状态与恢复

顶层只有三个状态域：

1. `ProgramRuntimeState`：Program 游标、环境、循环帧、run log 与恢复账本；
2. `StatementRuntimeState`：当前 invocation 的临时执行资源和 Journal memory 视图；
3. `EventJournal`：动作、观察、回执、输出和终态的追加事实。

Statement 结束后不把 monitor、控件缓存或 planner 私有数据提升到 Program。Journal 是唯一持久事实
流，Memory 只是按 invocation 构造的有界只读投影。

Checkpoint 保存 Program revision、Interpreter state 与 Journal cursor。Replay 从这些权威数据恢复
Program 进度；不支持把旧 DSL、旧 milestone 状态或缺少 Program revision 的历史执行事件解释成
当前运行。

恢复路由保持语义分层：

- 一次动作被机械拒绝：同一 Statement 的 Transition 重决策；
- 当前 Statement 仍可达但输出不足：同一 Statement 重试；
- Statement 结构性不可行或合同冲突：ProgramRuntime 发起 hot recompile；
- 预算耗尽或失败：收敛为 StatementOutcome，再由 ProgramRuntime 处理。

## 编译门禁

Compiler 只验证：

- 六节点结构合法；
- 引用在控制流上可达；
- inputs/returns/If/ForEach 的类型和作用域一致；
- 用户要求的目标值与范围没有在编译中丢失；
- 已知确定性能力使用 Command，显式分支使用 If。

它不验证页面字段、具体控件、站点路由、运行时数据字段、业务相位或集合内容。这些都要面对真实
上下文在 executor 内解决。
