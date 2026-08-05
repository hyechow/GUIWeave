# 编排层 hard 基线实验记录（2026-08-05）

> 状态：实验记录（非架构规范）
>
> 架构边界以 [GUI Agent 当前架构与模块边界](orchestrator_module_boundaries.md) 为准。
> 本文只记录 **qwen3.7-plus / tokenplan** 上、去掉 planner 任务语义诊断之后的
> **静态 coding-orchestrator** 扫频结果，不代表 live WebArena / MobileWorld 跑分。

## 1. 实验背景

同日完成三件事，共同构成「编排层是否还站得住」的基线：

1. **删语义门**：移除 `planner._semantic_contract_diagnostics`（goal/MD 正则诊断），
   compile 只保留结构不变量；任务形态归 eval / prompt / knowledge（§13.1）。
2. **默认模型**：`config.yaml` 基线切到 `tokenplan` + `qwen3.7-plus`（视觉槽）/
   `qwen3.6-flash`（轻量槽）。
3. **两套静态扫频**（均不启浏览器 / 模拟器）：
   - Browser：WebArena-Verified **hard 单站点** shopping_admin + shopping
   - Android：MobileWorld **GUI-only** 全任务 catalog 编排

问题：在「不靠 compile 语义纠偏」的前提下，硬任务上编排生成+结构编译的通过率是否仍可用？

## 2. 共同设定

| 项 | 值 |
|----|-----|
| 日期 | 2026-08-05 |
| 模型 | `qwen3.7-plus`（orchestrator 等核心槽） |
| Provider | `tokenplan`（`TOKENPLAN_*`，阿里云 MaaS） |
| 并发 | `-j 5` |
| k | 1（每任务单样本） |
| 路径 | router → `resolve_intent` → `generate_code` → `validate_code` |
| **不包含** | Playwright / adb live、WebArena oracle、MobileWorld `env.eval` |

评分口径（浏览器 eval 已固化；MobileWorld 扫频本身只报 executable）：

| 维度 | 含义 |
|------|------|
| **executable** | 生成程序通过结构 `validate_code` 且 `plan.executable` |
| **curated contract** | 仅 14 个手写 admin 合同：executable **且** AST 形态匹配 |
| inferred contract | 97 个非 curated 的 baseline 程序形态标注，**不计分** |

## 3. Browser：WebArena-Verified hard 单站点

### 3.1 范围

官方 hard subset
（`webarena-verified/assets/dataset/subsets/webarena-verified-hard.json`）中
**纯单站点**：

| Site | N |
|------|--:|
| shopping_admin | 55 |
| shopping | 56 |
| **合计** | **111** |

落点：

- cases：`evals/browser/orchestrator/cases.json`
- harness：`evals/browser/orchestrator/test_orchestrator.py`
- 基线说明：`evals/browser/orchestrator/BASELINE.md`
- 索引：`evals/browser/orchestrator/baseline_qwen37_tokenplan_20260805.json`
- 合并报告：`logs/orchestrator_eval/20260805_hard_single_site_merged/report.json`

### 3.2 结果

| 指标 | 结果 |
|------|------|
| **executable** | **91 / 111（82.0%）** |
| **curated contract** | **12 / 14（85.7%）** |
| samples ok（上述两维合成） | 89 / 111 |
| shopping_admin executable | 45 / 55 |
| shopping executable | 46 / 56 |

按 grade：

| Grade | N |
|-------|--:|
| `executable_pass` | 77 |
| `executable_fail` | 20 |
| `contract_pass` | 12 |
| `contract_fail` | 2（42 `SLICE_STOP`；549 `ORDERED_CALL`/`LITERAL_REQUIRED`） |

### 3.3 读法

- **可行**作为 hard 单站点编排 **smoke + 回归网**（~82% executable）。
- shopping **无** `knowledge/browser/shopping/`，shopping 侧几乎是「裸 intent 生成」。
- 空 contract / non-curated 的 PASS **不等于**业务做对；业务形态只在 14 curated 上量。
- 97 个 inferred contract 已标注、**不晋升 curated**（避免把偶然可编译形状锁成金标准）。

复跑：

```bash
uv run python evals/browser/orchestrator/test_orchestrator.py -j 5 --compare-baseline
uv run python evals/browser/orchestrator/test_orchestrator.py --group curated --compare-baseline
```

## 4. Android：MobileWorld GUI-only 编排扫频

### 4.1 范围与工具

| 项 | 值 |
|----|-----|
| Catalog | MobileWorld backend GUI-only（剔除 `agent-mcp` / `agent-user-interaction`） |
| Backend | `http://192.168.1.101:6800` |
| 任务数 | **117** |
| 脚本 | `scripts/mobileworld_orchestrator_sweep.py` |
| 主 run | `logs/orchestrator_eval/android_mw_sweep/20260805_103146/` |
| 冒烟 | `.../20260805_103106/`（2 task，2/2） |
| 产出 | `report.json` + `SUMMARY.md` + `sources/<Task>.py` |

扫频**不做** AST contract 评分，只报 executable / diagnostics / 是否绑到本地 knowledge。
与 `evals/android/orchestrator`（9 个手写 contract 回归）互补：后者是窄回归，前者是 catalog 宽度。

### 4.2 结果

| 指标 | 结果 |
|------|------|
| **executable** | **91 / 117（77.8%）** |
| failed | 26 |
| 带本地 knowledge | 48 / 117 |
| 耗时 | 587.5s（`-j 5`） |

Knowledge 切片（注意：有 knowledge ≠ 更高 executable）：

| 子集 | executable |
|------|------------|
| `has_knowledge=True` | 35 / 48（**72.9%**） |
| `has_knowledge=False` | 56 / 69（**81.2%**） |

单 app vs 多 app：

| 子集 | executable |
|------|------------|
| 单 app | 46 / 55（83.6%） |
| 多 app | 45 / 62（72.6%） |

按主 app（任务 `apps[0]`）：

| App | exec | 备注 |
|-----|------|------|
| Settings / Clock / Chrome / Camera / Gallery / Contacts / Maps | 100% | 样本小、形态直 |
| Mail | 7/8 | |
| Taodian | 6/7 | |
| Files | 16/20 | |
| Mastodon | 30/38 | 体量最大 |
| Calendar | 12/18 | 多为跨 app |
| Messages | 3/6 | |
| Mattermost | 1/4 | 最弱主 app |

### 4.3 失败形态（26）

诊断码频次（一任务可多码，按任务去重计数）：

| Code | 任务数 | 解读 |
|------|------:|------|
| `UNSTRUCTURED_QUERY_FORBIDDEN` | 10 | 无 structured schema 仍 `ctx.query`（视觉/非表场景） |
| `STATE_ENTITY_MISMATCH` | 9 | query entity ≠ 当前 reach（Mattermost Channel/Message 尤多） |
| `COMMIT_TARGET_UI_REQUIRED` | 8 | 有 target commit 无 target-bound reach |
| `UNSAFE_IMPORT` / `UNSAFE_ATTRIBUTE` | 6–7 | 生成 `import re` 等 sandbox 禁项 |
| `TARGET_COMMIT_VALUES_REQUIRED` / `DIRECT_COMMIT_REQUIRED` / `COMMIT_TARGET_REQUIRED` | 5 | commit 边界/空 values/目标绑定 |
| `SCHEMA_FREE_SOURCE_REQUIRED` | 3 | schema-free commit goal 未消费 read 派生值 |
| `ACTIVE_UI_REQUIRED` | 2 | 缺 reach 或 loop 后 UI 失效 |

失败任务（完整表见该 run 的 `SUMMARY.md`），簇：

- **正则/禁 import**：`MastodonPostPollTask`、`MastodonNewFilterTask`、`MattermostBudgetApprovalPipelineTask`、`TextArrivalTimeTask`…
- **Mattermost 多步跨实体**：`*Deadline*` / `*Feedback*` / `*Conflict*` / `*ShiftCoverage*` → `STATE_ENTITY_MISMATCH`
- **无 schema 却 query**：`CVEmailTask`、`SendInterviewEmailTask`
- **commit/UI 血统**：`BidFileRenameTask`、`CancelMeetingTask`、`MattermostReplyToMessageTask`…

### 4.4 读法

- **可行**作为 MobileWorld GUI-only 的编排宽度基线：~78% executable，与 browser hard ~82% 同量级。
- **有 knowledge 子集更难**（72.9% < 81.2%）：knowledge 多落在跨 app / 条件集合题，不是 knowledge 有害，而是题更难。
- Mattermost、多 app Calendar 链路是当前最大结构痛点（entity 切换 + commit 血统）。
- `import re` 频发 → 生成侧/prompt 可压；**不要**为此放宽 sandbox 安全面。
- 本扫频 **不是** live MobileWorld 得分；executable PASS 只说明「吐出了结构合法 Program」。

复跑：

```bash
uv run python scripts/mobileworld_orchestrator_sweep.py -j 5
uv run python scripts/mobileworld_orchestrator_sweep.py --task OpenFlightModeTask
# 窄回归（9 cases + contract）
uv run python evals/android/orchestrator/test_orchestrator.py
```

## 5. 对照与结论

| 套件 | N | executable | 形态合同 |
|------|--:|------------|----------|
| Browser hard 单站点 | 111 | 91（82.0%） | curated 12/14 |
| MobileWorld GUI-only | 117 | 91（77.8%） | 扫频未评分；9-case eval 另计 |
| 当日 9-case Android 回归 | 9 | — | 8/9（`SumFileLinesTask` 仍红） |

**结论（编排层，非 live）：**

1. 去掉 goal/MD 语义 compile 门后，hard 任务上编排 **仍可用**（两端 executable ~78–82%）。
2. 正确性应继续拆开：**executable**（宽网）+ **少量 curated contract**（深网）；禁止把 inferred 形状批量升成计分金标准。
3. 下一刀优先在 **knowledge / 跨任务 prompt / eval contract**，不在 compile 加语义正则（边界文档 §13.1）。
4. Android 侧值得跟的结构债：Mattermost entity 切换、schema-free 场景少用 `query`、禁 `re` 的生成纪律。
5. Browser shopping 缺 knowledge 包是明显短板；补事实后再比 executable 才公平。

## 6. 产物索引

| 路径 | 内容 |
|------|------|
| `evals/browser/orchestrator/BASELINE.md` | Browser 评分口径与复用 |
| `evals/browser/orchestrator/cases.json` | 111 cases + baseline + contract |
| `evals/browser/orchestrator/baseline_qwen37_tokenplan_20260805.json` | 机器可读索引 |
| `logs/orchestrator_eval/20260805_hard_single_site_merged/report.json` | Browser 合并报告 |
| `scripts/mobileworld_orchestrator_sweep.py` | MW 扫频入口 |
| `logs/orchestrator_eval/android_mw_sweep/20260805_103146/` | MW 主 run（report/SUMMARY/sources） |
| `docs/orchestrator_module_boundaries.md` §13.1 | 新 case 变绿优先级 / 禁语义回流 |

## 7. 非目标（本文不回答）

- live WebArena score、MobileWorld `env.eval` 分数  
- 97 个 browser inferred contract 是否应手写晋升 curated（已决定：**先不写**）  
- 是否放宽 sandbox 以换 executable（**否**）  
