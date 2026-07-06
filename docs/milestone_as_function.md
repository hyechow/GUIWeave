# GUI 任务 = 脚本生成；milestone = 交互调用（FFI）

> 分支 `feat/milestone-as-function` 的设计笔记。
>
> **总纲（用户拍板，2026-07-05 定稿）：用户诉求本质上是混合的——GUI 交互执行 + 非交互执行
> （计算/查询）；所以 GUI 任务的本质是【代码生成，确切说是脚本生成】。** decomposer 生成一份
> 混合脚本：非交互语句由解释器确定性执行，交互步骤是对 GUI 执行器的【函数调用】——一次跨进
> 非确定性世界的 FFI。本分支把这道调用边界的隐性合同显式化并交给机器强制，不重写执行器本身。
>
> 演化脉络：最初表述是「milestone = 页面上单向（读/写）的连续操作」→ 修正一：「单页」放宽为
> 「单边（FROM→TO，TO 可验证）」→ 修正二（本定稿）：「读/写」不是 GUI 层的本质轴——填表单和
> 点链接同为交互执行，执行器从不按数据方向分类；承重轴是【交互/非交互】（执行模式），
> 「纯度」的真义是【确定性】。中间一度以 CQS 语言描述 S3，已废弃该框架（机制保留，命名修正）。

## 脚本生成视角下的工具链

| 现有投资 | 身份 |
|---|---|
| decomposer | 编译器前端（自然语言 → 脚本） |
| validator | 类型检查 / 引用检查 |
| preflight | lint |
| normalize passes（dispatch gate / precondition / chain_from_states） | 编译 pass |
| sample-and-validate、groundtruth pass@K | 生成即测试 |
| milestone call（callframe.py） | **FFI**——跨进非确定性 GUI 世界的外部调用 |
| kickback + redecompose | 运行时异常触发的脚本热补丁 |
| DSL 范例（worked examples） | 语言文档（范例传播 > 规则条文，return_domains 采纳实证） |

**投资判据**：任何可靠性投入，要么把不确定性从脚本层挤出去（更多语句变成确定性执行：
URL-direct、data_query、compute），要么在 FFI 边界上把剩余的不确定性合同化（returns 域、
后置 gate、类型化异常）。两者之外（如让执行器更聪明）按「瓶颈在 program 层」的战略为低优先。

## 交互调用的合同（FFI 边界）

一次 milestone call 是脚本对 GUI 执行器的函数调用：

| 函数概念 | 载体 | 强制点 |
|---|---|---|
| 入参（入口状态） | `Run.from_state`（FROM，由 `chain_from_states` 确定性推导） | precondition 入口检查 |
| 目标规格 | `Run.name` / `success_condition` / `read_spec` | — |
| 出参合同 | `Run.returns` + `Run.return_domains`（类型域） | `callframe.check_return_contract`：缺失或出域 = 违约 → 有界恢复 → 诚实失败 |
| 后置条件 | `success_condition`（TO），checker 判定；dispatch gate 家族 = 确定性后置检查 | checker / FilterGate / url_changed |
| 执行模式 | `Run.is_query`（非交互：read/data_query）/ `is_interactive`（交互：navigation/filter/action） | S6 IR 分流 + `to_milestone` 边界类型强制 |
| 异常 | kickback：`FeasibilityVerdict.dead_route/required_route`（类型化载荷） | `kickback_adherence_issues` 确定性服从校验 + 锐化重试 |

执行模式的三条推论（代码已体现）：

- **分类看执行模式，不看数据方向**：带 returns 的交互 run 是「已发出 + 完成帧读值」的复合形态
  （dispatch gate 拥有验收、structured_read 拥有取值），仍是一次交互调用；
- **升格 = 重新分类**：名义上的 read 需要交互定位时被重建为交互 run
  （`force_interactive_return_recovery`），不是在查询里"顺便"交互；
- **记忆化门槛按确定性划**：只有非交互语句 / precondition 入口保障步可被 frame-1 跳过豁免
  （204 类教训的正确表述，无需读写语言）。

## 角色分工（三方协程）

```
DSL 解释器(runner.py)      —— 脚本执行引擎:非交互语句自己跑,交互步骤 yield 出去
agent loop(loop.py)        —— 调用栈/ABI 执行者:turn/帧控制流,边界决策全部取自 callframe
milestone supervisor        —— GUI 执行器(被调用方):checker/planner react 把一次交互调用开到 done
orchestrator/callframe.py  —— FFI 调用约定(本分支新增的显式 ABI)
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
  把「抓垃圾静默给错答」变成显式违约。`Milestone.returns/read_spec` 结构化通道
  （description 折叠保留，供既有 prompt）。**适用边界**：显式 enum 只覆盖封闭判定域
  （成功/失败、是/否、几种状态）；开放域字段（如 185 的 material，取值集是数据集内容、
  分解时不可知）decomposer 写不出 enum——那族的真实防线是 DOM-first 读取 + 空值恢复，
  不要高估 domain 校验对它的增益。
- **S3 `41ccbb7`** 执行模式纪律（原命名 CQS，S7 修正）：`Run.is_query/is_interactive` 词汇；
  preflight `ORCH_PRECONDITION_IMPURE`（error：precondition 挂 returns/sql = 读错误帧）、
  `ORCH_QUERY_WITH_MUTATION_VERB`（warning：非交互原语名字里的动作永远不会执行）。
  两条规则管的都是交互/非交互边界。task-63 的 foreach 采集列合同已在 runner.py，未重复。
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
    ValueError（查询 marshal 进执行器 = 类型错误）；read→navigation 升格显式重建为交互 Run
    （升格=重新分类，不是字段改写）；drive 循环与 callframe 守卫统一走 `is_query` 单谓词。
- **S7 `af78124` + `f034c13`** 本体论定稿：脚本生成总纲；CQS 框架废弃（`is_command`→`is_interactive`，
  机制不变）；return_domains 进 worked examples（规则条文 12 样本零采纳 → 范例后探针对自有字段
  泛化出 `enum:是|否`，eval dump 渲染采纳可观测）。
- **S8 `b327d9c`** 交互/非交互彻底拆离（用户指令：milestone = 连续交互操作，非交互 action 不再是
  milestone）：`RunLike` = run 家族共享形状；`Run` = 交互 action（kind 收窄
  navigation|filter|action，独有 from_state/return_domains/precondition；`InteractiveAction` 别名）；
  `Read`/`Query` = 平级非交互语句（Query 独有 sql/data_scope）。Stmt 经 callable discriminator
  按 op+kind 路由——wire 格式与 LLM draft 面零改动，旧序列化自动落到新类。每个 walker 逐点表态
  （全家族 → RunLike；仅交互 → Run）；查询节点不再携带空白 from_state 而是根本没有该字段。
  扫荡中抓到一个生产漏网：`decomposer._iter_runs` 供 approximate-entity SQL 重写，此前会漏掉
  Query 节点。144 处测试构造迁移，+1 wire 往返合同测试（867 全绿）。
  **milestone 一词至此退位**：编排层概念 = 交互 action + 非交互语句；Milestone 类只是执行器
  内部的载体格式（`to_milestone` 是 marshalling 的目标格式名），与 DAG/iphone/android 共享，
  其改名归跨平台专项。

- **S9 `25e8733` + `bbe3b21`** 按脚本生成视角重构 orchestrator 包（用户「是时候了」）：
  - *S9a* 退役 engine.py（两件事的合居）：AST normalize passes → `passes.py`（编译 middle-end）；
    Run→Milestone marshalling → `callframe.py`（FFI 边界，它本就自述为 ABI）。全部进口方按符号
    重指（production/tests/evals），867 绿，行为不变。
  - *S9b* 三生成入口收敛：两个门规范化 pass 此前在 cli/webarena/mobileworld **7 处**各自手工
    `normalize_precondition_gates(normalize_confirm_read_gates(...))` 包裹 decompose/redecompose/
    subdecompose 输出。抽成 `passes.finalize_gates`，由 decompose/redecompose 作为管线末步应用一次
    ——三入口现走同一管线（_invoke_plan LLM+validate/retry → to_program 结构 pass → sql-normalize
    → finalize_gates），lint/门规范化统一覆盖。**关键约束**：门规范化必须在 validate 之后（它改写
    kind/success_condition，会让按 kind 判定的 validator 规则失效——实测：折进 to_program 使
    filter-returns 校验静默通过），所以它不进 to_program、而是 finalize 步。7 处 wrap + 冗余 import
    全删。S4 kickback adherence 校验留在 loop（是 kickback 编排控制流，非生成）。
  - *S9c* 包级地图写进 `orchestrator/__init__.py` docstring（每模块的编译器/运行时/FFI 身份）。
- **S10 `1ab706a`** 三条 prompt 规则升格为确定性合同（按投资判据：把不确定性挤出脚本层）：
  ① `NOOP_FLOW_CONTROL_STEP`——交互步自述「无UI变化/仅用于流程控制」= LLM 发明的流程控制,
  行累积是运行时的事,checker 会对着它空转；② `COMPUTE_UNSUPPORTED_EXPR`/`COMPUTE_UNKNOWN_NAME`
  ——compute 方言与作用域的编译期强制（safe_eval 共享面:normalize_compute_expr 与 runner 单源、
  ProbeScope 从 pysurface 搬家、dry_check_expr;`row.sku` 这类运行时必炸的属性访问带教练提示拦截）；
  ③ `ENTITY_SCOPE_PREDICATE_MISSING`——1a970f7 的实体范围规则此前仅 prompt 层（重排丢失率实测
  1/6-2/3）,现为 validate_program(resolution=...) 硬合同,经 _invoke_plan 三入口共享。
  四 code 全部进入注册表+触发样例；872 绿；185 实弹零误伤。

## 离线可靠性（2026-07-04 A/B）

- 185 case-eval：分支 vs 基准 95137c3 各 6 样本 = **4/6 打平，无回归**；函数/内联形态选择是采样噪声。
- 778 case-eval：分支 3/3；groundtruth(k=3)：185 pass@1 ✅、778 pass@3 ✅（1 样本被
  `ROUTER_SET_SELECTOR_NOT_APPLIED` 拦→重试愈合，pass@K>pass@1 = sample-and-validate 的量化证据）。
- 顺带修了评测装置两个 bug：嵌套 `_has_foreach` 遮蔽（185 case 必崩，`95b2c2d`）、back-nav 断言
  只扫函数文本冤杀内联形态（`9531fcb`）。
- 双侧同有的真实抖动（~1/3）：base_sku 显式派生偶发写进 read_spec 散文而非显式 compute 步。

## 离线全面评测（2026-07-06，S9+清理之后）

- **groundtruth（生产门）**：778/185/63/113 **pass@1 全 100%**（07-04 时 778 pass@1 曾 50%）。
- **feasibility 套件 7/7**——S4 的 schema/prompt 改动未伤判定质量。
- **redecompose**：修复 S8 评测漏网后 13/18（基准 8/9）。唯一持续向下的信号 = **113 实体范围**
  （分支 1/6 vs 基准 2/3）：foreach returns 丢实体列 / data_query 丢范围谓词——该规则只有 prompt
  层（1a970f7），**无确定性兜底** → 候选 S10：resolution 有 lookup 实体 + 计划含 foreach+data_query
  时，validator 强制实体列与谓词在场。
- **29 例 case-eval**：分支 19/10 vs 基准 23/5，工艺层单跑噪声大。深挖了唯一跨样本一致的
  「登录前置标注率下降」：受控实验先后否定 schema 字段邻接（挪位不恢复）与 return_domains 内容
  （全量剥离到与基准字节相同仍 2/6 vs 6/8，p≈0.14 不显著）→ **不能归因于分支**，定性为观察项
  （该 case 族有 checker `_check.md` 登录判据 L2 兜底）。
- **过程产出**：S8 评测漏网第 2、3 批——redecompose eval + 实验脚本的 `isinstance(s, Run)`
  walker（`f552303`）、case-eval 对全家族 run 的 `.sql` 直接访问（`88bc4f2`）。教训追加：
  **IR 类型层次改动的消费方 = 全仓,evals/scripts 的 walker 与字段直取都算**。

## 遗留项与风险（后续分支 / live 首跑清单）

1. **enum 假违约风险（live 首跑第一优先）**：decomposer 声明 `enum:是|否` 但页面信号被读成
   「已连通」→ 假违约 → 3 次收紧重试 → 把对的答案做成失败。read_spec 与域配套引导是缓解；
   若出现，先放宽枚举匹配（包含/同义归一），不回退机制。
2. **服从校验的 live 召回**：锚词判据故意保守（宁漏不误杀）；跑 kickback 任务族（212/204）看召回。
3. **Milestone.returns 的 prompt 消费方**：checker 验收「declared returns 可见」、planner 读取指令
   改从结构化字段构建（现仍从 description 散文）。动 policy.py prompt，需 live 验证。
4. **非交互路径的域校验**：`non_interactive.py` 的 read/data_query 结果未过域检查
   （改 completed=False 会影响 interp.failed→goal_completed，需配套设计）。
5. **确定性记忆化门**：204 类——「只有 is_query/precondition 可被 frame-1 跳过判定豁免」，
   在 checker 侧落地。
6. **IR 分流终态已由 S8 交付**（字段下移完成）。剩两件：报表层撤非交互伪 milestone 条目
   （orchestrator_html 单列渲染 Read/Query 记录）；执行器侧 MilestoneKind 的
   collection/verification 清理 + Milestone 类改名归跨平台专项（iphone/android 采集仍依赖
   collection milestone）。

## 回归状态

基线 842 → 本分支 867 全绿（callframe 合同测试 19、preflight 4、FROM 链穿透 2 等）。
未跑 live（WebArena）；return_domains（含范例采纳）、adherence 重试、查询节点边界会改变 live 行为，
合入主线前需过 778/63/113 族任务。
