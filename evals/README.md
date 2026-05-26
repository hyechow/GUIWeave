# Evals

模块/整体测评数据与脚本。

## 目录结构

```
evals/
├── router/      # 意图路由测评
├── prefs/       # 用户偏好提取测评
├── reply/       # 回复生成测评
├── checker/     # SingleCheck 验收员测评（status/loading 准确性）
├── planner/     # 步骤规划器测评（指令类型正确性）
├── replan/      # 修复规划器测评（stuck 后策略切换）
├── back_nav/    # 回退导航测评
├── cascade_matcher/  # 级联匹配器测评
├── popup_detect/      # 弹窗检测测评
├── decomposer/        # 任务分解测评
```

## router/cases.json 用例分组

| 分组 | 说明 |
|------|------|
| 独立 | 用户直接给出完整指令，无历史 |
| 承接 | 依赖上文历史推断 APP / 操作 |
| 跨任务 | 跨多个历史任务切换 APP |
| 模糊 | 信息不足，需反问 |
| 闲聊/问答 | 不涉及手机操作，goal 为空 |
| 多轮 | 复杂多轮对话场景 |
| 偏好 | 有用户偏好上下文时的路由行为 |
| 裸APP名 | 仅说 APP 名称无动词，需反问 |
| 澄清合并 | 原始指令+补充说明合并后路由 |
| APP保护 | 用户明确指定 APP 时不被替换 |

## prefs/cases.json 用例分组

| 分组 | 说明 |
|------|------|
| 基础 | 单次成功任务触发偏好提取 |
| 澄清合并 | 经过澄清流程后合并消息触发偏好提取 |

## checker/cases.json 用例分组

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| milestone已满足 | 进入页面时验收条件已成立 | status=done |
| 搜索建议页 | 输入了关键词但未提交搜索 | status=in_progress, loading=false |
| 骨架屏 | 内容区域全部为灰色占位块 | loading=true |
| 白屏 | 页面完全空白 | loading=true |
| 正常进行中 | 有实质内容但未达到验收条件 | status=in_progress, loading=false |
| 加载中部分可见 | 页面有可见加载指示器但已有部分内容 | loading=true |

## replan/cases.json 用例分组

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| scroll失败 | scroll 导致屏幕冻结，stuck 触发 replan | 指令含「点击」，不含「滚动/滑动」 |

## planner/cases.json 用例分组

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| 规格面板-选项截断 | bottomsheet 中目标属性 chips 未显示 | 指令含「滚动/上滑」，不含直接点击目标选项 |
| 商品列表-目标可见 | 目标商品在列表中可见 | 指令含「点击」+商品名 |

## reply/cases.json 用例分组

| 分组 | 说明 | 关键验证点 |
|------|------|-----------|
| pre_existing | 目标完成但无写入动作（内容已存在） | 禁止说"已帮你…"，必须说"已存在/已有" |
| normal-success | 有 type/press_enter 动作的正常完成 | 禁止说"未完成/已存在" |
| normal-failure | 任务未完成 | 禁止说"成功/已帮你完成" |
| non-action | 无需操作手机的闲聊/追问 | 自然回答，无操作痕迹 |
| 承接 | 多轮对话中的后续操作 | 正常成功回复 |

## 最近测试结果（2026-05-26）

| 模块 | 通过/总数 | 备注 |
|------|----------|------|
| router | 51/51 | 新增「信息查询-微信查账单」case，修复 APP 内数据查询被误判为非手机操作 |
| checker | 9/9 | 新增「加载中部分可见」case，收紧 loading 判定：有加载指示器即使有部分内容也判 loading=true |
| planner | 4/4 | 无新增 |
| replan | 9/10 | 新增「scroll失败」case，验证 stuck 后 replan 改用 tap 而非继续 scroll |

### 关键改动

- **router**: 提示词增加规则——信息来源是某个 APP 内的数据（账单、订单、余额）即属于手机操作，不因为「查询」「多少钱」等词误判为通用问答
- **checker**: loading 判定从「有部分内容就 false」改为「有加载指示器就 true，内容完整渲染且无指示器才 false」
- **replan/planner**: 增加 scroll 无效后必须改用 tap 的规则，模拟生产 SimStuck 检测 → tried_instructions 注入 → replan 路径
