# Evals

针对 iphone-use 各核心模块的单元测评。每个模块独立运行，不依赖真实手机，直接调用 LLM 并与预期输出对比。

## 运行

```bash
uv run python evals/<module>/test_<module>.py
```

## 模块一览

| 模块 | Cases | 说明 |
|------|------:|------|
| router | 51 | 意图路由：将用户消息分类为手机操作/问答/闲聊，提取目标 APP 和 goal |
| checker | 10 | 验收员：根据截图判断当前 milestone 是否完成（done/in_progress/loading） |
| planner | 9 | 规划器：根据当前屏幕和 milestone 生成下一步操作指令 |
| replan | 1 | 修复规划器：stuck 触发后生成新策略（local_replan/escalate） |
| reply | 11 | 回复生成：任务结束后生成面向用户的自然语言回复 |
| prefs | 10 | 用户偏好提取：从对话历史中识别并结构化用户偏好 |
| decomposer | 5 | 任务分解：将用户目标拆解为有依赖关系的 milestone 列表 |
| back_nav | 14 | 回退导航：从当前页面找到返回目标页面的路径 |
| cascade_matcher | 22 | 级联页面匹配：基于视觉指纹和相似度判断两个截图是否是同一页面 |
| popup_detect | 7 | 弹窗检测：识别截图中是否存在覆盖主界面的弹窗/浮层 |
| snap | 7+9 | 坐标吸附：YOLO/OCR 把 LLM 给的 tap 坐标吸到真实元素（7 截图 case + 9 几何回归） |
| repeat_detect | — | 重复指令检测（无固定 cases，程序化生成测试） |
| stuck_detect | — | 卡住检测（无固定 cases，程序化生成测试） |

## 最新测试结果（2026-05-26）

| 模块 | 通过/总数 |
|------|----------|
| router | 51/51 |
| checker | 10/10 |
| planner | 9/9 |
| reply | 11/11 |
| prefs | 16/16 |
| decomposer | 5/5 |
| replan | 0/1 ⚠️ |

## 用例分组说明

### router

| 分组 | 数量 | 说明 |
|------|-----:|------|
| 独立 | 5 | 用户直接给出完整指令，无历史 |
| 承接 | 7 | 依赖上文历史推断 APP / 操作 |
| 多轮 | 14 | 复杂多轮对话场景 |
| 偏好 | 5 | 有用户偏好上下文时的路由行为 |
| 模糊 | 5 | 信息不足，需反问 |
| 裸APP名 | 3 | 仅说 APP 名称无动词，需反问 |
| 跨APP | 3 | 跨多个 APP 的任务 |
| 闲聊/问答 | 3 | 不涉及手机操作，goal 为空 |
| 澄清合并 | 2 | 原始指令+补充说明合并后路由 |
| APP保护 | 2 | 用户明确指定 APP 时不被替换 |
| 跨任务 | 1 | 跨多个历史任务切换 APP |
| 信息查询 | 1 | APP 内数据查询（账单/余额）属于手机操作 |

### checker

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| 首页 | 已在目标页面 | status=done |
| 搜索建议页 | 已输入关键词但未提交 | status=in_progress, loading=false |
| 骨架屏 | 内容区域全部为占位块 | loading=true |
| 微信发送界面 | 发送完成前的中间状态 | status=in_progress |
| 店铺列表页 | 有实质内容但未达验收条件 | status=in_progress, loading=false |
| iMessage聊天列表 | 应用身份识别防混淆 | status=in_progress |
| 微信账单页 | 有加载指示器时 loading=true | loading=true |

### planner

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| 规格面板 | 目标属性 chips 被截断 | 指令含「滚动/上滑」，不含直接点击目标 |
| 搜索建议页 | 搜索框有旧关键词残留 | 指令含「输入」+目标词，不含「返回/清空」 |
| 搜索结果页 | 上次点击进了直播间，需改变策略 | 不重复点击失败路径中的商品 |
| 商品列表 | 目标商品在列表中可见 | 指令含「点击」+商品名 |
| 微信账单页 | 两个外观相似按钮功能不同 | 必须点月份按钮，不点「全部账单」 |
| picker | date picker 拖动方向/列/回归 | direction/drag_column 正确，拖动而非点击 |

### decomposer

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| 单APP | 单应用内多步操作 | milestone 粒度适当，验收条件唯一可观测 |
| 跨APP | 涉及多个 APP 的任务 | 每次 APP 切换单独建模为 milestone |
| 微信账单 | 含相对时间表达 | 换算为绝对日期写入 global_constraints |

### cascade_matcher

| 分组 | 说明 |
|------|------|
| fingerprint | 基于视觉指纹的页面同一性判断 |
| similarity | 基于像素/结构相似度的页面匹配 |

### snap

截图 case 跑真实 `ActionExecutor._snap`（YOLO+OCR 仲裁），几何回归直接驱动
`YoloCalibrator.nearest()`，不依赖 LLM。截图被 gitignore，缺图时 SKIP。

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| 底部 tab「我的」 | 2 字标签 + conf=0.28 超宽误检框 | method=ocr 且吸到「我的」文字 |
| 主屏 App 图标 | 点落在图标框内，名字在图标下方 | method=yolo；OCR 命中落在图标框内=自身标签则走 YOLO，不受 conf 抖动影响 |
| 首页 tab（宽合并框） | 点落进宽误检框但远离其中心 | method=ocr，move>45 不锁定 YOLO，OCR 救回 |
| 我的 tab（弱框干扰） | conf 0.40 弱框压在过高的 tap 估计上 | method=ocr，弱框不够格锁 OCR，吸到真标签 |
| 会员页返回箭头（宽 banner） | 全屏 banner 包含左上角点 | method=yolo，snap 上限拒绝 banner 中心，吸到真箭头 |
| 左上角返回箭头 | 无文字、LLM 纵向估偏 | method=yolo 且经 margin 层吸到箭头 |
| 几何回归 | 分层不变式 + 守卫 | 小图标不偷点、超宽误检被拒、snap 上限防瞬移、contains()/OCR-lock 阈值判定 |
