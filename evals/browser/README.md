# Browser evals

Browser 平台的 LLM 驱动按需评测。

镜像 `evals/iphone/` 的形态：每个核心模块一个子目录（`cases.json` + `test_<module>.py` +
`screenshots/`，截图被 gitignore）。

运行约定与 iphone 一致：

```bash
uv run python evals/browser/<module>/test_<module>.py
```

## 模块一览（browser）

| 模块 | Cases | 说明 |
|------|------:|------|
| planner | 14 | 步骤规划器：截图+子目标→下一步指令。覆盖 navigate/type 分派、下拉候选、上传、网格筛选、面板滚动、折叠区 acquire 等回归。 |
| action_policy | 8 | 网页动作策略：截图+指令→单个动作。覆盖导航、站内搜索、输入、上传，以及输入指令不得退化成滚动。 |
| decomposer | 3 | **DAG** 分解器（`MilestoneSupervisorPolicy._decompose`）：goal+截图→milestone 列表。条件创建/移车入口/连通验收态。 |
| orchestrator | 3 | **DSL** 分解器（`orchestrator.decompose`，`--orchestrator` 用）：goal→run/if/finish 程序。测 prompt(L1) 质量——①「confirm-read 撑腰的 action 验收=dispatch/defer 门，别和 read 双判同一结果」（回归 20260615_100753，检测+创建两类，词表无关；dispatch 门在生产由 `engine.normalize_confirm_read_gates`(L2) 确定性兜底，见 `tests/test_orchestrator.py`）；②「登录/认证前置用终态建模，别写已登录会话不可达的『登录表单可见』门」（回归 20260615_153314 卡死）。FAIL=prompt 软信号非生产 bug。无截图（编排在 turn1 前分解）。 |

候选待建模块：milestone supervisor（web-tuned prompts）、router（browser intent）、
scroll-collect/stitch on web pages、WebArena agent_response 合成等。
