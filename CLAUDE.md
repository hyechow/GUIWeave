# GUIWeave

多平台 GUI Agent Runtime：像素感知 + LLM 决策执行真实界面，三平台 = iPhone(mirroir 镜像) / Browser(Playwright CDP) / Android(adb+scrcpy)。

## 架构边界

- **平台选择**:环境变量 `AGENT_PLATFORM`(`iphone` 默认 / `browser` / `android`)。`core/runtime/factory.build_platform()` 惰性装配该平台的 `PlatformBundle`(session/executor/perception/policy/supervisor),core **只依赖 runtime contracts + factory**,不直接 import 任何 adapter。
- **接新平台**:在 `adapters/<plat>/` 实现 `core/runtime/contracts.py` 的 Protocol(Device/Perception/ActionPolicy/SupervisorPolicy)+ 写 `build_<plat>_bundle` 注册进 `build_platform`。
- **DSL 编排器**(`core/orchestrator/`,browser 侧主战场):GUI 任务 = 混合脚本生成；Interpreter 统一分派 statement，Milestone 是交互 Run 的闭环 executor。工具链地图见 `core/orchestrator/__init__.py`，架构见 `docs/dsl_runtime_architecture.md`。

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
- **结构优先制度(2026-07-06 立,orchestrator 可靠性投入的准入规则)**:往 validator/preflight 加规则、往 passes 加 gate、或新增任何恢复/重试机制之前,**必须先回答"能否用结构实现"**——① schema 上不可表示(如 S8 的 kind 收窄)> ② 结构化谓词/类型(如 return_domains)> ③ 统一账本/异常分类 > ④ 事后文本规则(最后手段)。落到④时:validator 规则必须带触发样例(registry 测试强制)且事后用 `scripts/validator_retry_efficacy.py` 度量清除率,低效规则退役或降级为执行期改写;prompt 侧新知识优先写进 worked example 而非规则条文(范例采纳实证 12:0);evals 新断言一律用语义不变量(AST/数据流可判),禁止字面词表(两天三冤案)。背景见 `docs/dsl_runtime_architecture.md`。
