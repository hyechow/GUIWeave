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
| action_policy | 4 | 手机动作策略：截图+指令→单个动作。覆盖滚轮时间 picker「改值用 scroll、绝不 tap 选中数字」——回归点：tap picker 零效果会被 runner 判为操作无效而中止（见 logs/.../android/20260610_205402）；含一个点击列表项仍 tap 的对照 case，防 picker 规则把一切都变成 scroll |
| checker | 2 | 子目标验收器：截图+子目标→done/in_progress。覆盖滚轮 picker 的**读值幻觉**——回归点：屏幕显示『下午 06:00』(分钟=00)，checker 却幻觉成『已设为06:30』判 done、越级保存致跑飞（见 logs/.../android/20260610_220003 T8）；含一个 10:59 明显非完成态的基线。⚠️ 幻觉 case 当前 **FAIL**（已复现、待修：强制 checker 逐位读出选中值再比对，或把设值类子目标标 converge 走值传感器段）|

候选待建模块：milestone supervisor（mobile-tuned prompts：picker 粗调+精调、误入界面用 back 退）、
replan（picker 失败时继续滚动逼近而非升级成 tap）、router（android intent）。
