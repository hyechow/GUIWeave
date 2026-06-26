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
| decomposer | 2 | 任务分解（legacy milestone DAG：`MilestoneSupervisorPolicy._decompose`→milestone 列表，`--no-orchestrator` 路径）。① 20260611_113402 闹钟创建：设值类 picker 子目标必须 `repeat_until_satisfied`，最终验收=保存后闹钟列表出现 06:30 条目；② 20260625_195139 github-info：禁 `api.*/`JSON 直链取数（手机浏览器不渲染 JSON、纯视觉读不到字段）|
| orchestrator | 1 | DSL program 分解（`orchestrator.decomposer.decompose`→run/if/finish，**mobileworld `--orchestrator` 默认路径**，与 decomposer 是不同模块）。20260625_195139 github-info：program 的 run 禁 `api.*/`JSON 直链端点——由 `validate_program` 硬校验拒绝+feedback-retry（不只靠 prompt 软约束；软约束非确定，195139 那次 LLM 仍走了 api.github.com 致 contributors 读成 5/真 15）。修法见 `tests/test_orchestrator.py::test_validate_program_flags_api_direct_link` |
| action_policy | 11 | 手机动作策略：截图+指令→单个动作。覆盖滚轮时间 picker「改值用 scroll、绝不 tap 选中数字」，并收录 20260611_095209/104134/111632 的 picker 方向/列锚点/幅度回归：分钟列 52→30、21→30 反向纠偏、小时 05→06 小步调整、小时 09→06 必须 small 防震荡、小时 11→06 与分钟 17→30 远距离应放大幅度；含一个点击列表项仍 tap 的对照 case，防 picker 规则把一切都变成 scroll；另收录 20260626_220437 T17：Gmail 正文输入框可见但未聚焦时，`type` 必须带正文框坐标，不能无坐标假设当前输入框已聚焦。 |
| planner | 5 | 步骤规划器：截图+checker 结果→下一步指令与结构化 hints。收录 20260611_095209/111632/122343 的 Android 闹钟 picker 回归，要求 planner 输出 `direction`、`drag_column`、`drag_current_value`、`drag_target_value`，覆盖小时 09→06、分钟 52→30、分钟 22→30 反向纠偏、小时分钟已对但 PM→AM 仍需调时段列，以及时间已到 06:30 AM 时应保存而非继续零步滚动 |
| checker | 9 | 子目标验收器：截图+子目标→done/in_progress。覆盖滚轮 picker 的**读值幻觉**——回归点：屏幕显示『下午 06:00』(分钟=00)，checker 却幻觉成『已设为06:30』判 done、越级保存致跑飞（见 logs/.../android/20260610_220003 T8）；屏幕中间行是 06:28、30 仅在候选行却被误判为 06:30（见 logs/.../android/20260611_113402 T8）；06 仅在候选行却被当成小时已对齐（见 logs/.../android/20260611_115132 T5、20260611_120915 T7）；以及 PM/傍晚 6:30 不能验收 AM/上午 6:30（见 logs/.../android/20260611_122343 T12/T13）；含一个 10:59 明显非完成态的基线。另收录 20260626_220437 T21：发送邮件后从 Compose 跳回 Gmail Inbox，应按 dispatch 响应门判 done，不能重新 Compose 导致假失败；以及 20260626_220437 T7：仓库详情 JSON 里的 `contributors_url` 不能验收为 `/contributors` 顶层数组页面。修法同 iphone：设值类 picker 子目标走 `repeat_until_satisfied`→`_CHECK_SECTION_CONVERGE` 值传感器段（逐列枚举、只认中间高亮行、逐位比对、禁止被目标值带跑），发送/提交类动作则按上一动作历史 + 页面跳转响应收口。 |
| target_verify | 1 | 动作后落点校验：截图+归一化落点+指令→on_target/actual_element。覆盖底部 tab 误判——回归点：红叉实际落在『闹钟』tab，却被 target_verify 幻觉成『世界时钟 tab』，触发 OffTarget replan（见 logs/.../android/20260611_085000 T2） |
| structured_read | 1 | 视觉读取原语（reader LLM 从截图读 returns 字段）。20260625_202900 纠正：手机 GitHub 主页（Code tab）**不显示 stars 计数**——手机版精简，OCR+analyze 确认截图无 stars（可见仅 Issues 38 / PR 7），故读 stars 必空、读空**正确**；之前"reader 读不准 803/15.2k"结论错（截图压根没 803，32.8k 是 README 文件大小）。本 case 改测截图真实可见的 Issues 计数。stars 要看须切桌面版/stargazers 页 = **导航策略问题，非 reader 能力** |

候选待建模块：milestone supervisor（mobile-tuned prompts：picker 粗调+精调、误入界面用 back 退）、
replan（picker 失败时继续滚动逼近而非升级成 tap）、router（android intent）。
