# milestone = 函数：调用合同的显式化

> 分支 `feat/milestone-as-function` 的设计笔记。出发点（用户判断）：**milestone 的职能没定义清楚。
> milestone 本质是一个 GUI 执行器——在页面上单向（读/写）的连续操作——应该把它当成工具/函数。**
> 本分支把这个隐性合同显式化并交给机器强制，不重写执行器本身。

## 合同定义

一个 milestone 是 DSL program 调用的一个**函数**：

| 函数概念 | 载体 | 强制点 |
|---|---|---|
| 入参（入口状态） | `Run.from_state`（FROM，由 `chain_from_states` 确定性推导） | precondition 入口检查 |
| 目标规格 | `Run.name` / `success_condition` / `read_spec` | — |
| 出参合同 | `Run.returns` + `Run.return_domains`（类型域） | `callframe.check_return_contract`：缺失或出域 = 违约 → 有界恢复 → 诚实失败 |
| 后置条件 | `success_condition`（TO），checker 判定；dispatch gate 家族 = 确定性后置检查 | checker / FilterGate / url_changed |
| 纯度 | `Run.is_query`（read/data_query）/ `is_command`（navigation/filter/action） | preflight CQS 规则 |
| 异常 | kickback：`FeasibilityVerdict.dead_route/required_route`（类型化载荷） | `kickback_adherence_issues` 确定性服从校验 + 锐化重试 |

注意两处对原始直觉的修正：

1. **「单页」放宽为「单边」**：写类函数经常合法跨页（点行→详情页→Edit），本质合同是
   一条 FROM→TO 边、TO 可验证——页面数是实现细节。代码注释（program.py `from_state`）早已如此表述。
2. **「读写单向」落地为 CQS 而非硬拆**：带 returns 的命令是被认可的复合形态
   （dispatch gate 拥有验收、structured_read 拥有取值，engine `normalize_confirm_read_gates` 保证），
   真正的纪律是：查询不得隐含动作、precondition 必须纯、违约不得静默推进。

## 角色分工（三方协程）

```
DSL 解释器(runner.py)      —— 调用方/定序器:yield Run,收 RunResult
agent loop(loop.py)        —— 调用栈/ABI 执行者:turn/帧控制流,边界决策全部取自 callframe
milestone supervisor        —— 被调用方/函数体解释器:checker/planner react 把一个 milestone 开到 done
orchestrator/callframe.py  —— 调用约定本身(本分支新增的显式 ABI)
```

`callframe.py` 的操作集：`open_call`（调用=marshal+reseed）、`extract_ui_returns`（返回值提取:
URL JSON→DOM→视觉）、`check_return_contract`（返回类型检查）、`ReturnRecoveryLedger`+
`tighten_ui_return_run`（违约有界恢复）、`parse_kickback_directive`+`kickback_adherence_issues`
（异常解析与服从校验）。

## 各阶段落地（本分支提交序列）

- **S1 `90aae41`** 调用约定从 loop.py/non_interactive.py 抽入 `callframe.py`，行为不变；
  恢复预算字典化为 `ReturnRecoveryLedger`（按调用点隔离）。
- **S2 `b373c7b`** 返回值合同：`Run.return_domains`（url|number|date|enum:a|b|c|text，
  decomposer 可声明，未声明字段用保守字段名线索推断）；出域值走与空值同款恢复路径——
  杀「抓垃圾静默给错答」类（185 Burlap、Edit/Tee）。`Milestone.returns/read_spec` 结构化通道
  （description 折叠保留，供既有 prompt）。
- **S3 `41ccbb7`** 纯度纪律：`Run.is_query/is_command` 词汇；preflight
  `ORCH_PRECONDITION_IMPURE`（error：precondition 挂 returns/sql = 读错误帧）、
  `ORCH_QUERY_WITH_MUTATION_VERB`（warning：查询原语名字里的动作永远不会执行）。
  task-63 的 foreach 采集列合同已在 runner.py（采集列缺失→诚实失败），未重复。
- **S4 `ea143ce`** kickback 类型化：`dead_route/required_route` 结构化载荷经
  `【死路｜禁止再用】/【规定路线】` 标记折叠进 directive 单通道；redecompose 输出过
  确定性服从校验（禁用机制再现/原 milestone 重现/规定路线未采用），违规→点名违规锐化重试一次。
  直击实测 ~1/3 的 directive-adherence 弱环节。inline directive（空返回/data_query 失败）
  无标记 = 校验自然 no-op（这类恢复合法重访相似步骤）。

- **S6 非交互型从 milestone 剥离（`e665d07` + `0c3f13f`）** 运行时早已绕开（drive_pending_non_ui），
  这一步让类型系统跟上：
  - *S6a* `_chain_block`/`_func_exit_sc`：read/data_query 页面中立，FROM 链穿透（与 Compute 同款）——
    修「夹在两个 UI run 之间的 data_query 把后者 from_state 置空断链」的确定性缺陷；
  - *S6b* `Read`/`Query` 作为 Run 的窄化子类在 `_to_stmts` 构造时分流（wire 格式 op=run+kind 不变、
    LLM 面零改动、isinstance(s, Run) walker 零破坏）；`to_milestone`/`open_call` 对查询节点抛
    ValueError（查询 marshal 进执行器 = 类型错误）；read→navigation 升格显式重建为命令 Run
    （升格=重新分类，不是字段改写）；drive 循环与 callframe 守卫统一走 `is_query` 单谓词。

## 遗留项（后续分支）

1. **Milestone.returns 的 prompt 消费方**：checker 验收「declared returns 可见」、planner 读取指令
   改从结构化字段构建（现仍从 description 散文）。动 policy.py prompt，需 live 验证。
2. **非 UI read 路径的域校验**：`non_interactive.py` 的 read/data_query 结果未过域检查
   （改 completed=False 会影响 interp.failed→goal_completed，需配套设计）。
3. **纯度驱动的记忆化门**：204 类「PreExisting-skip 给了有副作用的函数」——运行时规则
   「只有 is_query/precondition 可被 frame-1 跳过判定豁免」，在 checker 侧落地。
4. **return_domains 的 LLM 采纳率**：prompt 已写，需 live 跑量确认 decomposer 真的产出 enum 域；
   不产出时推断兜底仍在。
5. **服从校验的 live 调参**：锚词判据故意保守（宁漏不误杀）；跑 kickback 任务族（212/204）看召回。
6. **IR 分流的终态**：Read/Query 目前是 Run 子类（wire 兼容、walker 零破坏的折中）；终态是与
   Compute 同级的独立 op（sql/data_scope 等字段下移、报表层撤伪 milestone 条目）——等 live 证明
   边界后再做大改。执行器侧 MilestoneKind 的 collection/verification 清理归跨平台专项
   （iphone/android 采集仍依赖 collection milestone）。

## 回归状态

基线 842 → 本分支 866 全绿（+24：callframe 合同测试 18、preflight 4、FROM 链穿透 2）。
未跑 live（WebArena）；return_domains 推断、adherence 重试、查询节点边界会改变 live 行为，合入主线前需过
778/63/113 族任务。
