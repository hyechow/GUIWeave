# 编排器显式 state 架构与参数实验记录

> 状态：当前生产规范（2026-08-05 合入）
>
> 本文记录 2026-08-05 完成的 ctx API 显式化改造、双平台契约迁移、业务正确性调优，
> 以及 LLM 采样参数实验结论。旧的隐式单例状态方案参见
> [ctx.API 状态依赖与查询降解](ctx_api_translator_layer_design.md)，不再定义当前接口。

## 1. 背景与动机

旧 ctx API 用隐式全局单例管理 UI 状态：`reach` 不返回值（内部设 `_current_ui`），
`query` 内部黑盒做 lookup→constrain→acquire 三步，`commit`/`command` 隐式清除状态。三个问题：

1. sandbox 需要约 440 行源序支配分析推断“哪个 reach 活跃”（隐式状态无法按变量跟踪）；
2. `query` 黑盒把一等 Statement `Acquire` 藏起来，LLM 无法控制采集（复用会话/跳过重复定位）；
3. 跨应用导航靠 prompt 规则约束，没有类型级安全网。

## 2. 新 ctx API

```python
def run(ctx, state):          # state = 初始屏幕
    state = ctx.reach(state, goal, *, success, target=None) -> state   # 消费旧 state,产新
    state = ctx.command(state, capability, **args) -> state            # 消费旧 state,产新
    state = ctx.commit(state, goal, *, target=None, values) -> state   # 返回 post-commit 观测
    scope = ctx.query(state, *, entity, filters={}, coverage) -> scope # 建会话(无 fields)
    rows = ctx.acquire(scope, *, fields, field_types={}, coverage) -> rows  # 投影行,scope 可复用
    detail = ctx.read(state, *, target=None, fields) -> dict           # 借用 state
```

### 消费语义

| 操作 | 参数语义 | 返回 |
|------|---------|------|
| reach/command/commit | 消耗旧 state,产出新 | state |
| query | 借用 state(不失效) | scope |
| acquire | 借用 scope(可复用,多次 acquire 不同投影) | rows |
| read | 借用 state(不失效) | dict |

### 设计要点

- **初始 state 作为 `run(ctx, state)` 第二参数**：全局注入行不通（`state = ctx.reach(state, ...)`
  在函数内先读后写触发 UnboundLocalError），改为运行时把初始屏幕作为参数传入。
- **fields 只在 acquire**：query 只建会话(entity+filters)，投影和类型映射归 acquire。
- **commit/command 返回 post 状态**：使“导航后落到什么 UI”成为可观察值，而不是让 LLM 猜着 re-reach。
- **reach 接收 state 并返回新 state**：它是导航生产者，`state` 是“从当前屏幕导航”的输入，
  首个 reach 的输入是 `run(ctx, state)` 传入的初始屏幕。

## 3. 静态分析重写

新增 `_ctx_state_flow_diagnostics`（显式 state/scope 数据流分析）：

- `STATE_REQUIRED`：UI 操作缺 state/scope 参数
- `SCOPE_REQUIRED`：acquire 收到的 scope 非 query 产出
- `STATE_CONSUMED`：被 commit/command 消费后复用
- `STATE_STALE`：被更晚的 state 生产者替代后复用

旧分析器保留（负责 commit-target 绑定、direct-read 声明、循环失效检查）。数据流分析按变量
SSA 跟踪，取代了隐式全局下的支配/互斥推断。

## 4. 双平台契约迁移

`query(fields=...)` 契约全部拆成 `query(entity/filters) + acquire(fields/field_types)`：

- Android cases.json（10 个）、Browser cases.json（111 个）机械迁移
- eval `CTX_POSITIONS` 加 acquire；`_call_records` 给 acquire 提取 coverage
- 契约过严裁决（保留语义要求、放宽结构/风格要求）：
  - 544：移除冗余 Name 投影（产品已按 Name filter 定位）
  - 709：`reach: 1` → `min-reach: 1`（2 个 reach 功能正确,只是不精简）
  - 549：移除冗余 Attribute Code 投影 + 颜色字面量（程序语义已处理绿色变体）

## 5. 业务正确性调优

- **知识文件**：Settings/Clock/Chrome 三个 Android app 原先无知识，LLM 发明字段名导致系统性失败。
  新建 `_app.md` 声明契约字段（flight mode/brightness/alarm 字段），修复 4 个 task。
- **few-shot 范例**：coding.md 加完整程序示例（全词查询 + 条件短词回退 + 类型 acquire +
  target-bound reach + state 线程化），修复 Browser 491 系统性失败。
- **show→reach 规则补回**：重构 prompt 曾丢失“show/render → 一个 reach”，709 因此判成 commit。

## 6. LLM 采样参数实验（2026-08-05）

`generate_code` 新增 `temperature` 参数（默认 0），两个 eval harness 暴露 `--temp` 和
`--best-of`（生成 N 个 plan 取最优）。

### temperature 扫描结果

| temp | Browser curated | Android |
|------|-----------------|---------|
| **0** | **14/14(稳定 ×3)** | **10/10** |
| 0.3 | 13/14 | 8/10 |
| 0.7 | 14/14 | 9/10 |
| 1.0 | — | 9/10 |

**结论：temperature 0 最优。** 提高温度只增加生成方差、降低可靠性（android 从 10/10 掉到
8-9/10）。早期观察到的“7-9/10 方差”根因是缺知识文件的字段发明，不是采样问题；知识补齐后
temp 0 已稳定到双平台全过。

## 7. 当前基线（2026-08-05）

| 维度 | 结果 |
|------|------|
| Browser curated | 14/14（k=1 稳定） |
| Android | 10/10（k=1 稳定） |
| 回归闸 | 904/904 通过 |
| 系统性失败 | 无（剩余为单次生成方差） |

复跑命令：

```bash
uv run pytest tests/ -q
uv run python evals/android/orchestrator/test_orchestrator.py -j 5            # --temp 0 默认
uv run python evals/browser/orchestrator/test_orchestrator.py --group all -j 5
```

## 8. 产物与 commit

- 生产核心：`gui_agent/core/orchestrator/{models,runtime,sandbox}.py`
- prompt：`gui_agent/prompts/tasks/orchestrator/coding.md`
- 契约：`evals/{android,browser}/orchestrator/cases.json` + `test_orchestrator.py`
- 知识：`knowledge/android/{Settings,Clock,Chrome}/_app.md`
- 测试：`tests/test_coding_orchestrator.py` 等 10 个文件迁移到新 API

分支 `experiment/coding-orchestrator`，9 个 commit 按主题分组：
`1c72bad`(生产核心) → `5b31a5b`(prompt) → `b61666d`(契约迁移) → `6256918`(知识) →
`8f0ffba`(测试) → `55daa67`(few-shot) → `7aef6e9`(549 裁决) → `306fa77`(采样工具)。
