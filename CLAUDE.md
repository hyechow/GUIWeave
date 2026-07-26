# GUIWeave

多平台 GUI Agent Runtime：像素感知 + LLM 决策执行真实界面，三平台 = iPhone(mirroir 镜像) / Browser(Playwright CDP) / Android(adb+scrcpy)。

## 架构边界

- **平台选择**:环境变量 `AGENT_PLATFORM`(`iphone` 默认 / `browser` / `android`)。`core/runtime/factory.build_platform()` 惰性装配该平台的 `PlatformBundle`(session/executor/perception/policy/supervisor),core **只依赖 runtime contracts + factory**,不直接 import 任何 adapter。
- **接新平台**:在 `adapters/<plat>/` 实现 `core/runtime/contracts.py` 的 Protocol(Device/Perception/ActionPolicy/SupervisorPolicy)+ 写 `build_<plat>_bundle` 注册进 `build_platform`。
- **Reviewed-Python 编排器** (`core/orchestrator/`)：Planner 生成并审查受限 Python；
  `CodingProgramRuntime` 执行控制流并把 `ctx.*` 调用转换成类型化 Statement。Statement executor
  只负责当前语义目标，不改写剩余 Program。

## 常用命令

```bash
uv sync                        # Python 依赖;iPhone 后端另需: brew tap jfarcand/tap && npx -y mirroir-mcp install
uv run pytest tests/ -q        # 回归闸(确定性单测+契约 conformance,无需真机,每次改动后跑)

# iPhone(默认平台)
uv run python -m gui_agent.core.runner "打开微信并进入通讯录" --auto-continue --supervisor milestone
# Browser:先起带远程调试的 Chrome(独立 profile),登录目标页后跑 agent
bin/launch_chrome_cdp          # 默认端口 9222;非默认: PORT=<port> bin/launch_chrome_cdp + export CHROME_CDP_URL=...
AGENT_PLATFORM=browser uv run python -m gui_agent.core.runner "点击右上角设置按钮" --auto-continue
# Android:USB 或 `adb tcpip 5555` 转无线;adb/scrcpy 用 vendor/ 独立静态包(bin/* 自动指 ADBUTILS_ADB_PATH)
ANDROID_SERIAL=<序列号|host:port> bin/runner android "打开设置"    # 只连一台可不设
bin/android-key back|home|menu|recents
bin/scrcpy <serial> --off      # 可选镜像窗口(HUD/cursor 叠其上)
# 可见性开关(三平台统一):--headless / AGENT_HEADLESS=1 = 全后台,屏蔽 HUD 与动作可视化
# iPhone 应用结构探测:bin/iphone_recon --app 微信 --depth 2(--hud 状态面板;--export 导出知识)
```

## 约定

- `tests/` 是确定性回归闸 + `core/contracts` 契约 conformance(每次改动后跑,保平台中性边界);`evals/` 是 LLM 驱动的按需评测。
- 改 adapter 不应回归另一平台;core 改动三平台都受影响。
- 搬运模块时同时 grep `gui_agent.X` 与 `from gui_agent import X` 两种导入形式,并查 `__file__`/`parents[N]` 深度假设。
- **结构优先**：任务策略写在 reviewed Python 中；core 只保留跨任务成立的类型、数据流和
  平台中性约束。不要在 gate 或底层 `ctx.*` 实现里加入 case 文本匹配。新增静态检查时使用
  AST、类型或结构化数据流不变量，并用泛化测试锁定。
