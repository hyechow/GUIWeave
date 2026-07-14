# Signal-Source Architecture — typed observation + scoped authority + model arbitration

本文只描述 observation/context 进入模型时的来源与权威域。运行时动作事实另由
`core/run/action_signals.py` 统一归档，typed claim 由 milestone `evidence.py` 投影，完成建议由
`ExecutionCoordinator` 归约；模型输出本身不是运行时状态转移权限。

## 为什么

GUI agent 里判断"当前状态"的逻辑正在以**特判**堆叠:checker.md 里散落着"DOM current 优先于截图/窄框以 current 为准/native_select 展开即已选"等局部规则;policy.py 里 dispatch/filter gate 用 url_changed / active-filters chip **确定性判 done、跳过 LLM**。这些都是"我们替模型硬编码地整合不同信号源"。

本架构把它收敛成一套统一协议:**每条信号标注来源(source_type)+ 它对哪个 claim/domain 权威(authoritative_for)+ 新鲜度 + 覆盖度**,模型按一条冲突协议自己裁决。不是"把所有确定性交给 LLM"——确定性快路仍合理,只是它应读**同一套信号**,而不是散成特殊规则。

## 源类(source_type)—— 观测 vs 判断 vs 命令,三分,不混成一个排序

| source_type | 是什么 | 对什么权威 |
|---|---|---|
| `obs.dom` | 适配器/DOM 直读事实 | `current_state`(控件值/选中态/chip/记录数/url/title) |
| `obs.vision` | 截图/视觉推断 | `current_state`(布局/弹层/可见结构/空间关系) |
| `obs.effect` | 动作后确定性效果(url/dom delta) | `current_state`(动作是否产生跳转/变化) |
| `rt.judgment` | 过去模型的判断(checker/replan/history) | **不对任何当前状态维度权威**;只对"过去如何判、为何失败、试过哪些路径"有用 |
| `knowledge` | 应用知识 | `app.semantic_structure`(站点结构/字段位置);可能过时 < 实时观测 |
| `directive` | 上层运行时纠正 | **`strategy.required`**(下一步策略/禁止路径/纠正方向) |

**三类权威分开,不要合成一个大排序:**
- `obs.*` → `current_state`(页面现在是什么样)
- `directive` → `strategy.required`(该怎么做 / 禁走哪条路)——可覆盖 knowledge 与默认策略,但**不能把一个不存在的 DOM 控件变成存在**
- compiler / runtime policy → `program_legality`(DSL 是否合法)

## Authority domains(权威落到 claim,而非仅 block)

`control.value` · `control.selected` · `filter.applied` · `filter.residual` · `table.record_count` · `table.rendered_rows` · `page.url` · `page.title` · `layout.visible_structure` · `modal.visibility` · `spatial.relationship` · `action.response.url_changed` · `action.response.dom_changed` · `action.response.none_observed` · `prior_judgment` · `app.semantic_structure` · `strategy.directive`

同一个 block 常混多个 claim(如 form_controls 含 存在性/当前值/selected_text/是否被弹层遮挡),它们**不是同一权威域**:DOM 对 `control.value`/`control.selected` 权威,但不对"是否被弹层遮挡/是否可见"权威(那归 `obs.vision`)。P1 先在 block 级实现,但 domain 命名尽量细。

## 每条信号的元数据

- `source_type`：上表之一
- `authoritative_for: list[domain]`
- `not_authoritative_for: list[domain]`（可选,但很有用——显式挡住越权)
- `freshness: turn | post_action | prior_turn | task_static`
- `coverage: complete | rendered_only | partial | unknown`

**表格边界必须写清**:`obs.dom` 对 `table.rendered_rows` / `table.record_count` 权威,`coverage=rendered_only` **≠ 对全量数据集权威**。不写这条,模型会把当前页 rows 当全量。

## 冲突裁决协议(4 条可执行规则,给模型)

1. **先识别当前要判断的维度**:控件值 / 筛选是否生效 / 页面布局 / 动作效果 / 历史诊断……
2. **只采信对该维度标 `authoritative_for` 的 fresh 源**。
3. **非权威源只能补充解释,不能反驳权威源**(如:截图里"下拉仍展开"不能推翻 DOM 的"已选中"——`control.selected` 归 `obs.dom`)。
4. **同一维度多个权威源冲突**:不硬猜;按 `freshness` / `coverage` 排序;仍冲突 → 判 `in_progress` 或请求补观察。
- 特例:`rt.judgment` 与 fresh `obs.*` 冲突时,一律视为**过时**,不得放大上轮误判。

## 迁移边界(分阶段)

- **P1(本步)**:`ContextBlock` 加 `authoritative_for` / `not_authoritative_for` / `freshness` / `coverage` + render header 暴露;checker/planner 加 4-rule 协议;**只迁 3 个高价值块**:`form_controls` / `applied_filters` / `last_action_response`。**不动 gate bypass。**
- **P2**:其余 obs 块分源(grid_status / url / title / 截图 vision 元信息)。
- **P3**:多选(入口案例)——`control.selected` 由 `obs.dom` 权威,模型裁决,修 native multiselect 循环。
- **P4**:dispatch / filter gate **降级**为 `obs.effect` / `obs.dom` 信号块;**保留 deterministic fast path 作为"同一信号协议下的安全快路"**(读同一套信号),仅当 eval 证明 checker 在无 fast path 时稳定后,再逐个移除 bypass。
- **P5**:清理散落特判,checker.md 的局部"DOM 优先"规则收敛进协议。

## 不变量

`ContextBlock` 元数据、报告(reports)、prompt header、checker 输出**都要能看到同一套 source/authority 结构**——不是只改 prompt 文字。
