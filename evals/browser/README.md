# Browser evals

Browser 平台的 LLM 驱动按需评测（待建）。

镜像 `evals/iphone/` 的形态：每个核心模块一个子目录（`cases.json` + `test_<module>.py` +
`screenshots/`，截图被 gitignore）。候选模块：browser action policy、milestone supervisor
（web-tuned prompts）、router（browser intent）、scroll-collect/stitch on web pages、
WebArena agent_response 合成等。

运行约定与 iphone 一致：

```bash
uv run python evals/browser/<module>/test_<module>.py
```
