# Manager Protocol 实验记录（2026-08-09）

本文整理 `logs/manager_protocol/` 中已经完成的实验。它是实验快照，不是对所有模型、端点或
完整 GUI agent 的普遍结论。

## 实验环境与口径

- provider：`tokenplan`
- model：`qwen3.7-plus`
- temperature：`0.0`
- GUI 输入：冻结截图；不启动浏览器，不执行动作
- action 输出：每种 action 使用独立 tool schema，随后统一转换为真实 Browser Action model 校验
- WebArena replay：8 个官方评分 `1.0` 的成功轨迹帧，覆盖 `tap`、`type`、
  `select_option`、`scroll` 各 2 个

严格成功要求协议、参数和预期动作全部正确。`semantic_action_match` 是诊断指标：即使模型把
正确坐标错误编码成数组，也可以说明动作意图和定位基本正确，但不能替代严格成功。

## 原始报告索引

原始 JSON 是本地运行产物，不提交仓库；下列路径用于追溯本次整理的数据来源。

| 时间目录 | 用途 | 是否用于主结论 |
|---|---|---|
| `20260809_112450` | action protocol 首次 smoke；暴露 thinking + required 的端点错误 | 兼容性结论 |
| `20260809_112547` | 单独复测 thinking + auto tool call | 兼容性结论 |
| `20260809_112610` | 修正配置后的四协议 navigate smoke | smoke 结果 |
| `20260809_142645`—`143558` | replay runner、schema 与诊断指标迭代，只有 1—2 个 case | 不跨版本聚合 |
| `20260809_145446` | 首次 8-case 架构 run，诊断口径尚在调整 | 参考 |
| `20260809_150134` | 统一诊断口径后的 8-case、四架构 run | 架构对照 |
| `20260809_151144` | joint，thinking on + auto，8 case × 3 | 协议主对照 |
| `20260809_151351` | joint，thinking off + auto，8 case × 3 | 协议主对照 |
| `20260809_151505` | joint，thinking off + required，8 case × 3 | 协议主对照 |
| `20260809_151718` | recorded intent，thinking off + required，8 case × 3 | action 基线 |

## 1. Structured output 与 tool call smoke

最终修正后的单个 `navigate` case 四组均通过：

| Variant | 协议配置 | 成功 | 延迟 | 输出 token | reasoning token |
|---|---|---:|---:|---:|---:|
| `structured_off` | structured，thinking off | 1/1 | 2.039s | 48 | 0 |
| `structured_on` | structured，thinking on | 1/1 | 4.494s | 143 | 93 |
| `tool_call_off` | tool call，thinking off，required | 1/1 | 1.614s | 44 | 0 |
| `tool_call_on` | tool call，thinking on，auto | 1/1 | 3.116s | 85 | 35 |

这个 smoke 只能证明 runner 和四条协议链路可用，不能据此判断哪种协议的 action 准确率更高。
本例中 tool-call 输入约为 5.1k token，structured 输入约为 3.6k token，原因是前者发送了完整的
动态 action tools；动态性并非零成本。

首次运行把 `thinking=on` 与 `tool_choice=required` 组合后，端点返回 HTTP 400：

```text
The tool_choice parameter does not support being set to required or object in thinking mode
```

把 thinking-on 组改为 `tool_choice=auto` 后通过。这个限制是本次 tokenplan 端点的实测行为，
不是通用 Tool Calling 规范。

## 2. 状态与动作架构 replay

`20260809_150134` 在 8 个不同 action case 上各运行一次。该历史报告记录了
`thinking=on`，但当时尚未把 `tool_choice` 写到报告顶层。

| Variant | 状态正确 | 有 tool | 协议有效 | 严格成功 | 语义动作正确 | 平均延迟 |
|---|---:|---:|---:|---:|---:|---:|
| `action_only` | N/A | 8/8 | 5/8 | 5/8 | 8/8 | 4.581s |
| `joint_content_tool` | 8/8 | 7/8 | 6/8 | 6/8 | 7/8 | 7.553s |
| `separate_two_call` | 8/8 | 8/8 | 7/8 | 5/8 | 6/8 | 12.137s |
| `recorded_intent_policy` | N/A（复用轨迹） | 8/8 | 8/8 | 8/8 | 8/8 | 3.930s |

逐 action 的严格成功数：

| Action | action only | joint | separate | recorded intent |
|---|---:|---:|---:|---:|
| `tap` | 0/2 | 1/2 | 1/2 | 2/2 |
| `type` | 1/2 | 2/2 | 2/2 | 2/2 |
| `select_option` | 2/2 | 2/2 | 0/2 | 2/2 |
| `scroll` | 2/2 | 1/2 | 2/2 | 2/2 |

这组小样本支持两个有限结论：

1. 给 Action Policy 一个已经确定的 intent，在这次 replay 中最稳定。
2. “同一 LLM 同时输出状态和动作一定降低 action 准确率”没有被证实；joint 反而略高于
   action-only 和 separate，但样本太少，且差异受协议失败与参数编码错误影响。

两次调用的 `separate_two_call` 没有自动获得更高准确率，并且平均延迟约为 joint 的 1.6 倍。
因此，状态/动作分层是否有价值，应与“是否使用两个模型调用”分开讨论。

## 3. Joint content + tool 的协议消融

三组 joint 实验均为 8 case × 3 samples。`content` 被要求输出结构化状态，tool call 被要求输出
action。结果揭示了明显的输出通道竞争：

| Thinking / tool choice | 有状态 content | 有 tool | 协议有效/严格成功 | 语义动作正确 |
|---|---:|---:|---:|---:|
| on / auto | 24/24 | 24/24 | 21/24 | 24/24 |
| off / auto | 24/24 | 0/24 | 0/24 | 0/24 |
| off / required | 0/24 | 24/24 | 0/24 | 16/24 |

在这个模型和端点上：

- `auto` 的含义确实是“允许不调用工具”。runner 的“必须有一个工具”只是响应后的评分约束，
  不能把 `auto` 变成 provider 侧强制。
- thinking off + auto 的 24 次响应全部选择了普通 content，完整输出状态，但漏掉 action tool。
- thinking off + required 的 24 次响应全部选择了 tool call，却没有状态 content。因此
  `required` 能强制动作通道，但不能保证同一消息仍输出状态文本。
- thinking on + auto 的 24 次响应都同时给出状态和 tool call，21 次严格通过；这说明 joint
  协议在当前端点可行，但不是 API 层保证，仍需显式检查与恢复策略。

thinking on + auto 的 3 次严格失败都发生在 `tap` 参数编码：模型选择了正确工具，也找到了目标，
却把单个 `x`/`y` 写成坐标数组。也就是说，失败来自 schema adherence，而不是漏工具或状态判断。

## 4. Action handoff 基线

`recorded_intent_policy` 使用通关轨迹中已经确定的 instruction，再以
`thinking=off + tool_choice=required` 生成动作：

| 指标 | 结果 |
|---|---:|
| 有 tool | 24/24 |
| tool 名正确 | 24/24 |
| 语义动作正确 | 24/24 |
| 参数合法、严格成功 | 18/24 |

6 次失败全部是两个 `tap` case 的坐标数组编码错误，每个 case 3 次。这进一步表明当前主要瓶颈
之一是坐标参数表示与 schema adherence；它不能直接归因于状态预测干扰 action。

## 5. Thinking、content 与显式 reasoning 的边界

本实验中的三个概念必须分开：

- `enable_thinking`：provider/model 的内部推理模式；实测 usage 中单独出现 reasoning token。
- assistant `content`：模型的普通可见输出通道；joint 实验用它承载状态 JSON，而不是内部 thinking。
- schema 中的 `reasoning` 或状态字段：模型显式生成、可见、可校验的业务解释；它只是输出任务的
  一部分，不等价于 provider 的内部推理。

本 runner 使用 Chat Completions 风格调用和 `extra_body={"enable_thinking": ...}`。它没有验证
Responses API 的 `reasoning={"effort": "minimal"}`，因此不能从这些结果推断 Responses API 的行为。

## 6. 对下一阶段架构实验的约束

这些结果适合作为完整主链路实验的协议约束，而不是最终架构裁决：

1. Master 可以动态定义一个 execution unit 的目标、成功条件和可用 tools；tools 应按当前任务
   裁剪，保留 tool call 相对静态 union schema 的动态优势。
2. Worker 应是带内部 loop 的动态 Statement，同时覆盖交互和非交互动作；本轮 replay 只验证了
   单帧决策协议，尚未验证 loop、观察更新、分支和终止。
3. 若采用单响应 `content=state + tool=action`，当前 tokenplan 安全组合只能先用
   thinking on + auto，并对 missing tool、missing state、非法参数做显式恢复。
4. 若动作必须 provider 侧强制，可将状态/目标作为 Master 的独立 handoff，再用
   thinking off + required 执行动作；不要假设 required 响应还能同时携带状态 content。
5. 下一轮必须跑完整纵向链路，但节点内部可以简化；不能用只读 Statement 的组件实验替代
   Master → 动态 Statement/Worker loop → action execution → observation → termination。

## 当前结论

Tool Calling 值得继续，核心价值是按任务动态暴露动作空间，而不是在静态 schema 中维护所有业务
分支。当前证据同时说明：动态 tools 不会自动解决参数准确率，joint content/tool 也受模型与端点
解码策略影响。下一阶段应保留完整 agentic 主链路，用严格协议校验和恢复机制包住这些不确定性。
