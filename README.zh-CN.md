# GUIWeave

GUIWeave 是一个以 **Tool Agent Master** 为核心的本地 GUI 自动化运行时。当前仓库
对应 macOS Developer Preview：采用「Codex Skill + 本地 stdio MCP」发布，同时保留
WebArena 和 MobileWorld 的评测体系。

开发者预览版支持：

- macOS 上的 Chrome，通过 Playwright 或现有 Chrome CDP 会话运行；
- 通过 ADB 操作 Android 真机或模拟器；
- 通过 macOS iPhone 镜像操作 iPhone；截图固定走 `bin/sck_server`，输入固定走
  `bin/mirror_daemon`；
- 每次运行的日志、事件轨迹、截图、动作可视化、HTML 报告和确定性 replay；
- Tool Agent、WebArena、MobileWorld 的单元测试和 evals。

这个版本只有一条正式运行路径。旧的 reviewed-Python、policy、router、supervisor
agent loop 不再属于本发行版。

## 架构

```text
Codex Skill
    └─ 本地 stdio MCP (`guiweave-mcp`)
         └─ ToolAgentService
              ├─ 确定性 App Router → 应用/站点知识绑定
              └─ Tool Agent Master / visual Workers
                   ├─ Browser adapter → Chrome / Playwright
                   ├─ Android adapter → ADB
                   └─ iPhone adapter → sck_server + mirror_daemon

每次运行 → context + trace + screenshots + replay + HTML report
```

App Router 直接从用户目标中的应用名称/alias 识别目标，并结合当前 URL 或平台应用标识
绑定知识。它不要求用户预选应用、不调用模型、不改写用户目标，也不是已经移除的旧
Router Agent。

MCP 进程完全在本机运行，不开放网络端口；Codex 通过 stdin/stdout 与它通信。

## 环境要求

- Browser/Android 需要 macOS 13 或更高版本；iPhone helper 开发者预览版仅支持
  搭载 M 系列芯片的 Apple Silicon Mac，并以 macOS 26 为目标
- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- 在 `.env` 或 shell 中配置的 OpenAI-compatible 模型
- headed browser 任务需要 Chrome；headless 任务可使用 Playwright Chromium
- 插件已包含 Android `adb` 和 arm64 `scrcpy` 4.0；`scrcpy` 仅用于可选镜像和动作
  覆盖层，Intel Mac 会回退使用 PATH 中的兼容版本
- iPhone 任务需要 M 系列 Mac 和 macOS iPhone 镜像应用；插件已包含
  `sck_server` 和 `mirror_daemon`

安装运行时：

```bash
git submodule update --init --recursive webarena-verified
uv sync
uv run playwright install chromium
```

配置模型网关（推荐使用仓库提供的 OpenAI-compatible `standard` 模板）：

```bash
cp .env.example .env
```

然后在 `.env` 中至少替换：

```dotenv
AGENT_CONFIG=config.standard.yaml
STANDARD_BASE_URL=https://你的模型网关/v1
STANDARD_API_KEY=你的密钥
```

`config.standard.yaml` 已分别声明 Master、Worker、Perception、Presentation、Loading
和 Target Verify 使用的模型。Worker、Perception 和视觉校验槽位会发送截图，因此网关中
对应模型必须支持 OpenAI-compatible 图片输入。也可选择 `config.tokenplan.yaml` 并配置
`TOKENPLAN_BASE_URL` / `TOKENPLAN_API_KEY`。模型密钥只应放在本地 `.env` 或 shell 中，
不要提交进仓库；修改后需要重启 Run Console 或 Codex。

统一前置检查会同时检查模型和所选平台：Browser 检查 Chrome CDP（headless 除外）；
Android 检查 `adb` 设备，并提示可选 `scrcpy` 和 ADBKeyboard 状态；iPhone 检查是否为
M 系列 Mac、两个插件 helper 的存在、可执行权限与 Gatekeeper 状态，以及 iPhone 镜像窗口。
Android 连接成功后会执行 `svc power stayon true`，使设备在充电时保持亮屏并降低无线
ADB 因深度休眠断开的概率；它不会解除锁屏，因此仍应关闭自动锁屏或延长休眠时间。
可用 `adb -s <设备地址> shell svc power stayon false` 恢复默认设置。

## 安装 Codex 插件

repo marketplace 插件位于 `plugins/guiweave-automation/`，需要从完整仓库 clone
安装。在仓库根目录执行：

```bash
codex plugin marketplace add .
codex plugin add guiweave-automation@guiweave-dev
```

安装后重启 Codex。插件提供 `$guiweave-local-automation` Skill，以及以下本地 MCP
工具：

- `check_environment`
- `run_browser_task`
- `run_android_task`
- `run_iphone_task`
- `get_run_result`
- `preview_knowledge_document` / `get_knowledge_draft`
- `commit_knowledge_draft`
- `list_user_knowledge` / `get_user_knowledge`

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

打开 iPhone 镜像后，iPhone 使用相同的 Tool Agent 入口：

```bash
uv run guiweave check iphone
uv run guiweave run iphone "打开设置并返回当前可见的 Apple ID 名称"
```

任务会操作当前已登录的本地界面。建议优先使用测试账户或独立 profile，并明确复核可能
发送、购买、发布、删除或修改账户设置的目标。

## 本地 Run Console

启动本地任务管理界面：

```bash
uv run guiweave console
```

然后访问 `http://127.0.0.1:7468`。Console 支持按平台启动任务、查看实时结构化事件、
请求安全取消，以及打开本地报告、trace、replay 和 stdout/stderr 日志。同一平台同一时间
只允许一个活跃任务，服务只监听本机 loopback 地址。打开新建任务窗口或切换平台时，
Console 会显示模型、CDP/adb/iPhone helper 的检查结果；硬依赖未就绪时不会启动任务。
选择 Android 后可直接填写设备 IP、`IP:端口` 或 adb serial；裸 IP 默认连接 5555
端口。该地址同时用于前置检查和实际任务，留空时才会自动选择唯一已连接设备。

运行环境和产物在本机，但模型推理不一定离线：任务会使用 `.env` 或 shell 中配置的模型
网关与 API key。

## 导入用户文档

插件可以把本地 PDF、Markdown 或 UTF-8 文本文档转换为私有 knowledge 草稿。Codex
会先展示生成的 `_app.md` 和功能章节；只有用户在后续消息中明确确认，草稿才会生效。
扫描版 PDF 需要先进行 OCR。

macOS 上提交后的用户知识默认存放于
`~/Library/Application Support/GUIWeave/knowledge/`，不写入仓库。可通过
`GUIWEAVE_KNOWLEDGE_ROOT` 指定其他私有目录。同平台、同应用名称发生冲突时，用户知识
优先于内置知识。

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

如果 clone 时没有初始化 WebArena，请运行：

```bash
git submodule update --init --recursive webarena-verified
```

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
- 三个平台分别依赖本机 Chrome/CDP、已连接的 Android 设备，或 M 系列 Mac 上可见的
  iPhone 镜像窗口；设备侧可执行程序已纳入插件包。
- 仓库内 iPhone helper 是本地源码预览构建。对外分发可下载的插件包之前，必须使用
  Developer ID 对两个 helper 签名并完成 notarization；前置检查会阻止被 Gatekeeper
  拒绝且带下载隔离标记的 helper。
- repo marketplace 插件通过 `uv` 启动本地 MCP server，并依赖完整 GUIWeave
  checkout；不能只复制插件目录作为独立安装包。
- GUI 自动化具有概率性；失败时应结合 HTML 报告和 replay 产物排查。

## License

见 [LICENSE](LICENSE)。
