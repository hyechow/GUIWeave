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
| planner | 1 | 步骤规划器：截图+子目标→下一步指令。覆盖「打开网址应指令『导航到 X』而非『在搜索框输入网址』」（真正的回归点：planner 上游决定 navigate vs type） |
| action_policy | 3 | 网页动作策略：截图+指令→单个动作。覆盖「导航指令→navigate（而非把 URL type 进页面搜索框）」，含一个防 navigate 过触发的对照 case |

候选待建模块：milestone supervisor（web-tuned prompts）、router（browser intent）、
scroll-collect/stitch on web pages、WebArena agent_response 合成等。
