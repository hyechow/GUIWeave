# Android evals

Android 平台的 LLM 驱动按需评测。

镜像 `evals/iphone/` · `evals/browser/` 的形态：每个核心模块一个子目录
（`cases.json` + `test_<module>.py` + `screenshots/`，截图被 gitignore，本地放）。

运行约定与其它平台一致：

```bash
uv run python evals/android/<module>/test_<module>.py
```

## 模块一览（android）

| 模块 | Cases | 说明 |
|------|------:|------|
| action_policy | 10 | 手机动作策略：截图+指令→单个动作。覆盖滚轮时间 picker「改值用 scroll、绝不 tap 选中数字」，并收录 20260611_095209/104134/111632 的 picker 方向/列锚点/幅度回归：分钟列 52→30、21→30 反向纠偏、小时 05→06 小步调整、小时 09→06 必须 small 防震荡、小时 11→06 与分钟 17→30 远距离应放大幅度；含一个点击列表项仍 tap 的对照 case，防 picker 规则把一切都变成 scroll |
| router | 2 | Android 任务入口改写：保留直接设备动作，并把浏览器读取目标细化到正确时间范围而不绑定固定页面布局。 |
| target_verify | 1 | 动作后落点校验：截图+归一化落点+指令→on_target/actual_element。覆盖底部 tab 误判——回归点：红叉实际落在『闹钟』tab，却被 target_verify 幻觉成『世界时钟 tab』，触发 OffTarget replan（见 logs/.../android/20260611_085000 T2） |
| loading | 4 | 分级加载感知：显式平台信号与高置信视觉端点直接裁决，稀疏非空帧和视觉/结构时序冲突才调用轻量 VLM。当前覆盖 Mastodon 中央 Logo splash（含 UIAutomator 已提前暴露后台内容的变体）、Android launcher 和已渲染 alarm picker，并输出准确率、VLM 路由率与平均兜底耗时。 |
| acquire | 5 | 共享 cells→records 采集：空集合、单条记录、跨窗口 multi-cell feed、同构列表，以及 Mastodon 真机多窗口回放。LLM 只选记录锚点和字段 source refs，Runtime 负责原文复制、对齐、去重与机械验收。 |
| action_feedback | 1 | 动作反馈链收敛：同一控件反复 off_target 必须改用 target_ref 而非无限视觉估点。固化 Mattermost 频道列表 '+' 连续 ~24 turn 点错 case（无语义 icon glyph → 视觉估错 → off_target 不纠）。三层锁：icon label 用 resource id 兜底(plus)、off_target 累计注 ref 约束、bind 把 ref 估点 snap 到权威中心；含错误图标不被误救、单次 miss 不触发两个对照。 |
| orchestrator | 7 | MobileWorld 静态 coding 编排：基础设置、闹钟、浏览器读取、购物车/文件计算和条件集合写入；不启动模拟器，使用确定性 AST 合同验收生成程序。 |

`agent-user-interaction` 任务不进入当前 orchestrator baseline；例如
`CalculateCartPricesByOwnerAskUserTask` 保留在 MobileWorld 官方集合中，但本版本不把 AskUser
能力混入 GUI 编排回归。
