# Signal-Source Architecture — typed observation + scoped authority + model arbitration

本文只描述 observation/context 进入模型时的来源与权威域。运行时动作事实另由
`core/run/action_signals.py` 统一归档，typed claim 由 statement `evidence.py` 投影，终态证据由
`CompletionReducer` 归约。LLM 拥有 statement 内的语义下一步；它不能发明事实，终态提议仍受
合同与证据 Guard 否决。

## 为什么

GUI agent 若让 checker、planner 和 policy 各自解释 DOM、截图、URL 与回执，很快就会形成互相
冲突的隐式状态机。当前实现把信号先投影成有来源和权威域的 facts，再让同一个 Transition 结合
StatementMemory 与当前帧决定下一步；Runtime 只验证终态和合同边界。

协议是：**每条信号标注来源（source_type）+ 它对哪个 claim/domain 权威
（authoritative_for）+ 新鲜度 + 覆盖度**。模型按同一冲突协议作语义判断；确定性代码只处理
可验证事实、动作 grounding、硬预算与终态否决，不绕过 Transition 选择业务流转。

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
4. **同一维度多个权威源冲突**:不硬猜;按 `freshness` / `coverage` 排序;仍冲突 → 选择一个可执行验证动作，不能输出无因等待。
- 特例:`rt.judgment` 与 fresh `obs.*` 冲突时,一律视为**过时**,不得放大上轮误判。

## 当前边界

- `form_controls`、`applied_filters`、动作回执和 EffectSignal 以来源/权威域进入统一 Transition；
- 没有 checker/planner/filter-gate 三条完成路径，所有语义完成都先由 Transition 提议；
- `CompletionReducer` 只回答证据是否支持终态，不输出下一动作；
- Guard 否决后同帧最多重决策一次，再失败即 `exhausted`，不写空 running turn。

## 不变量

`ContextBlock` 元数据、报告(reports)、prompt header、checker 输出**都要能看到同一套 source/authority 结构**——不是只改 prompt 文字。
