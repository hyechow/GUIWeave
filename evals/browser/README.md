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
| orchestrator | 13 | 无浏览器的 coding-orchestrator 静态回归：5 个查询任务与 8 个表单提交任务，检查 API、顺序、字段、条件、类型和返回值契约。 |
| perception | fixtures | 浏览器结构感知和原生控件暴露。 |
| router | cases.json | 任务路由与目标标准化。 |

候选待建模块：scroll-collect/stitch on web pages、WebArena agent_response 合成等。

Orchestrator 两组任务可以独立运行：

```bash
uv run python evals/browser/orchestrator/test_orchestrator.py --group query
uv run python evals/browser/orchestrator/test_orchestrator.py --group form
```
