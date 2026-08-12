# GUIWeave

GUIWeave 是一个以 **Tool Agent Master** 为核心的本地 GUI 自动化运行时。当前仓库
对应 macOS Developer Preview：采用「Codex Skill + 本地 stdio MCP」发布，同时保留
WebArena 和 MobileWorld 的评测体系。

开发者预览版支持：

- macOS 上的 Chrome，通过 Playwright 或现有 Chrome CDP 会话运行；
- 通过 ADB 操作 Android 真机或模拟器；
- 每次运行的日志、事件轨迹、截图、动作可视化、HTML 报告和确定性 replay；
- Tool Agent、WebArena、MobileWorld 的单元测试和 evals。

这个版本只有一条正式运行路径。旧的 reviewed-Python、policy、router、supervisor
agent loop 不再属于本发行版。

## 架构

```text
Codex Skill
    └─ 本地 stdio MCP (`guiweave-mcp`)
         └─ ToolAgentService
              └─ Tool Agent Master / visual Workers
                   ├─ Browser adapter → Chrome / Playwright
                   └─ Android adapter → ADB

每次运行 → context + trace + screenshots + replay + HTML report
```

MCP 进程完全在本机运行，不开放网络端口；Codex 通过 stdin/stdout 与它通信。

## 环境要求

- macOS 13 或更高版本（首个 Developer Preview 的目标平台）
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- 在 `.env` 或 shell 中配置的 OpenAI-compatible 模型
- headed browser 任务需要 Chrome；headless 任务可使用 Playwright Chromium
- Android 任务需要 Android Platform Tools；`scrcpy` 仅用于可选镜像和动作覆盖层

安装运行时：

```bash
uv sync
uv run playwright install chromium
```

模型密钥只应放在本地 `.env` 或 shell 中，不要提交进仓库。

## 安装 Codex 插件

插件包位于 `plugins/guiweave-automation/`。在仓库根目录执行：

```bash
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

安装后重启 Codex。插件提供 `$guiweave-local-automation` Skill，以及以下本地 MCP
工具：

- `check_environment`
- `run_browser_task`
- `run_android_task`
- `get_run_result`

Skill 会要求 Codex 先做环境检查、严格保持用户任务边界，并在执行发送、购买、发布、
删除或账户设置修改等高影响动作前确认。更多说明见
[`plugins/guiweave-automation/README.md`](plugins/guiweave-automation/README.md)。

## 本地 CLI

首次运行前检查环境：

```bash
uv run guiweave check browser
uv run guiweave check android --adb-serial emulator-5554
```

使用可见 Chrome 时，先启动独立 CDP profile：

```bash
bin/launch_chrome_cdp
uv run guiweave run browser "打开账户页面并报告当前套餐"
```

headless browser 和 Android 示例：

```bash
uv run guiweave run browser "打开 example.com" --headless
uv run guiweave run android "打开设置并进入 Wi-Fi 页面" \
  --adb-serial emulator-5554
```

任务会操作当前已登录的本地界面。建议优先使用测试账户或独立 profile，并明确复核可能
发送、购买、发布、删除或修改账户设置的目标。

## 日志、报告与 replay

普通运行默认写入：

```text
logs/gui_agent/tool_agent/<platform>/<timestamp>/
```

目录通常包含 `context.json`、`tool_agent_trace.json`、`tool_agent_replay.json`、截图、
stdout/stderr 日志以及 `report.html`。可通过 `GUIWEAVE_LOG_ROOT` 修改日志根目录。

无需设备、浏览器、网络或模型即可 replay 已记录运行：

```bash
bin/replay_run logs/gui_agent/tool_agent/browser/<timestamp>
```

打开或重新生成 HTML 报告：

```bash
bin/report logs/gui_agent/tool_agent/browser/<timestamp>
```

## WebArena 与 MobileWorld

两个 benchmark 都使用同一套 Tool Agent 运行时：

```bash
bin/webarena 11
bin/webarena --headless 11

bin/mobileworld --list
bin/mobileworld OpenFlightModeTask
```

WebArena 资产和输出位于 `webarena-verified/`；MobileWorld 参考资产位于
`benchmark/mobileworld/`。benchmark 专属事实只放在对应 knowledge 或 harness 中，
不会写入 core prompt。

## 开发与验证

```bash
uv run pytest
uv run pytest tests/test_tool_agent_runtime.py
uv run pytest evals/browser/webarena_response/test_response_replay.py
```

核心运行时位于 `gui_agent/core/`，平台适配器位于 `gui_agent/adapters/`，报告位于
`gui_agent/reports/`，知识文件位于 `knowledge/`，确定性测试位于 `tests/`，评测用例
位于 `evals/`。

## 当前限制

- 当前只把 macOS 作为已测试宿主；Linux 和 Windows 打包尚未支持。
- Browser 和 Android 可用性依赖本机 Chrome/CDP 或 ADB 状态。
- 插件以源码形式分发，并通过 `uv` 启动本地 MCP server。
- GUI 自动化具有概率性；失败时应结合 HTML 报告和 replay 产物排查。

## License

见 [LICENSE](LICENSE)。
