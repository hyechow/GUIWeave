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
| decomposer | 1 | 任务分解：用户目标+截图→milestone 列表。收录 20260611_113402 闹钟创建回归：设值类 picker 子目标必须使用 `repeat_until_satisfied`，最终验收必须是保存后闹钟列表出现 06:30 条目，而不是新建页 picker 显示目标值 |
| action_policy | 10 | 手机动作策略：截图+指令→单个动作。覆盖滚轮时间 picker「改值用 scroll、绝不 tap 选中数字」，并收录 20260611_095209/104134/111632 的 picker 方向/列锚点/幅度回归：分钟列 52→30、21→30 反向纠偏、小时 05→06 小步调整、小时 09→06 必须 small 防震荡、小时 11→06 与分钟 17→30 远距离应放大幅度；含一个点击列表项仍 tap 的对照 case，防 picker 规则把一切都变成 scroll |
| planner | 5 | 步骤规划器：截图+checker 结果→下一步指令与结构化 hints。收录 20260611_095209/111632/122343 的 Android 闹钟 picker 回归，要求 planner 输出 `direction`、`drag_column`、`drag_current_value`、`drag_target_value`，覆盖小时 09→06、分钟 52→30、分钟 22→30 反向纠偏、小时分钟已对但 PM→AM 仍需调时段列，以及时间已到 06:30 AM 时应保存而非继续零步滚动 |
| checker | 7 | 子目标验收器：截图+子目标→done/in_progress。覆盖滚轮 picker 的**读值幻觉**——回归点：屏幕显示『下午 06:00』(分钟=00)，checker 却幻觉成『已设为06:30』判 done、越级保存致跑飞（见 logs/.../android/20260610_220003 T8）；屏幕中间行是 06:28、30 仅在候选行却被误判为 06:30（见 logs/.../android/20260611_113402 T8）；06 仅在候选行却被当成小时已对齐（见 logs/.../android/20260611_115132 T5、20260611_120915 T7）；以及 PM/傍晚 6:30 不能验收 AM/上午 6:30（见 logs/.../android/20260611_122343 T12/T13）；含一个 10:59 明显非完成态的基线。修法同 iphone：设值类 picker 子目标走 `repeat_until_satisfied`→`_CHECK_SECTION_CONVERGE` 值传感器段（逐列枚举、只认中间高亮行、逐位比对、禁止被目标值带跑），故 cases 的 milestone 标 completion_strategy=repeat_until_satisfied。 |
| target_verify | 1 | 动作后落点校验：截图+归一化落点+指令→on_target/actual_element。覆盖底部 tab 误判——回归点：红叉实际落在『闹钟』tab，却被 target_verify 幻觉成『世界时钟 tab』，触发 OffTarget replan（见 logs/.../android/20260611_085000 T2） |

候选待建模块：milestone supervisor（mobile-tuned prompts：picker 粗调+精调、误入界面用 back 退）、
replan（picker 失败时继续滚动逼近而非升级成 tap）、router（android intent）。
