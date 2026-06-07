# 运行时延迟分析

任务：`打开微信账单`（主屏 → 微信 → 我 → 服务 → 钱包 → 账单），agent-loop 实机跑。
数据来源：每轮 `[Timing]`（decompose/checker/planner/action_policy）+ `[Settle]`
日志 + `context.json` 里的 `timings` 与 `settle_s`。

## 每轮成本模型

```
一个 action 轮  =  decide  +  settle  +  截图/执行/杂项
                    ↑           ↑          ↑
        3 次串行 LLM      等真机经镜像     ~0.3s
   checker→planner→action   渲染稳定
   (后者依赖前者，无法并行)  (物理延迟)
```

- decompose 只在第 1 轮调用一次（任务分解）。
- `SkipCheck`：进入导航子目标时跳过首次验收，advance 轮只 1 次 checker。
- settle：首帧等 1.0s（`SETTLE_FIRST_S`），之后每 0.5s 轮询，直到「相对动作前帧变过 且 相对上一帧停稳」。2 轮≈1.7s，3 轮≈2.2s。

## 三次对比跑（同任务）

| | 195341 (qwen3.5) | 201058 (qwen3.6) | 201707 (qwen3.6) |
|---|---|---|---|
| 子目标数 | 4 | 5 | 5 |
| 轮数 | 7 | 7 | 7 |
| decompose（一次性） | 5.0s | 6.5s | **3.2s** |
| decide/轮（稳态中位） | ~3.5s | ~2.6s | ~2.5s |
| settle/轮（稳态） | ~1.7s（2 轮） | ~1.75s | **~2.2s（3 轮）** |
| Σ LLM（含 decompose） | 25.4s | 25.3s | 21.3s |
| Σ settle | 8.3s | 10.5s | **13.0s** |
| **全程（≈Σllm+Σsettle+~2s 杂项）** | ~36s | ~38s | ~36s |

> 注：decompose 与子目标拆解粒度（4 vs 5）是 decomposer 的运行间抖动（同一模型也会拆出不同步数），
> 直接影响轮数与总时长，并非速度问题。

## 类别占比（以 195341 为例）

| 类别 | 耗时 | 占比 |
|---|---|---|
| LLM（decompose + 每轮 decide） | 25.4s | ~71% |
| settle（真机渲染） | 8.3s | ~23% |
| 截图 + 执行 + 杂项 | ~2s | ~6% |

## 模型对比：qwen3.5 → qwen3.6（supervisor / action_policy / back_nav）

每次 LLM 调用明显更快：

| 调用 | 3.5 | 3.6 |
|---|---|---|
| checker | 1.0–2.0s | 0.7–1.6s（多数 ~0.9） |
| planner | 1.0–1.2s | 0.7–0.85s（偶发抖到 1.8–2.6） |
| action_policy | 0.9–1.2s | 0.6–0.72s |
| **decide/轮** | **~3.5s** | **~2.5s（−26%）** |

**3.6 是更优选择**，每轮 decide 稳定降 ~1s。但三跑总时长持平，原因是：
1. 201058/201707 被 decomposer 拆成 5 个子目标（多一整轮 + settle）；
2. 201707 的 settle 普遍跑到 3 轮（真机这次稳得慢），把 LLM 省下的 ~5s 又吃回去。

## 成本对比：3.6 更快但更贵（阿里云百炼定价，每百万 token）

| 模型 | 输入 | 输出 | 备注 |
|---|---|---|---|
| **qwen3.5-35b-a3b** | **0.4 元**（≤128K）/ 1.6 元（128K-256K） | **3.2 元** / 12.8 元 | 我们的输入（截图~1-2k + prompt）远低于 128K，吃便宜档 |
| **qwen3.6-35b-a3b** | **1.8 元**（≤256K，单档） | **10.8 元** | 无便宜档，思考/非思考同价 |

→ **3.6 单 token 成本约为 3.5 的 4.5×（输入）/ 3.4×（输出）**。

**权衡**：3.6 每轮 decide 快 ~26%（~1s/轮），但 token 成本涨 ~3-4.5×。
- **默认/批量测试**：用 `config.yaml`（qwen3.5，便宜）。
- **延迟敏感 / 对比验证**：`AGENT_MODEL=qwen36`（profile 覆盖核心模型为 3.6）。
- 决策时按场景权衡：低量、重延迟 → 3.6；成本敏感、批量回归 → 3.5。

### 横向对比 OpenAI（汇率 ~7.1 RMB/USD，每百万 token）

| 模型 | 输入 $/M | 输出 $/M | vs qwen3.6 |
|---|---|---|---|
| qwen3.5-35b-a3b | $0.056 | $0.45 | 0.2× / 0.3× |
| **qwen3.6-35b-a3b** | **$0.25** | **$1.52** | 1× / 1× |
| GPT-5-Codex | $1.25 | $10.00 | 5× / 6.6× |
| GPT-5.2 / 5.3-Codex | $1.75 | $14.00 | 7× / 9.2× |
| GPT-5（标准） | $1.25 | $10.00 | 5× / 6.6× |
| GPT-5.5 | $5.00 | $30.00 | 20× / 20× |

→ **Codex / GPT-5 系是 qwen3.6 的 5–9×，是默认 qwen3.5 的 ~22× 以上。** 两个 caveat：
1. **Codex 是编码模型，非 GUI 视觉模型**——本 agent 每轮要读截图做 grounding，Codex 未必适配（要对标得看通用多模态 GPT-5）。
2. **本 workload 视觉重、输出极小**（每轮一张截图输入 + ~200 字 JSON 输出）；OpenAI **图像输入单独高价（~$8/M 图像 token）**，而 qwen 35b-a3b 视觉走文本档——**输入侧真实差距比上表文本倍数更大**。

**实测对照（codex computer use，同任务）**：平均 **~5s/turn**，其中 LLM 推理 **~3s**。
速度上 **比 qwen3.5（decide ~3.5s）快，但不及 qwen3.6（~2.5s）**。

→ 即 **codex 比 qwen3.6 既慢（~3s vs ~2.5s decide）又贵（~5-9× token）**——在速度和成本两个维度都被 qwen3.6 压制，无可取之处。

| 方案 | decide/轮 | token 成本（vs qwen3.6） |
|---|---|---|
| qwen3.5 | ~3.5s | 0.2-0.3× |
| **qwen3.6** | **~2.5s** | **1×** |
| codex computer use | ~3s | ~5-9× |

**结论**：对截图驱动的 GUI agent，OpenAI/codex 系既贵一个数量级、图像侧更甚，速度还慢于 qwen3.6，视觉适配也存疑；**留在 qwen 档（3.5 省 / 3.6 快）是性价比最优解**。

> 定价来源（2026-05 查）：[OpenAI Codex rate card](https://help.openai.com/en/articles/20001106-codex-rate-card)、
> [pricepertoken gpt-5-codex](https://pricepertoken.com/pricing-page/model/openai-gpt-5-codex)、
> [OpenAI API Pricing](https://openai.com/api/pricing/)；阿里云百炼 qwen3.5/3.6 官方定价页。

## 关键发现：瓶颈已从 LLM 转移到 settle

3.6 让「思考」变快后，**settle（真机经 Mac 镜像渲染的物理等待）成了占比 ~23–35%、且波动最大的一项**——
2 轮 vs 3 轮直接差 0.5s/轮 ×6 ≈ 3s。它与模型无关，软件层只能设地板/上限，压不动实际渲染速度。

```
每个 action 轮 ≈ decide 2.5s（模型，已接近地板） + settle 2.2s（真机，最大且最不稳）
```

## 已做的提速优化（均已落库 + 回归用例）

| 优化 | 效果 |
|---|---|
| `SkipCheck`：导航子目标进入时跳过首次验收 | advance 轮 2 次 checker → 1 次 |
| kind-scoped done 守卫 | 导航 done 不再因 visible_evidence 空而重试（省 ~1s）；action done 仍校验抓 wrong-done |
| done 守卫递归封顶 1 次 | 最多 2 次调用，杜绝无界重试（曾观测到 4 次） |
| decomposer「到达页面=navigation」分类 | 避免 action 误标使 SkipCheck / nav-done 优化失效 |
| 动态 settle 间隔（首帧 1s 后 0.5s） | 挡住转场动画中途采样的假停稳，避免浪费整轮 |
| YOLO+OCR 与 decide 并行预算 | 吸附计算移出关键路径，~0.4s/轮 |
| qwen3.6 可选（`AGENT_MODEL=qwen36`） | 每轮 decide −26%，但成本 ~3-4.5×；默认仍用 3.5 |

## 结论与后续空间

**通用模式下（每轮全套 感知→决策→执行 + 真机 settle），~36s / 5 动作 已接近这套架构的极限。**
浪费（冗余 check、重试、误吸附、动画误判）基本榨干，剩下的是真实成本：模型推理 + 真机渲染。

再要 2–3× 提速，需结构性改造，非调参：

| 方向 | 量级 | 代价 |
|---|---|---|
| 合并 LLM 调用（checker+planner 或 +action_policy，3→2/1 次） | −1~1.5s/轮 | 中高：精度 / 状态机风险 |
| checker 换小/快/本地模型（只是判断，不必 35B） | checker 1~2s → <0.5s | 中：幻觉风险 |
| **双模式：已知路径跑「执行模式」**（连发预规划 tap，每步轻校验、不每步全套 LLM） | 整任务 36s → 十几秒 | 需 recon/knowledge 成熟 |
| settle 物理延迟 | 受真机/镜像限制 | 软件压不动 |

最大的一招是**双模式执行**：路径一旦被 recon 学会，执行时跳过每步的完整「感知→决策」，
直接连发动作 + 轻量校验——这才是把 36s 压到十几秒的结构性解法。
