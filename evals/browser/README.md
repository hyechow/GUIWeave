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
| orchestrator | 3 | DSL compiler（`orchestrator.decompose`）：goal→run/if/finish 程序。覆盖 confirm-read dispatch/defer 门与登录/认证前置终态建模。 |

候选待建模块：scroll-collect/stitch on web pages、WebArena agent_response 合成等。
