# Checker 按 kind 拆分 + 动作校验 重构计划

## 背景与目标

当前 `_SingleCheckResult` 是**一张 schema 喂所有 milestone kind**（navigation/filter/action/collection），
prompt 也每轮把所有 kind 段落都发。问题：

- **慢**：checker 是最大延时块（占 LLM 总时长 ~42.8%，单次 ~2.4s，输出均值 242 字符）。
  延时 ≈ `1.07s 固定(图像 prefill+TTFT+网络)` + `0.0053s/字符`。
- **易幻觉**：泛泛判断"看起来完成了吗" → 看到底部 tab 就误判 done（见 issue_checker_hallucination）。
- **缺动作校验**：没有"我刚才那一下点到没到位"的机制。点错（如点搜索框误中转账）会连续多轮空转，
  因为屏幕变了所以 SimStuck 放行、checker 只问 milestone done 不问动作落点。

四阶段重构，每阶段独立可回归。延时模型告诉我们：**输出变短能逼近 ~1.07s 地板，
只有非 LLM 路径（导航指纹）能越过地板**。提速主要靠：输出变短 + 非 LLM 快路径 + off_target 跳 checker。

---

## 阶段 1：checker 按 kind 拆分（最大价值，最大 blast radius）

**动机**：每个 kind 的 done 判据本质不同；统一 schema 逼 LLM 填无关字段（浪费+噪声+幻觉）。

实际只有 4 个 kind（decompose prompt 已禁止生成 verification）：

| kind | done 判据 | 需要的输出 |
|---|---|---|
| navigation | 当前在目标页 | status + page_identity + 页面签名证据 |
| filter | 当前筛选值 == 目标值 | status + **结构化 current_value / target_value / matches** |
| action | 预期效果发生 | status + 一句 effect_reason |
| collection | 内容已读取 | status + read_instruction + captured |

共享内核：`CheckCore { status(done/in_progress), loading }`。

**改动**
- `supervisor/milestone/schemas.py`：拆出 `CheckCore` + 4 个子 schema（NavigationCheck / FilterCheck / ActionCheck / CollectionCheck）。
- `supervisor/milestone/prompts.py`：把 CHECK_PROMPT 拆成 per-kind 段落，按 kind 只发对应段。
- `supervisor/milestone/helpers.py` `run_checker`：加 kind dispatch（选 schema + prompt）。
- `supervisor/milestone/policy.py`：`_single_check` / `_plan_single` 适配各 kind 的 check 对象；
  **picker 方向逻辑改用 FilterCheck 的结构化 current/target**，不再解析 `check.reason`/`missing_evidence` 文本
  （现在在 planner prompt ~238 行 + `_fix_picker_direction`）。
- 顺带删死字段 `frozen`（全仓零读取，零风险）。

**保留**：done 防幻觉护栏（navigation/filter 的 done 必须有可见证据），不能因拆分砍掉。

**风险**：filter 结构化字段牵动 picker 方向（见 issue_date_picker_ltr）；planner 输入契约变化。

**回归**：`evals/checker` 按 kind 拆成 navigation/filter/collection/action 分组；`evals/planner` 的 picker 用例确认方向不回退。

**完成判据**：各 kind checker 输出 ≤ ~120 字符；checker 单次延时 ~2.4s → ~1.5-1.7s；checker eval 不低于现状；picker 方向用例全过。

---

## 阶段 2：navigation 指纹快路（⏸ 推迟 — 缺页面参考库）

> **状态（调查后）**：当前 knowledge/ 是每页 **markdown 文本**，**无任何页面级视觉 embedding**；
> `auto_discover_knowledge` 给的是 app 级文本知识，不是"当前屏 vs 目标页"的视觉匹配；
> CascadeMatcher 视觉匹配只用于离线 recon。**没有目标页参考指纹可比** → 指纹快路无法直接落地。
> 要做需先建"目标页 embedding 采集+存储+绑定 navigation 目标"的基础设施（独立工程）。
> 故推迟，先做阶段 3。下文为原设计，待基础设施就绪后再启。



**动机**：navigation 是最高频 kind；页面指纹匹配可不发 LLM → ~0.2s 而非 ~1.5s。

**前提（诚实）**：需要目标页的**参考指纹**。只有当目标页已在知识库 / CascadeMatcher 有参考时才能走快路；
否则回退到阶段 1 的 navigation LLM checker。所以这是**机会性加速**，覆盖子集，不是全部 navigation。

**改动**
- `recon/cascade_matcher.py` / 页面指纹：navigation done 判定先试视觉匹配目标页参考。
- `policy.py` navigation 分支：指纹高置信命中 → done（跳过 LLM）；否则 LLM navigation checker。
- 依赖知识库自动发现（project_runner_knowledge_auto）提供 per-page 参考。

**回归**：在 `evals/cascade_matcher` 或新增 nav-match 用例，确认"在目标页/不在目标页"判定正确，且不误判 done。

**完成判据**：有参考的 navigation 轮 done 判定无 LLM 调用、延时 ~0.2s；无参考时正确回退。

---

## 阶段 3：post-verify 动作落点校验（新能力，独立）

**机制**：把吸附后的落点画成标记（空心环+十字）渲染在**动作前帧**上，让轻量 LLM 判断
"标记是否落在指令意图的目标元素上"。已在 `tmp_scripts/exp_target_verify.py` 实验验证：
turn 12（点搜索框误中转账）正确判 off_target；对照（我的 tab）on_target。

**关键约束**：
- **不阻塞执行**：tap 照常发出（允许点错），校验与 `_settle_after_action` **并发**跑 → 几乎零静默增量。
- 校验用前帧+标记，不依赖结果帧，故可在 tap 一发出就起跑。
- **只出 on_target + actual_element**，不出修正坐标（跨帧无意义）。仅 tap/click 校验，scroll/drag 不验。

**改动**
- 新建 `policy_expr/target_verify.py`：`render_marker(png, nx, ny)` + `verify_target(marked_png, instruction) -> TargetVerify{on_target, actual_element}`。
- `schemas.py`：`TargetVerify` model + `PolicyTurn.target_verify: Optional[TargetVerify] = None`。
- `executor.py`：`execute()` 暴露吸附后落点（返回或存 `last_snap`），仅 tap/click 有。
- `runner.py`（agent-loop）：execute 后若 tap/click，`ThreadPoolExecutor.submit(verify_target)` 在 settle 前提交，settle 后取结果写进 `turn.target_verify`。

**回归**：新增 `evals/target_verify`（复用实验用例：turn 12 off / 控制 on），断言 on_target 判定。

**完成判据**：turn 12 类被判 off_target；正常 tap 判 on_target；happy path 延时不显著增加。

---

## 阶段 4：off_target 接 replan（依赖阶段 3）

**机制**：off_target → 下一轮**跳过 checker 直接 replan**，由 LLM 在新帧上重新决策。
坐标不跨帧携带（无意义），只把"上一步想点 X、误中 Y、未成功"作为语义诊断喂给 replanner。

**改动**
- `policy.py` `_run_single_turn` 开头（白屏检查后、checker 之前）：
  若 `history[-1].target_verify` 且 `not on_target` → 打印 `[OffTarget]` → 构造
  `_SingleCheckResult(status="stuck", reason="误中{actual_element}…")` → `_handle_stuck(...)`，跳过 checker。
- `_handle_stuck` 默认参数即可（off_target 重试计入升级次数，避免无限循环）。

**它补的盲区**：SimStuck 抓"屏幕没变"；off_target 抓"屏幕变了但点错了"（turn 12 类）。互补。

**回归**：构造一个带 `target_verify(on_target=False)` 的 PolicyTurn history，断言 `_run_single_turn` 不调 checker、走 `_handle_stuck`。

**完成判据**：off_target 轮不调 checker、进 replan；重复 off_target 触发升级而非死循环。

---

## 依赖与顺序

```
阶段1 (按 kind 拆) ──→ 阶段2 (nav 指纹，建在 nav checker 之上)
阶段3 (post-verify) ──→ 阶段4 (off_target 接 replan)
```
两条链独立，可并行推进；组内有序。建议顺序：1 → 2 → 3 → 4（用户指定）。

## 不在本计划内（避免混入）

- 减 checker→planner 输入契约（独立大改，风险在 planner 质量）。
- 图像格式改 JPG（不碰 prefill 地板、有损伤小元素识别风险，不是提速杠杆）。
- 图像分辨率上调（为准确性而非速度，反方向权衡，单独评估）。

## 提速账（预期）

- 单轮决策链 `checker 2.4 + planner 1.2 + action_policy 1.0 ≈ 4.6s`
  → checker 降到 ~1.6 后 **≈ 3.8s**；navigation 指纹命中轮更低；off_target 轮省一整次 checker。
