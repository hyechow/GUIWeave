# Manager Protocol Experiment

隔离比较同一个 GUI action 决策使用不同模型输出协议时的效果，不评测任务分解、`ctx.api` 编排
或完整 GUI loop。

2026-08-09 的实验配置、原始报告索引、聚合结果和阶段性结论见
[`RESULTS.md`](RESULTS.md)。原始运行日志仍保留在 `logs/manager_protocol/`，不纳入版本控制。

## 实验对象

每个 case 都只包含一次 action-policy 决策：

```text
截图 + action instruction + 可选 grounding hints
                         ↓
                 预测一个 GUI action
```

复用已有数据，不复制 cases 或截图：

- `evals/iphone/action_policy/cases.json`
- `evals/android/action_policy/cases.json`
- `evals/browser/action_policy/cases.json`

case 内截图路径优先按仓库根目录解析；不存在时，runner 应使用 case 文件同级的
`screenshots/<basename>`，兼容早期 iPhone case 的旧路径。

## 对照组

| Variant | 输出协议 | Thinking |
|---|---|---|
| `structured_off` | 当前平台的 ActionDecision schema | off |
| `structured_on` | 当前平台的 ActionDecision schema | on |
| `tool_call_off` | 每种 action 一个独立 tool schema | off |
| `tool_call_on` | 每种 action 一个独立 tool schema（`tool_choice=auto`） | on |

Tool-call 组不是一个带大量可选字段的 `execute_action` 工具。`tap`、`type_text`、`scroll`、
`drag`、`navigate`、`upload_file` 等 action 分别注册，各自只暴露需要的参数；平台专属 action
只在对应平台出现。

## 公平比较

四组实验必须使用相同的：

- model、provider、temperature、max tokens 和采样次数
- 截图、instruction、grounding hints 和 action-policy prompt 语义
- action 字段定义、默认值、坐标系统及平台能力范围
- 参数规范化和确定性 postprocess
- 重试策略；协议本身的失败应单独计数，不能只比较重试后的最终结果

当前 token-plan 端点在 thinking 模式下拒绝 `tool_choice=required`，因此 suite 明确配置
`tool_call_off=required`、`tool_call_on=auto`；runner 不做静默 fallback。两组仍都要求响应最终必须且
只能包含一个 action tool call，thinking-on 在 auto 模式下没有调用工具会记为 `missing_tool`。
Structured 组必须只返回一个 ActionDecision。两种协议的结果统一规范化为同一个平台 Action model
后，再复用 case 的 `expected` 断言评分。

## 指标

- `protocol_valid`：是否产生唯一且可解析的 action
- `action_type_correct`：action/tool 选择是否正确
- `args_valid`：参数是否通过该 action 的 schema
- `expected_fields_correct`：case 标注字段是否全部匹配
- `missing_tool`、`multiple_tools`、`unexpected_text`、`schema_error`
- raw first-attempt accuracy 与 repaired/retried accuracy
- input/output/thinking tokens、延迟和调用次数

运行结果写入 `logs/manager_protocol/<timestamp>/`，不要提交生成结果。

## WebArena 状态/动作分离 replay

`state_action_run.py` 从 WebArena 官方评分 `1.0` 的通关轨迹读取冻结截图、状态机输出和
实际命中 action，不启动浏览器，也不执行 `ctx.api`。当前包含 8 个帧，每类 2 个：

- `tap`：展开侧栏菜单、勾选 Columns 复选框。
- `type`：填写日期、替换长标题。
- `select_option`：选择订单状态、选择产品类型。
- `scroll`：在 Dashboard 和产品详情页继续寻找目标字段。

case 分布在多条成功轨迹中；每个 case 通过 `source_run + event_index` 定位原始上下文和截图。
runner 同时汇总总体结果与 `summary_by_action_type`，避免坐标密集的 `tap` 掩盖其他 action 的
协议表现。纯原生 resolver 且没有 Action Policy 模型输出的步骤不纳入本组实验。

它比较四种架构：

| Variant | LLM 调用 | 状态输出 | Action 输出 |
|---|---:|---|---|
| `action_only` | 1 | 无 | tool call |
| `joint_content_tool` | 1 | 同一响应的 `content` JSON | 同一响应的 tool call |
| `separate_two_call` | 2 | 第一次 structured JSON | 第二次 tool call |
| `recorded_intent_policy` | 1 | 复用通关轨迹的状态/指令 | tool call |

前三组用于区分“额外状态输出是否干扰 action”与“两阶段是否改善 grounding”；最后一组是当前
Supervisor/Action Policy 分层的 replay 参考上限。所有 tool-call 组都要求恰好一个动作；缺工具、
多工具、状态 JSON 缺失或非法均作为协议失败，不静默修复。坐标用目标区域命中率评分，同时报告
到通关轨迹记录点的归一化距离。若模型把一个点错误填入标量参数数组，runner 仍判定协议失败，
但会额外记录 `attempt_diagnostic.diagnostic_target_hit`，用于区分“定位正确、编码错误”和真正的
决策/grounding 错误；该诊断结果不会参与严格通过字段 `ok`。`semantic_action_matches` 则把这类
可诊断的正确语义动作单独计数，不能替代 `protocol_valid` 或 `args_valid`。

```bash
uv run python manager_protocol/state_action_run.py
uv run python manager_protocol/state_action_run.py --case orders_enable_customer_email --no-write
uv run python manager_protocol/state_action_run.py --variant joint_content_tool --samples 3
uv run python manager_protocol/state_action_run.py --thinking off
uv run python manager_protocol/state_action_run.py --thinking off --tool-choice required
```

`--tool-choice` 可显式选择 `auto` 或 `required`，并写入每条 result 和报告顶层。当前 suite 默认
`thinking=on + tool_choice=auto`；这是端点兼容配置，不代表 runner 在生成时强制调用工具。runner
对“恰好一个工具”的要求属于响应后的严格评分约束。报告分别统计 `with_tool`、`missing_tool`、
`with_content` 和 `missing_state_content`，不要只用一个 `protocol_valid` 混合解释两类缺失。

用于隔离联合响应协议变量的推荐命令：

```bash
# 兼容 thinking，但 auto 不能保证调用工具
uv run python manager_protocol/state_action_run.py --variant joint_content_tool \
  --thinking on --tool-choice auto --samples 3

# 检查关闭 thinking 后 auto 的工具选择倾向
uv run python manager_protocol/state_action_run.py --variant joint_content_tool \
  --thinking off --tool-choice auto --samples 3

# 强制工具，并检查联合响应中是否仍有状态 content
uv run python manager_protocol/state_action_run.py --variant joint_content_tool \
  --thinking off --tool-choice required --samples 3
```

当前 token-plan 端点会拒绝 `thinking=on + tool_choice=required`，runner 不应自动改写这个组合；
需要用单独状态调用加 required action 调用，或接受 auto 的首轮漏工具并把重试单独计数。
