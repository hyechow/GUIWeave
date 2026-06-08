# iphone-use

多平台 **GUI Agent**:在像素感知上用 LLM 决策 + 执行,操作真实界面。目前支持两个平台:

- **iPhone**(屏幕镜像):经 `mirroir-mcp` / 自有 mirror daemon 截图 + 零抢占输入。
- **Browser**(Playwright over CDP):接管已开远程调试的 Chrome,vision-only(MVP)。

> 名字 `iphone-use` 是项目早期的遗留;现在是平台中性的 core + 各平台 adapter。

## 架构(core + adapter)

```
gui_agent/
  core/        平台中性:contracts(Protocol 缝)· factory(平台工厂)· schema/schemas
               · runner/chat_cli(agent loop 编排)· supervisor · policies/base
               · reader/output/temporal/trace/prefs/... · self_learning
  adapters/
    iphone/    镜像 I/O · executor · perception(SCK)· recon(YOLO/OCR 像素探测)
               · text_input/ocr · 具体 action policy / supervisor · recon_cli
    browser/   PlaywrightDevice(CDP)· perception · executor · BrowserActionPolicy · factory
```

- **平台选择**:环境变量 `AGENT_PLATFORM`(`iphone` 默认 / `browser`)。`core/factory.build_platform()` 惰性装配该平台的 `PlatformBundle`(session/executor/perception/policy/supervisor),core **只依赖 contracts + factory**,不直接 import 任何 adapter。
- **接新平台**:在 `adapters/<plat>/` 实现 `core/contracts.py` 的 Protocol(Device/Perception/ActionPolicy/SupervisorPolicy)+ 写 `build_<plat>_bundle` 注册进 `build_platform`。

## 环境配置

```bash
# iPhone 后端依赖
brew tap jfarcand/tap
npx -y mirroir-mcp install
# Python 依赖(含 playwright)
uv sync
```

## 运行

```bash
# iPhone(默认平台)
uv run python -m gui_agent.core.runner "打开微信并进入通讯录" --auto-continue --hud --supervisor milestone

# Browser:先起带远程调试的 Chrome(独立 profile,默认 profile 开不了 CDP),在弹出窗口登录目标页,再跑 agent:
bin/launch_chrome_cdp   # 独立 profile + 关遮挡节流(遮住也持续出帧→截图快、非抢占);默认端口 9222
AGENT_PLATFORM=browser uv run python -m gui_agent.core.runner "点击右上角设置按钮" --auto-continue
# 非默认端口:PORT=<port> bin/launch_chrome_cdp,agent 侧 export CHROME_CDP_URL=http://localhost:<port>
# 动作可视化:默认复用 agent_cursor OS 覆盖层(蓝箭头,画在页面外不污染截图);BROWSER_VISUALIZER=dom 切到页内 DOM 覆盖

# iPhone 应用结构探测(recon mode):零抢占 daemon + agent 光标;参数透传 recon_cli
bin/iphone_recon --app 微信 --depth 2          # 加 --hud 显示状态面板;--export 微信 导出知识

# 回归闸(确定性单测 + 契约 conformance,无需真机)
uv run pytest tests/ -q
```

## 技术栈

- Python 3.11+,`uv` 包管理
- `mcp` — 连接 mirroir-mcp(iPhone 后端)
- `playwright` — Browser 后端(connect_over_cdp 接管 Chrome)
- `anthropic` / `langchain-*` — LLM(决策/监督/路由)
- `ultralytics`(YOLO)· `ocrmac`(Vision OCR)— iPhone 像素感知
- `fastapi` + `uvicorn` — HTTP server

## 约定

- `tests/` 是确定性回归闸 + `core/contracts` 契约 conformance(每次改动后跑,保平台中性边界);`evals/` 是 LLM 驱动的按需评测。
- 改 adapter 不应回归另一平台;core 改动两平台都受影响。
- 搬运模块时同时 grep `gui_agent.X` 与 `from gui_agent import X` 两种导入形式,并查 `__file__`/`parents[N]` 深度假设。
