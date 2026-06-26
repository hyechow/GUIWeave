# Android Mini Benchmark

轻量真机 benchmark，用 runner 的编排器模式跑可拆分的 Android 子任务。这里的任务会真实启动
`bin/runner android ... --orchestrator`，因此不放在 `evals/`；`evals/` 只保留离线/LLM 单点回归。

## 运行

```bash
HEADLESS=1 ANDROID_SERIAL=<serial> uv run python benchmark/android-mini/run.py
```

也可以只跑一个 case：

```bash
HEADLESS=1 ANDROID_SERIAL=<serial> uv run python benchmark/android-mini/run.py --label github-repo-discovery-androidworld
```

这个 mini benchmark 默认按 case 的 `runner_args` 透传 `--orchestrator --no-router`，
用于隔离测试编排器执行层；入口 router 分类问题应单独收录。

每个 Android case 可以声明 `init_script`。脚本在 runner 启动前执行，用来设置真机初始
状态；例如 `benchmark/android-mini/init/open_chrome_url.sh` 可以把 Chrome 打开到指定
URL，并按 case 配置选择保留 Chrome 前台，或 force-stop 后回到 Launcher。只
`force-stop` 不够，因为 Chrome 会恢复上一次打开的 tab。

## Cases

| label | layer | goal | assertion |
| --- | --- | --- | --- |
| github-repo-discovery-androidworld | CheckGithubInfoTask / step 1 | 帮我找到 AndroidWorld 这个 Android GUI agent benchmark 项目的 GitHub 仓库。 | 当前 Android UI 文本必须处在 `google-research/android_world` 仓库详情页，搜索结果页不算通过 |
| github-repo-info-androidworld | CheckGithubInfoTask / step 2 | 帮我看下 AndroidWorld 这个 Android GUI agent benchmark 的 GitHub 仓库现在有多少 stars 和 contributors，直接告诉我两个数字。 | 最终输出必须同时包含 stars 和 contributors 的数量表达；UI 仍需停留在 GitHub 相关页面，不走 API/JSON |
