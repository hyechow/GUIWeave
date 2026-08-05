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
| action_policy | 8 | 网页动作策略：截图+指令→单个动作。覆盖导航、站内搜索、输入、上传，以及输入指令不得退化成滚动。 |
| intent_resolver | cases.json | 用户目标中的实体、精确匹配与检索词解析。 |
| execution_signal | cases.json | 浏览器动作执行与响应信号。 |
| orchestrator | 111 | WebArena-Verified **hard 单站点**：admin 55 + shopping 56。评分只看 **executable（全量）** 与 **curated contract（14）**；其余 97 个带 baseline 推断 contract 仅作标注。见 `orchestrator/BASELINE.md`。 |
| perception | fixtures | 浏览器结构感知和原生控件暴露。 |
| router | cases.json | 任务路由与目标标准化。 |

候选待建模块：scroll-collect/stitch on web pages、WebArena agent_response 合成等。

Orchestrator 可按站点或 curated 子集运行（默认 `-j 5` 并发）：

```bash
uv run python evals/browser/orchestrator/test_orchestrator.py -j 5 --compare-baseline
uv run python evals/browser/orchestrator/test_orchestrator.py --group admin
uv run python evals/browser/orchestrator/test_orchestrator.py --group shopping
uv run python evals/browser/orchestrator/test_orchestrator.py --group curated
uv run python evals/browser/orchestrator/test_orchestrator.py --task 108 549
```

正确率解读与 baseline 标注：`evals/browser/orchestrator/BASELINE.md`。
