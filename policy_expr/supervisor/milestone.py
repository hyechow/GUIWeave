"""Milestone supervisor v2: single-step and loop milestones as separate state machines."""

import base64
import io
import json
import re
from typing import Literal, Optional

from PIL import Image
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.schemas import CollectionScope, Milestone, Observation, PolicyTurn, SupervisorStep

load_dotenv()

MAX_RETRIES = 3
STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95
STUCK_SCREEN_FROZEN = 0.99
MAX_SCROLL_PER_MILESTONE = 3
STUCK_REPEAT_WINDOW = 3
BLANK_SCREEN_RATIO = 0.84  # Near-white pixel ratio above this = blank/loading screen
STUCK_REPEAT_WORD_OVERLAP = 0.85


# ── Schemas ───────────────────────────────────────────────────────────


class _SingleCheckResult(BaseModel):
    """Checker output for single-step milestones (navigation/filter/action/verification/read_once).

    LLM checker should only return done or in_progress.
    stuck is reserved for programmatic checks (screen similarity, instruction repetition).
    """
    status: Literal["done", "in_progress", "stuck"] = Field(
        description="判断状态：done（验收通过）或 in_progress（未完成）。禁止填 'loading'——页面加载状态用独立的 loading 布尔字段表示"
    )
    reason: str = Field(description="判断理由")
    stuck_reason: str = Field(default="", description="卡住原因（仅程序化 stuck 时填写）")
    issues: list[str] = Field(default_factory=list)
    visible_evidence: list[str] = Field(default_factory=list, description="截图中支持 done 的可见证据")
    missing_evidence: list[str] = Field(default_factory=list, description="缺失的验收证据")
    page_identity: str = Field(default="", description="当前页面的身份识别（如：订单列表、发票管理、个人中心）")
    summary: str = Field(description="当前屏幕状态一句话描述")
    read_instruction: Optional[str] = Field(
        default=None,
        description="kind=collection(read_once) 或 kind=verification 时填写：当前屏幕需要提取的内容说明；其他类型留空",
    )
    frozen: bool = Field(default=False, description="屏幕是否冻结（相似度≥99%，即使 reader 返回新内容也应停止）")
    loading: bool = Field(default=False, description="页面正在加载（骨架屏/启动屏/转场动画），应等待下一帧而非立即规划动作")


class _LoopFrameResult(BaseModel):
    """Per-frame assessment for scroll_until_boundary milestones."""
    boundary_reached: bool = Field(default=False, description="当前可见内容是否已到达列表物理边界（无更多条目）")
    should_stop: bool = Field(default=False, description="是否满足停止条件，应结束滚动采集")
    stop_reason: str = Field(default="", description="停止原因（should_stop=true 时填写）")
    read_instruction: str = Field(default="", description="当前屏幕需要提取的内容说明；无相关内容时留空")
    collection_scope: Optional[CollectionScope] = Field(default=None)
    summary: str = Field(description="当前屏幕内容一句话描述")


class _PlanResult(BaseModel):
    instruction: str = Field(description="下一步精确操作指令")
    summary: str = Field(description="规划依据一句话摘要")
    direction: Optional[Literal["up", "down", "left", "right", "increase", "decrease"]] = Field(
        default=None,
        description=(
            "scroll 时填手指移动方向（up/down/left/right）；"
            "picker drag 时填值的变化方向（increase=值变大，decrease=值变小）；"
            "tap/type/home/stop 留空"
        ),
    )
    drag_column: Optional[str] = Field(
        default=None,
        description="picker drag 时的目标列，如 'year'/'month'/'day'；非 picker drag 留空",
    )
    drag_magnitude: Optional[Literal["small", "medium", "large"]] = Field(
        default=None,
        description="picker drag 时的拖动幅度：small=差1格，medium=差2-3格，large=差4格以上；非 picker drag 留空",
    )


class _ReplanResult(BaseModel):
    diagnosis: str = Field(description="失败根本原因（一句话）")
    strategy: Literal["local_replan", "escalate_human", "force_complete"]
    instruction: str = Field(default="")
    escalation_message: str = Field(default="")
    can_degrade_to_collection: bool = Field(default=False)


class _StopConditionPatch(BaseModel):
    scroll_stop_condition: str = Field(
        description="一句话描述何时应停止滚动。从依赖链的约束维度推导：有日期范围用日期边界，"
                    "有关键词用关键词消失条件，没有任何约束的全量采集用'滚动至列表物理底部时停止'"
    )
    observable_boundary: bool = Field(
        description="该停止条件是否在屏幕上可直接观察。日期标记、'没有更多了'提示为 true；"
                    "关键词相关性、内容充分性判断为 false"
    )


class _DecomposeResponse(BaseModel):
    goal: str
    global_constraints: list[str] = Field(default_factory=list)
    milestones: list[Milestone]
    task_type: Literal["action", "analysis"] = Field(
        description="action=执行具体操作；analysis=查看/比较/总结信息；有疑问时选 analysis"
    )


# ── Prompts ───────────────────────────────────────────────────────────

DECOMPOSE_PROMPT = """\
你是 iPhone 自动化任务的规划 Supervisor。将用户任务分解为子目标（milestone）。
你会收到当前屏幕截图，请根据截图判断设备当前状态。

可用操作：tap（点击）、type（输入文字，通过剪贴板粘贴支持中文，自动清空旧内容）、press_enter（按回车提交）、scroll（滚动）、home（返回主屏幕）
⚠️ 设备通过 iPhone Mirroring 控制，使用 Mac 键盘输入，iOS 虚拟键盘不会在屏幕上弹出。因此验收条件中禁止使用「键盘弹出」「键盘可见」等无法观察的条件，应改为「输入框获得焦点（显示光标）」「输入框显示指定文字」等可观测条件。
- goal：任务一句话描述
- global_constraints：全局约束列表
- milestones：子目标列表，每个包含 id/name/description/depends_on/success_condition/kind/completion_strategy/scroll_stop_condition/failure_hints
- task_type：
  - action：用户要求执行具体操作（发消息、打开应用、修改设置）
  - analysis：用户要求查看/比较/总结信息（统计数据、总结列表、查询结果）；有疑问时选 analysis

原则：
## 子目标粒度
每个子目标对应一个**截图可确认的稳定页面状态**（如：应用主页、搜索结果页、详情页、设置页）。
子目标之间是页面级别的跨越，子目标内部的具体操作（点击、输入、滑动、切换 tab、搜索导航）不需要拆成子目标。
**关键：多步页面导航应合并为一个子目标**。例如"搜索并进入某人的聊天"是一个子目标（最终到达聊天页），不应该拆成"进入搜索页→输入关键词→点击搜索结果→进入聊天"四个子目标。

示例：
- ❌ 「打开XX应用」→「点击底部Tab」（太细，单步动作不应成为子目标）
- ❌ 「进入搜索页」→「输入搜索词」→「点击搜索结果」（太细，搜索导航应合并为一个子目标）
- ❌ 「找到联系人并发送消息」（太粗，两个不同的目标页面状态）
- ✅ 「进入XX应用主页」→「导航到某人的详情页」→「发送消息」（合适，每个子目标到达一个稳定页面）

## 验收条件
每个 success_condition 必须指向**唯一可截图确认的状态**，只用一个核心判定，不要用「且」「及」连接多个条件。
好：「看到XX页面标题」「消息气泡出现在聊天记录中」
差：「看到底部导航栏及聊天列表」（"及"连接两个条件）
禁止使用模糊修饰（如「任意页面均可」「如XX页面」「或其他页面」等）。
**action 类子目标的验收条件必须描述操作的最终可见结果**，不能只验证中间步骤。例如发送消息的验收条件必须是「看到包含"XX"的消息气泡出现在聊天记录中」，不能是「输入框获得焦点」「输入框显示指定文字」。
⚠️ 发送/分享类 action 的验收条件必须是发送完成的证据，例如「消息气泡出现在聊天记录中」「看到发送成功 Toast 提示」。禁止使用「发送按钮可见」「联系人已选中」「分享界面显示」等发送前的中间状态作为验收条件——这些只是发送前的准备状态，并非操作完成。

## 相对时间换算
任务中出现「本周/上周/本月/上月/今天/昨天/最近X天」等相对时间表达时，必须根据当前日期换算为明确的日期区间（格式：YYYY-MM-DD ~ YYYY-MM-DD），并以「时间范围：XXXX-XX-XX ~ XXXX-XX-XX」的形式加入 global_constraints。注意：中国以周一为一周的第一天。不要把相对时间留在 goal 或 success_condition 里，必须用具体日期。

## 其他规则
1. 如果当前不在主屏幕，第一个子目标应为「回到主屏幕」，验收条件为「看到主屏幕（桌面图标界面）」
2. 如果当前已在主屏幕或已在目标应用内，不需要「回到主屏幕」步骤
3. depends_on 填依赖的前置子目标 id，无依赖留空
4. kind 必须表达子目标语义：
   - navigation：打开应用、进入页面、切换 tab
   - filter：设置范围、搜索词、筛选条件、排序条件
   - collection：读取并收集页面内容（记录列表、消息流、搜索结果）
   - action：执行一次具体操作
   - verification：确认结果是否满足目标
5. completion_strategy 必须表达完成方式：
   - visible_once：看到指定页面/状态即可完成
   - read_once：读取当前屏幕一次即可完成
   - scroll_until_boundary：需要反复滚动，直到列表到底或无更多内容
   - repeat_until_satisfied：重复操作直到条件满足
   - human_escalation：需要人工处理
6. 信息获取类任务的内容收集子目标必须使用 kind=collection；来自可滚动列表、记录流或消息流的内容必须使用 completion_strategy=scroll_until_boundary
   - scroll_until_boundary 的子目标必须填写 scroll_stop_condition（一句话说明何时停止滚动）：
     * 有时间范围：「当可见记录日期早于 {目标开始日期} 时停止」
     * 有关键词条件：「当可见内容不再包含 {关键词} 时停止」
     * 全量采集：「滚动至列表物理底部时停止」
7. failure_hints 列出该子目标可能失败的原因
8. 禁止生成 kind=verification 的子目标。action 任务的验证内化到 action 子目标的验收条件中；analysis 任务的数据完整性由采集阶段保证。
9. 跨 APP 任务：当任务涉及多个 APP（如从拼多多分享商品到微信、从支付宝截图发微信），每次 APP 切换都必须单独建模：
   - 触发切换的动作（点击分享按钮、选择目标 APP）归入当前 APP 的 action 子目标，验收条件为「看到目标 APP 的界面」
   - 切换后在新 APP 内的操作另立子目标（kind=action 或 kind=navigation），不要将两个 APP 的操作合并为一个子目标
   - 示例：拼多多分享到微信 → ①「在拼多多找到商品并点击分享到微信，看到微信界面」→ ②「在微信中选择联系人并发送，看到发送成功提示」
"""

SINGLE_CHECKER_PROMPT = """\
你是 iPhone 自动化任务的验收员。根据当前屏幕截图和子目标验收条件，判断执行进展。

请按以下步骤分析（按顺序推理）：

**第一步：页面识别**
先识别当前页面是什么。当前应该在「{app_name}」应用内。观察截图中的标题、tab标签、页面布局，确定页面身份（如：订单列表、发票管理、个人中心、账单页面、聊天列表等）。
⚠️ 识别页面时必须考虑当前应用上下文：当前应该在「{app_name}」内，不要将当前应用的页面误判为其他应用。
将结果填入 page_identity 字段。

**第二步：验收判断**
⚠️ 只看验收条件（success_condition）的字面要求，忽略子目标名称和描述。子目标名称不能替代验收条件作为判定依据，必须验收条件中明确描述的内容全部可见才能判 done。
基于第一步的页面识别结果，判断验收条件是否满足。
## 当前子目标
- 名称：{milestone_name}
- 描述：{milestone_desc}
- 验收条件：{success_condition}
- 子目标类型：{milestone_kind}
- 完成策略：{completion_strategy}
- 任务类型：{task_type}
- 全局约束：{constraints}

## 历史操作记录
{history_text}

## 筛选类子目标（kind=filter）
- 截图必须显示精确的筛选条件或等价范围，才能判 done
- 更宽的范围不能当作筛选完成；即使可见项都在目标范围内，筛选摘要显示更宽范围也不能 done

## 搜索类子目标（kind=filter，含搜索操作）
判 done 必须同时满足：
1. 当前页面是结果页，不是信息流、建议页、历史页或加载页
2. 搜索框或标题显示完整目标查询/条件
3. 页面显示与查询对应的结果列表或详情

⚠️ 搜索建议页 vs 搜索结果页 区分：
- 若搜索框右侧仍显示「搜索」按钮（尚未提交），或搜索框处于激活输入状态，则当前是自动补全/建议页——即使下方列表出现了目标商家名称，也必须判 in_progress
- 搜索建议项通常左侧有放大镜图标或时钟图标，且没有评分、标签等商家详情元素
- 只有用户已提交搜索（按下搜索按钮或回车），进入独立的搜索结果页，才能判 done

## 内容读取（kind=collection read_once 或 kind=verification）
- 如果当前屏幕有与用户目标相关的可提取内容，填写 read_instruction
- navigation/filter/action 阶段 read_instruction 必须留空

## 发送/分享类子目标（验收条件含「发送」「分享」「消息」）
⚠️ 严格区分「可以发送」与「已发送」：
- 发送按钮可见、联系人已选中、分享界面显示 → 仍是准备状态，必须判 in_progress
- 只有看到以下证据才能判 done：消息气泡出现在聊天记录中、发送成功 Toast 提示、"已发送"文字标识
- 不能将「发送按钮就绪」「界面已就绪」「视为发送状态」等推断作为 done 依据

## 通用规则
- done：验收条件中描述的每个具体内容都必须在截图上可直接观察到
- in_progress：验收条件尚未完全满足，包括页面不匹配、还在导航中、操作未完成等所有非 done 的情况
- 验收条件要求某页面标题：必须看到顶部标题与验收条件精确匹配
- 验收条件要求某底部 tab：必须看到该 tab 高亮/选中
- 验收条件要求某段文字内容（如消息文本）：截图中对应元素的文字必须与验收条件中的文字精确匹配，包含多余前缀、后缀或拼写错误都不能判 done
- 只看可观测事实，不要凭感觉判断
- ⚠️ 页面底部「已选：...」或「当前选择：...」摘要文字只反映当前值，不代表对应的选项 chip/按钮可点击——判断某选项可见，必须在截图中看到实际的选项 chip 元素，不能从摘要文字推断
- done 时：visible_evidence 必须列出截图中直接支持验收条件的文字；missing_evidence 必须为空
- 存在任何 missing_evidence 不能返回 done

## loading 字段
loading 是独立布尔字段，与 status 无关。status 只能填 done 或 in_progress，不要填 loading。
以下情况必须设置 loading=true（页面内容尚未稳定，不应执行操作）：
- 应用启动屏（仅显示 Logo + 品牌名）
- 骨架屏：内容区域全部被灰色占位块替代，没有真实文字或图片可读
- 全屏加载动画或旋转菊花，内容区域为空
- 页面完全空白（仅有状态栏/导航栏，正文一片空白）
- 页面有可见的加载指示器（转圈动画、进度条、"加载中"文字），即使已有部分内容渲染
以下情况设置 loading=false（内容已稳定，可供规划器决策）：
- 页面内容已完整渲染，无任何加载指示器
- 有错误提示、空状态提示等可交互元素
"""

LOOP_FRAME_PROMPT = """\
你是内容收集的屏幕状态评估员。当前任务正在滚动收集页面列表内容。
根据当前截图，评估以下内容：

## 当前子目标
- 名称：{milestone_name}
- 描述：{milestone_desc}
- 停止条件：{scroll_stop_condition}
- 全局约束：{constraints}

## 历史操作记录
{history_text}

## 评估要点

### 1. 列表边界（boundary_reached）
boundary_reached=true 必须有明确可见证据，例如：
- 看到"没有更多内容"、"已全部加载"、"到底了"等文字
- 列表末尾出现明显空白且无加载指示器
- 看到与前一屏重叠的最后一条记录，且下方无新内容
不确定时填 false。

### 2. 停止判断（should_stop）
对照上方「停止条件」，判断当前屏幕是否已触发该条件：
- should_stop=true：当前可见内容已满足停止条件，继续滚动只会偏离目标
- should_stop=false：目标内容仍在当前滚动方向，应继续采集
- 如果停止条件是「滚动至列表物理底部」，should_stop 跟随 boundary_reached
- 只有确定触发时才返回 true；不确定时返回 false
- should_stop=true 时必须填写 stop_reason 说明触发依据

### 3. 当前屏幕内容（read_instruction）
如果当前屏幕有与用户目标相关的内容，填写 read_instruction，说明需要提取哪些字段（如时间、金额、名称、状态）。
无相关内容时留空。

### 4. 采集范围（collection_scope，可选）
如果可见内容有明确的范围标志（时间范围、分组标题、筛选摘要），填写 collection_scope 作为参考信息。
"""

PLAN_PROMPT = """\
你是 iPhone 自动化任务的步骤规划器。根据当前截图、子目标和验收结果，给出下一步操作指令。

## 当前子目标
- 名称：{milestone_name}
- 描述：{milestone_desc}
- 验收条件：{success_condition}
- 子目标类型：{milestone_kind}
- 全局约束：{constraints}

## Checker 结果
- status：{check_status}
- reason：{check_reason}
- issues：{issues}
- missing_evidence：{missing_evidence}
- 当前屏幕摘要：{check_summary}

## 历史操作记录
{history_text}

规划规则：
- ⚠️ 每条指令只包含一个动作。禁止组合多个动作（如「输入文字并按回车」）。输入和提交必须拆成两条指令
- 描述要操作的具体 UI 元素，如「点击底部导航栏左起第二个Tab」
- 不要给出目标级指令，如「进入通讯录页面」「完成搜索」
- 如果当前子目标要求「回到主屏幕」，下一步必须指令「按 Home 键返回主屏幕」
- ⚠️ Home 键只在子目标明确要求「回到主屏幕」时使用；当前已在目标应用内，禁止用 Home 键退出应用——不管当前页面是否符合预期，都应在应用内使用返回按钮、Tab 切换或应用内搜索找到正确路径
- 如果当前已在目标应用内但不在正确页面，给出应用内导航指令，不要重新打开已打开的应用
- 如果当前在 iOS 主屏幕且目标应用图标不可见：优先点击底部「搜索」胶囊按钮；看不到时才向下滑动打开系统搜索；搜索框出现后输入应用名称
- 如果当前是 iOS 系统搜索页，直接输入或点击搜索结果中的目标应用，不要返回主屏
- 如果当前在应用的子页面需要回到上级，优先使用可见返回控件
- 如果当前在应用内搜索页：搜索框已聚焦则直接输入；未聚焦则先点击搜索框
- ⚠️ 需要提交/发送/确认输入时（如发送消息、确认搜索），必须指令「按回车键提交」，禁止指令「点击发送按钮」
- ⚠️ 输入框无论有无旧内容，直接生成输入文字指令即可——系统会自动清空后输入，无需先清空
- 普通列表滚动指令描述要查看什么内容（如「滚动查看更早的消息」），不要指定手指方向
- 滚轮选择器/日期选择器/时间选择器/城市选择器等多列 picker：
  * picker 物理规律（实测）：手指向上拖（to_y < y）→ 列表内容向上 → 数值增大；手指向下拖（to_y > y）→ 列表内容向下 → 数值减小
  * 因此：目标值 < 当前值（如5月→1月）必须手指向下；目标值 > 当前值（如1月→5月）必须手指向上
  * ⚠️ 如果目标项已经在 picker 滚轮的可见行内（上下邻近行均可见），必须直接点击目标项，禁止再拖动——拖动 1 格精度不足，容易超调
  * 拖动幅度要一次到位：目标值与当前值相差多少格，就选对应幅度（4格以上选 large），不要每次只拖1格再重试
- ⚠️ 「未生效」「屏幕无变化」对 picker 的处理：先检查拖动方向是否正确；若连续 2 次方向正确但仍超调，改用点击可见目标行；普通列表/网格无响应时才改用 tap
- ⚠️ 生成输入文字指令时，必须使用子目标描述或验收条件中明确指定的原始文字，禁止凭空编造或改写输入内容
- ⚠️ 输入文字动作已包含自动点击输入框的步骤，不需要先单独生成「点击/激活输入框」指令，看到输入框时直接生成输入指令即可
- 商品规格选择面板（bottomsheet）中，若目标属性（如糖度/甜度）的分类标题可见但选项 chips 未出现或被截断，应先在面板内向上滑动使该属性的选项行完整显示，再点击目标选项

## 结构化方向提示（direction / drag_column）
每次输出时必须决定以下字段：
- direction：
  * 下一步是 scroll → 填手指移动方向（down/up/left/right）
  * 下一步是 picker drag → 填「值的变化方向」（不是手指方向！）：
    - 目标值比当前值小（如5月→1月，2026年→2024年，20日→5日）：direction=decrease
    - 目标值比当前值大（如1月→5月，2024年→2026年，5日→20日）：direction=increase
  * 其他动作（tap/type/home/stop）→ 留空
- drag_column：
  * 下一步是 picker drag → 填目标列（year=年份列，month=月份列，day=日期列）
  * 其他动作 → 留空
- drag_magnitude（仅 picker drag 时填写）：根据当前值与目标值的差距选择：
  * small：相差 1 格（如当前2026年目标2025年）
  * medium：相差 2-3 格
  * large：相差 4 格以上
  * 其他动作 → 留空
"""

LOOP_SCROLL_PROMPT = """\
你是滚动方向决策器。当前任务需要滚动收集页面内容，请根据截图决定第一次滚动的方向和位置。

## 当前子目标
- 名称：{milestone_name}
- 描述：{milestone_desc}
- 全局约束：{constraints}

## 当前屏幕状态
{frame_summary}

规则：
- 输出一个滚动指令，描述要查看什么内容（如「滚动查看更多记录」「滚动查看更早的消息」）
- 不要指定手指滑动方向
- 如果当前屏幕已显示列表内容，滚动以获取更多同类内容
"""

REPLAN_PROMPT = """\
你是 iPhone 自动化任务的修复规划器。某个子目标执行失败，请诊断原因并制定修复策略。

## 失败的子目标
- 名称：{milestone_name}
- 描述：{milestone_desc}
- 验收条件：{success_condition}
- 失败原因：{stuck_reason}
- 具体问题：{issues}
- 已重试次数：{retry_count}
- 全局约束：{constraints}
- 预期失败提示：{failure_hints}

## 已完成的子目标（不要退回这些状态）
{completed_milestones}

## 历史操作记录
{history_text}

## 分析要求
1. 观察截图，理解当前所有可见 UI 元素
2. 检查历史操作是否存在 A→B→A→B 交替循环，如果存在必须跳出
3. 禁止退回到已完成子目标的状态（如已完成「回到主屏幕」就不要再按 Home 键）
4. 分析之前失败的根本原因
5. 找到一条不同的路径——如果同一 UI 组件已尝试 2+ 次均失败，必须跳出该组件：
   - 截图中是否有尚未尝试的标签（Tab）、按钮、面板或入口？优先尝试
   - 当前弹窗/面板是否可以关闭，回到上级页面寻找替代路径？
   - 不要继续在同一个已失效的组件上重试不同操作

## 决策规则
- 验收条件已满足（截图中可见目标状态）→ force_complete（不再生成操作指令，直接标记完成）
- 工具限制/数据问题 → local_replan
- 如果筛选无法精确设置，但后续 collection 子目标可通过逐条过滤补偿，can_degrade_to_collection=true
- 以下指令已尝试过且失败，禁止再次使用：
{tried_instructions}
- instruction 只包含一个原子操作，禁止包含「并」「然后」「再」等连接词
- 如果子目标要求「回到主屏幕」，必须指令「按 Home 键返回主屏幕」
- ⚠️ 打开应用后发现不是目标应用或进入错误页面时，应先在应用内导航到该应用的主界面（点击返回按钮、关闭弹窗等），确认当前所在的应用身份后，再决定是继续在应用内操作还是按 Home 键回主屏。禁止未确认应用身份就直接按 Home 键。
- 滚动指令描述要查看什么内容，不要指定手指方向
- ⚠️ 如果失败原因包含「屏幕无变化」「屏幕冻结」「未生效」，说明当前 UI 不支持滚动（日历选择器、滚轮、静态网格等）。此时必须改用 tap 点击目标位置，绝对禁止生成包含「滚动」「滑动」「向上」「向下」的指令
"""

STOP_CONDITION_PATCH_PROMPT = """\
你是 iPhone 自动化任务的规划助手。你需要从依赖链推导滚动采集子目标的停止条件。

推导规则：
1. 看前置子目标的验收条件，找出约束维度（时间范围？金额阈值？关键词？）
2. 如果前置验收条件限定了时间范围，停止条件就用对应的日期边界
3. 如果前置验收条件限定了金额/数量，停止条件就用数值边界
4. 如果前置验收条件限定了关键词/类别，停止条件就用关键词消失条件
5. 如果没有任何筛选约束，是全量采集，使用"滚动至列表物理底部时停止"

可观察性判断：
- 日期边界（列表按日期排序，看到某个日期就停）→ observable_boundary=true
- 列表物理结束标识（"没有更多了"、分组标题变化）→ observable_boundary=true
- 关键词/相关性消失（需要判断"是否还有相关内容"）→ observable_boundary=false
- 瀑布流/无限加载（永远不会有"到底"信号）→ observable_boundary=false

要求：
- 输出一句话描述何时停止滚动
- 必须从约束维度推导，不能默认使用"物理底部"
- 如果已给出当前停止条件且与约束维度一致，保持不变
"""

# ── History formatter ─────────────────────────────────────────────────


def _format_history(history: list[PolicyTurn]) -> str:
    if not history:
        return "（无历史记录，这是第一轮）"
    recent = history[-8:]
    lines = []
    for idx, turn in enumerate(recent):
        sv = turn.supervisor
        next_sv = recent[idx + 1].supervisor if idx + 1 < len(recent) else None
        result = next_sv.summary if next_sv else "（结果尚未记录）"
        # Detect if this action led to stuck: next turn for same milestone indicates stuck/replan
        failed = (
            turn.executed
            and next_sv
            and next_sv.milestone_id == sv.milestone_id
            and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
        )
        prefix = "❌ " if failed else ""
        if turn.action_decision and turn.executed:
            action = turn.action_decision.action
            outcome = f"导致错误: {result}" if failed else f"结果: {result}"
            lines.append(
                f"{turn.index}. {prefix}指令=「{sv.instruction}」"
                f" → [{action.action_type}] {action.description}"
                f" → {outcome}"
            )
        elif turn.action_decision and not turn.executed:
            action = turn.action_decision.action
            lines.append(
                f"{turn.index}. {prefix}指令=「{sv.instruction}」 → [未执行] [{action.action_type}] {action.description}"
            )
        else:
            lines.append(f"{turn.index}. [跳过动作] {sv.summary} → 结果: {result}")
    return "\n".join(lines)


# ── Public invoke functions (shared by production and evals) ─────────


def _make_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


def _build_msgs(system_prompt: str, png_bytes: bytes) -> list:
    from datetime import datetime
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    b64 = base64.b64encode(resize_to_logical_png(png_bytes)).decode()
    return [
        SystemMessage(content=f"{system_prompt}\n\n当前日期：{today}"),
        HumanMessage(content=[
            {"type": "text", "text": "请根据当前屏幕做出决策。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]


def run_checker(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    app_name: str = "未知应用",
    task_type: str = "action",
    constraints: Optional[list[str]] = None,
    extra: str = "",
) -> _SingleCheckResult:
    """Run the single-step milestone checker. Used by both production and evals."""
    if constraints is None:
        constraints = []
    prompt = SINGLE_CHECKER_PROMPT.format(
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        success_condition=milestone.success_condition,
        milestone_kind=milestone.kind,
        completion_strategy=milestone.completion_strategy,
        task_type=task_type,
        constraints=json.dumps(constraints, ensure_ascii=False),
        history_text=_format_history(history),
        app_name=app_name,
    )
    if extra:
        prompt += f"\n\n## 输出修正要求\n{extra}"
    result = invoke_structured(_make_llm(), _build_msgs(prompt, observation.png_bytes), _SingleCheckResult)

    if result.status == "done" and (not result.visible_evidence or result.missing_evidence):
        print("  [SingleCheck] done 缺少证据，重试...")
        result = run_checker(
            milestone, observation, history,
            app_name=app_name, task_type=task_type, constraints=constraints,
            extra="你刚才返回 done 但 visible_evidence 为空或 missing_evidence 非空。请重新核对截图，确有证据才能 done，否则返回 in_progress 或 stuck。",
        )
    if result.status == "done" and (not result.visible_evidence or result.missing_evidence):
        return _SingleCheckResult(
            status="stuck",
            reason="checker 返回 done 但缺少可见验收证据",
            stuck_reason="done 缺少可见证据",
            summary=result.summary,
        )
    return result


def run_planner(
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    constraints: Optional[list[str]] = None,
    extra: str = "",
    app_knowledge: Optional[str] = None,
) -> _PlanResult:
    """Run the step planner. Used by both production and evals."""
    if constraints is None:
        constraints = []
    # When replanning (retry_count > 0), inject all previously tried instructions
    # so the planner avoids paths that led into dead ends before stuck was detected.
    if milestone.retry_count > 0 and not extra:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        if tried:
            tried_lines = "\n".join(f"  - 「{i}」" for i in tried)
            extra = (
                f"⚠️ 该子目标已重试 {milestone.retry_count} 次。以下操作在本子目标中已全部尝试过"
                f"（含导致失败或死路的路径），请务必选择完全不同的路径：\n{tried_lines}"
            )
    prompt = PLAN_PROMPT.format(
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        success_condition=milestone.success_condition,
        milestone_kind=milestone.kind,
        constraints=json.dumps(constraints, ensure_ascii=False),
        check_status=check.status,
        check_reason=check.reason,
        issues=json.dumps(check.issues, ensure_ascii=False),
        missing_evidence=json.dumps(check.missing_evidence, ensure_ascii=False),
        check_summary=check.summary,
        history_text=_format_history(history),
    )
    if extra:
        prompt += f"\n\n## 输出修正要求\n{extra}"
    msgs = _build_msgs(prompt, observation.png_bytes)
    if app_knowledge:
        msgs[1].content = [{"type": "text", "text": f"## 应用导航知识\n{app_knowledge}\n\n"}] + msgs[1].content
    return invoke_structured(_make_llm(), msgs, _PlanResult)


# ── Main class ────────────────────────────────────────────────────────


class MilestoneSupervisorPolicy:
    """Two-machine milestone supervisor: single-step and loop run independently."""

    name = "milestone"

    def __init__(self) -> None:
        self._global_constraints: list[str] = []
        self._milestones: dict[str, Milestone] = {}
        self._order: list[str] = []
        self._current_id: Optional[str] = None
        self._recent_screenshots: list[bytes] = []
        self._scroll_counts: dict[str, int] = {}
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._app_name: str = ""
        self._last_page_identity: dict[str, str] = {}
        self._last_check_summary: dict[str, str] = {}

    def set_app_knowledge(self, text: str, app_name: str = "") -> None:
        self._app_knowledge = text
        if app_name:
            self._app_name = app_name

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        if not self._order:
            self._decompose(goal, observation)

        if self._current_id is None:
            return self._terminal_step()

        milestone = self._milestones[self._current_id]
        if _is_loop(milestone):
            return self._run_loop_turn(milestone, observation, history)
        return self._run_single_turn(milestone, observation, history)

    # ── Single-step machine ───────────────────────────────────────────

    def _run_single_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        # Blank screen: page is still loading (transition / app render). Skip all LLM calls
        # and do not add this frame to screenshot history (avoids corrupting AB-loop detection).
        # The runner's noop_count handles termination after too many consecutive blank waits.
        if _is_blank_screen(observation.png_bytes):
            print("  [BlankScreen] 检测到白屏，页面加载中，等待下一帧...")
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                summary="页面加载中（白屏），等待...",
                **_ctx(milestone, None),
            )

        # After a 'type' action the screen changes only minimally (input text).
        # Reset screenshot history so SimStuck doesn't fire as a false positive.
        if history and history[-1].action_decision:
            if history[-1].action_decision.action.action_type == "type":
                self._recent_screenshots.clear()

        # Snapshot previous page identity before running checker.
        prev_page_id = self._last_page_identity.get(milestone.id, "")

        check = self._single_check(milestone, observation, history)
        print(f"  [SingleCheck] {check.status}: {check.reason}")

        if check.loading:
            print("  [Loading] 检测到加载状态，等待下一帧...")
            return SupervisorStep(
                should_act=False, instruction=None, stop=False,
                goal_completed=False, summary="页面加载中，等待...",
                **_ctx(milestone, None),
            )

        current_page_id = check.page_identity or ""
        self._last_page_identity[milestone.id] = current_page_id

        if check.status == "done":
            return self._advance(milestone, observation, history)

        # Checker says in_progress — check for stuck signals before planning
        sim_stuck = self._check_screen_similarity(observation)

        # Suppress SimStuck when checker's summary changed from the previous turn —
        # picker wheels produce tiny pixel diffs per step (99%+ similarity) but the
        # checker reliably tracks the actual selected value.  If the value changed,
        # the action worked; reset the screenshot window and let planning continue.
        prev_check_summary = self._last_check_summary.get(milestone.id, "")
        # Only suppress frozen-type SimStuck (picker wheels: 99%+ on all frames).
        # AB-loop SimStuck (frozen=False: 2-back high, adjacent low) must NOT be
        # suppressed even if the checker summary changed — the summary alternates
        # between two states, which is the symptom of the AB loop, not progress.
        if sim_stuck is not None and sim_stuck.frozen and prev_check_summary and prev_check_summary != check.summary:
            print(f"  [SimStuck] 已抑制：picker 进展（frozen+摘要变化）")
            sim_stuck = None
            self._recent_screenshots.clear()
        self._last_check_summary[milestone.id] = check.summary

        rep_stuck = self._check_instruction_repetition(history, milestone.id) if not sim_stuck else None
        if sim_stuck or rep_stuck:
            stuck = sim_stuck or rep_stuck
            assert stuck is not None
            print(f"  [Stuck] {stuck.status}: {stuck.reason}")
            page_changed = sim_stuck is None
            return self._handle_stuck(
                milestone, stuck, check.read_instruction, observation, history,
                page_changed=page_changed,
                prev_page_id=prev_page_id,
                current_page_id=current_page_id,
            )
        return self._plan_single(milestone, check, observation, history)

    def _plan_single(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        plan = self._invoke_planner(milestone, check, observation, history)
        if self._is_sequence(plan.instruction):
            print("  [Planner] 多步序列，重试...")
            plan = self._invoke_planner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回当前屏幕上马上要做的一个操作。",
            )
        # Post-check: reject instructions that repeat previously tried ones
        if self._is_repeated_instruction(plan.instruction, milestone.id, history):
            print("  [Planner] 指令重复已失败操作，重试...")
            plan = self._invoke_planner(
                milestone, check, observation, history,
                extra=(
                    "你刚才的指令与之前失败的操作相同。"
                    "请仔细查看截图，找一个不同的 UI 元素或操作路径。"
                ),
            )
            # If retry still repeats, escalate to replanner
            if self._is_repeated_instruction(plan.instruction, milestone.id, history):
                print("  [Planner] 重试仍重复，升级为 stuck 处理")
                stuck_check = _SingleCheckResult(
                    status="stuck",
                    reason=f"planner 无法找到与之前不同的操作路径，已尝试指令均导致错误",
                    stuck_reason="planner 陷入重复，无法生成新操作",
                    summary=check.summary,
                )
                return self._handle_stuck(milestone, stuck_check, check.read_instruction, observation, history)
        print(f"  [Planner] {plan.instruction}")
        if plan.direction or plan.drag_column or plan.drag_magnitude:
            print(f"  [Planner] hints: direction={plan.direction} column={plan.drag_column} magnitude={plan.drag_magnitude}")
        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(plan.instruction),
            instruction=plan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            direction=plan.direction,
            drag_column=plan.drag_column,
            drag_magnitude=plan.drag_magnitude,
            **_ctx(milestone, check.read_instruction),
        )

    # ── Loop machine ──────────────────────────────────────────────────

    def _run_loop_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        # Track scroll count
        self._scroll_counts[milestone.id] = self._scroll_counts.get(milestone.id, 0) + 1
        scroll_count = self._scroll_counts[milestone.id]

        # Termination: scroll budget (safety net, always applies)
        budget = MAX_SCROLL_PER_MILESTONE  # strict for non-observable boundaries
        if milestone.observable_boundary:
            budget = 10  # generous for observable boundaries (stop_condition should trigger first)
        if scroll_count > budget:
            print(f"  [Loop] 滚动预算耗尽（{scroll_count}/{budget}，observable={milestone.observable_boundary}）→ 结束收集")
            return self._advance(milestone, observation, history)

        # Termination: sim_stuck (screen stopped changing)
        sim_stuck = self._check_screen_similarity(observation)
        last_read_added = bool(
            history
            and history[-1].supervisor.milestone_id == milestone.id
            and history[-1].read_added_content
        )
        if sim_stuck:
            if sim_stuck.frozen:
                print("  [Loop] 屏幕冻结（≥99%），即使 reader 返回新内容也结束收集")
                return self._advance(milestone, observation, history)
            if not last_read_added:
                print("  [Loop] 截图连续无变化且无新增内容 → 判为边界，结束收集")
                return self._advance(milestone, observation, history)
            print("  [Loop] 截图相似但上一轮读到了新内容，继续收集")

        # Per-frame assessment
        frame = self._loop_check(milestone, observation, history)
        print(f"  [LoopFrame] boundary={frame.boundary_reached}, should_stop={frame.should_stop}")
        if frame.should_stop:
            print(f"  [Loop] 停止条件触发：{frame.stop_reason}")
        # Action tasks: loop is for finding a target, no need to read content
        if self.task_type == "action":
            read_inst = None
        else:
            read_inst = frame.read_instruction or _default_read_instruction(milestone)

        # Termination: stop condition triggered
        if frame.should_stop:
            if _has_collected(history, milestone.id):
                print("  [Loop] 已触发停止条件且有采集内容 → 结束收集")
                final_read = _ctx(milestone, read_inst, frame.collection_scope)
                if milestone.scroll_stop_condition:
                    final_read["collection_summary"] = (
                        f"停止条件「{milestone.scroll_stop_condition}」已触发"
                        f"（{frame.stop_reason}）"
                    )
                return self._advance(
                    milestone, observation, history,
                    final_read=final_read,
                )
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"停止条件已触发但尚未采集到目标内容：{frame.stop_reason}",
                stuck_reason="停止条件触发且没有可用采集结果",
                summary=frame.summary,
            )
            return self._handle_stuck(milestone, stuck, read_inst, observation, history)

        # Termination: boundary confirmed after at least one scroll
        if frame.boundary_reached and _last_scroll_was_for(history, milestone.id):
            print("  [Loop] 确认列表边界 → 结束收集")
            return self._advance(milestone, observation, history)

        # Continue: reuse last scroll or plan first scroll
        milestone.status = "running"
        loop_summary_prefix = "继续滚动查找目标" if self.task_type == "action" else "继续滚动收集内容"
        if _last_scroll_was_for(history, milestone.id):
            return SupervisorStep(
                should_act=True,
                instruction="继续滚动",
                preformed_action=history[-1].action_decision,
                stop=False,
                goal_completed=False,
                summary=f"{loop_summary_prefix}。{frame.summary}",
                read_instruction=read_inst,
                allow_read=bool(read_inst),
                milestone_id=milestone.id,
                milestone_kind=milestone.kind,
                completion_strategy=milestone.completion_strategy,
                collection_scope=frame.collection_scope,
            )

        plan = self._invoke_loop_scroll(milestone, frame, observation)
        print(f"  [LoopScroll] {plan.instruction}")
        return SupervisorStep(
            should_act=True,
            instruction=plan.instruction,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            read_instruction=read_inst,
            allow_read=bool(read_inst),
            milestone_id=milestone.id,
            milestone_kind=milestone.kind,
            completion_strategy=milestone.completion_strategy,
            collection_scope=frame.collection_scope,
        )

    # ── Shared: advance, stuck, terminal ─────────────────────────────

    def _advance(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        final_read: Optional[dict] = None,
    ) -> SupervisorStep:
        """Mark milestone done, route immediately to next milestone's machine."""
        done_name = milestone.name
        milestone.status = "done"
        self._current_id = self._next_milestone()
        self._recent_screenshots.clear()
        print(f"  子目标「{done_name}」已完成")

        # Detect pre_existing: milestone found done without any executed actions for it.
        # This means the target state existed before the agent did anything for this milestone.
        pre_existing = not any(
            t.executed for t in history
            if t.supervisor.milestone_id == milestone.id
        )
        if pre_existing:
            print(f"  [PreExisting] 子目标「{done_name}」未执行任何动作即判完成，目标状态在会话前已存在")

        if self._current_id is None:
            return SupervisorStep(
                should_act=False, stop=True, stop_reason="所有子目标已完成",
                goal_completed=True, pre_existing=pre_existing,
                summary=f"子目标「{done_name}」已完成，任务全部完成。",
                **(final_read or {}),
            )

        next_ms = self._milestones[self._current_id]
        print(f"  开始执行「{next_ms.name}」")

        # If there's content to read from the completed milestone, return an
        # intermediate step so the runner processes it before advancing.
        if final_read:
            return SupervisorStep(
                should_act=False, stop=False, goal_completed=False,
                summary=f"子目标「{done_name}」已完成，下一子目标「{next_ms.name}」待执行。",
                **final_read,
            )

        if _is_loop(next_ms):
            return self._run_loop_turn(next_ms, observation, history)
        return self._run_single_turn(next_ms, observation, history)

    def _handle_stuck(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        read_inst: Optional[str],
        observation: Observation,
        history: list[PolicyTurn],
        page_changed: bool = True,
        prev_page_id: str = "",
        current_page_id: str = "",
    ) -> SupervisorStep:
        self._recent_screenshots.clear()
        # Decide whether to count this as a retry:
        # - Not executed (action policy refused): skip
        # - Truly new page (page_identity changed): navigation progress, not a retry — skip
        # - Same page with pixel changes (e.g. different search term in same search box): count
        skip_retry = False
        if history and history[-1].supervisor and history[-1].supervisor.milestone_id == milestone.id:
            if history[-1].supervisor.instruction and not history[-1].executed:
                print(f"  [Replan] 上一轮指令未执行，不计入重试次数")
                skip_retry = True
            elif page_changed:
                truly_new_page = bool(prev_page_id and current_page_id and prev_page_id != current_page_id)
                if truly_new_page:
                    print(f"  [Replan] 页面已跳转（{prev_page_id} → {current_page_id}），不计入重试次数")
                    skip_retry = True
                    # Failure constraints are page-scoped: an action that failed on the old UI
                    # may work fine on the new UI. Clear action-specific failure constraints
                    # (pattern: "指令「...」导致错误：...禁止重复此指令") so the planner
                    # starts fresh on the new page without stale prohibitions.
                    before = len(self._global_constraints)
                    self._global_constraints = [
                        c for c in self._global_constraints
                        if not (c.startswith("指令「") and "禁止重复此指令" in c)
                    ]
                    cleared = before - len(self._global_constraints)
                    if cleared:
                        print(f"  [Replan] 清除 {cleared} 条旧页面操作约束")
                else:
                    print(f"  [Replan] 屏幕变化但页面未变（{current_page_id or '未知'}），计入重试次数")
        if not skip_retry:
            milestone.retry_count += 1

        # Propagate failure to global_constraints so subsequent planner calls avoid it
        self._record_failure_constraint(milestone, check, history)

        if milestone.retry_count >= MAX_RETRIES:
            fallback = self._try_filter_fallback(milestone, can_degrade=True, read_inst=read_inst)
            if fallback:
                return fallback
            return self._fail(milestone, check, read_inst)

        print(f"  [Replan] 第 {milestone.retry_count} 次重试...")
        replan = self._invoke_replanner(milestone, check, observation, history)
        print(f"  [Replan] 诊断={replan.diagnosis}, 策略={replan.strategy}")

        if replan.strategy == "force_complete":
            print(f"  [Replan] replanner 判定验收条件已满足，强制完成")
            return self._advance(milestone, observation, history)

        if replan.strategy == "escalate_human":
            fallback = self._try_filter_fallback(
                milestone, can_degrade=replan.can_degrade_to_collection, read_inst=read_inst,
            )
            if fallback:
                return fallback
            milestone.status = "failed"
            self._current_id = self._next_milestone()
            return SupervisorStep(
                should_act=False,
                stop=self._current_id is None,
                stop_reason=replan.escalation_message or "升级人工介入",
                goal_completed=False,
                summary=replan.diagnosis,
                **_ctx(milestone, read_inst),
            )

        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(replan.instruction),
            instruction=replan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=f"子目标「{milestone.name}」卡住，第 {milestone.retry_count} 次重试。{replan.diagnosis}",
            **_ctx(milestone, read_inst),
        )

    def _fail(self, milestone: Milestone, check: _SingleCheckResult, read_inst: Optional[str]) -> SupervisorStep:
        milestone.status = "failed"
        self._current_id = self._next_milestone()
        print(f"  子目标「{milestone.name}」失败")
        if self._current_id is None:
            return SupervisorStep(
                should_act=False, stop=True,
                stop_reason=f"子目标「{milestone.name}」重试 {MAX_RETRIES} 次后失败",
                goal_completed=False, summary=check.reason,
                **_ctx(milestone, read_inst),
            )
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False,
            summary=f"子目标「{milestone.name}」失败，跳过继续下一个。",
            **_ctx(self._milestones[self._current_id], read_inst),
        )

    def _terminal_step(self) -> SupervisorStep:
        failed = [m for m in self._milestones.values() if m.status == "failed"]
        pending = [m for m in self._milestones.values() if m.status == "pending"]
        if failed or pending:
            return SupervisorStep(
                should_act=False, stop=True, goal_completed=False,
                stop_reason=f"无可执行子目标；失败：{'、'.join(m.name for m in failed) or '无'}；未完成：{'、'.join(m.name for m in pending) or '无'}",
                summary="任务未完成，存在失败或依赖未满足的子目标。",
            )
        return SupervisorStep(
            should_act=False, stop=True, stop_reason="所有子目标已完成",
            goal_completed=True, summary="任务完成",
        )

    def _try_filter_fallback(
        self,
        milestone: Milestone,
        can_degrade: bool,
        read_inst: Optional[str],
    ) -> Optional[SupervisorStep]:
        if milestone.kind != "filter" or not can_degrade:
            return None
        dependent = next(
            (self._milestones[mid] for mid in self._order
             if self._milestones[mid].status == "pending"
             and milestone.id in self._milestones[mid].depends_on
             and self._milestones[mid].kind == "collection"),
            None,
        )
        if dependent is None:
            return None
        # When degrading filter → collection, update stop_condition to reflect
        # the filter's original intent (e.g. date range) so the loop knows when
        # content has left the target scope.
        if _is_loop(dependent):
            filter_intent = milestone.success_condition
            dependent.scroll_stop_condition = (
                f"当可见内容不再满足筛选条件「{filter_intent}」时停止滚动"
            )
            dependent.observable_boundary = False
        milestone.status = "done"
        self._current_id = dependent.id
        self._recent_screenshots.clear()
        msg = (
            f"子目标「{milestone.name}」无法精确筛选，已降级为在「{dependent.name}」阶段收集并过滤。"
        )
        if msg not in self._global_constraints:
            self._global_constraints.append(msg)
        print(f"  [Fallback] {msg}")
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False, summary=msg,
            **_ctx(dependent, read_inst),
        )

    # ── LLM invocations ───────────────────────────────────────────────

    def _record_failure_constraint(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        history: list[PolicyTurn],
    ) -> None:
        """Write a semantic failure constraint to global_constraints."""
        reason = check.stuck_reason or check.reason
        # When planner escalates due to repetition, skip — the original
        # stuck event already recorded the right constraint.
        if "planner 陷入重复" in reason:
            return
        last_action = next(
            (t for t in reversed(history)
             if t.supervisor and t.supervisor.milestone_id == milestone.id
             and t.supervisor.instruction and t.executed),
            None,
        )
        if not last_action:
            return
        instruction = last_action.supervisor.instruction
        constraint = f"指令「{instruction}」导致错误：{reason}，禁止重复此指令"
        if constraint not in self._global_constraints:
            self._global_constraints.append(constraint)
            print(f"  [Constraint] {constraint}")

    def _single_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _SingleCheckResult:
        app_name = self._app_name or "未知应用"
        if not self._app_name:
            for t in reversed(history):
                if t.supervisor and t.supervisor.app_name:
                    app_name = t.supervisor.app_name
                    break
        return run_checker(
            milestone, observation, history,
            app_name=app_name,
            task_type=self.task_type,
            constraints=self._global_constraints,
            extra=extra,
        )

    def _loop_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> _LoopFrameResult:
        prompt = LOOP_FRAME_PROMPT.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            scroll_stop_condition=milestone.scroll_stop_condition or "滚动至列表物理底部时停止",
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            history_text=_format_history(history),
        )
        return invoke_structured(self._llm(), self._msgs(prompt, observation), _LoopFrameResult)

    def _invoke_planner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _PlanResult:
        return run_planner(
            milestone, check, observation, history,
            constraints=self._global_constraints,
            extra=extra,
            app_knowledge=self._app_knowledge,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = LOOP_SCROLL_PROMPT.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            frame_summary=frame.summary,
        )
        return invoke_structured(self._llm(), self._msgs(prompt, observation), _PlanResult)

    def _invoke_replanner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _ReplanResult:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        tried_text = "\n".join(f"  - 「{i}」" for i in tried) if tried else "  （无）"
        # Add completed milestone context to prevent regression
        done_lines = [
            f"  - [{m.id}] {m.name}（已完成，不要退回到该状态）"
            for m in self._milestones.values()
            if m.status == "done" and m.id != milestone.id
        ]
        done_context = "\n".join(done_lines) if done_lines else "  （无）"
        prompt = REPLAN_PROMPT.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            success_condition=milestone.success_condition,
            stuck_reason=check.stuck_reason or check.reason,
            issues=json.dumps(check.issues, ensure_ascii=False),
            retry_count=milestone.retry_count,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            failure_hints=json.dumps(milestone.failure_hints, ensure_ascii=False),
            completed_milestones=done_context,
            history_text=_format_history(history),
            tried_instructions=tried_text,
        )
        if extra:
            prompt += f"\n\n## 输出修正要求\n{extra}"
        msgs = self._msgs(prompt, observation)
        if self._app_knowledge:
            msgs[1].content = [{"type": "text", "text": f"## 应用导航知识\n{self._app_knowledge}\n\n"}] + msgs[1].content
        result = invoke_structured(self._llm(), msgs, _ReplanResult)
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    # ── Stuck detection ───────────────────────────────────────────────

    def _check_screen_similarity(self, observation: Observation) -> Optional[_SingleCheckResult]:
        self._recent_screenshots.append(observation.png_bytes)
        if len(self._recent_screenshots) > STUCK_SCREEN_WINDOW:
            self._recent_screenshots.pop(0)
        if len(self._recent_screenshots) < STUCK_SCREEN_WINDOW:
            return None

        current = self._recent_screenshots[-1]
        sims = [_png_sim(current, p) for p in self._recent_screenshots[:-1]]
        max_sim = max(sims)
        if all(s >= STUCK_SCREEN_SIMILARITY for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            frozen = max_sim >= STUCK_SCREEN_FROZEN
            if frozen:
                print(f"  [SimStuck] {sim_str} → 屏幕冻结（≥{STUCK_SCREEN_FROZEN:.0%}）")
            else:
                print(f"  [SimStuck] {sim_str} → 截图连续无变化")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_SCREEN_WINDOW} 帧截图相似度 [{sim_str}]，屏幕无实质变化",
                stuck_reason="连续帧高度相似，上一步操作未生效",
                issues=["屏幕像素变化低于阈值"],
                summary="屏幕连续无变化",
                frozen=frozen,
            )
        sim_2back = _png_sim(self._recent_screenshots[-1], self._recent_screenshots[-3])
        sim_adj = _png_sim(self._recent_screenshots[-1], self._recent_screenshots[-2])
        if sim_2back >= STUCK_SCREEN_SIMILARITY and sim_adj < STUCK_SCREEN_SIMILARITY:
            print(f"  [SimStuck] 2back={sim_2back:.2%}, adj={sim_adj:.2%} → AB 循环")
            return _SingleCheckResult(
                status="stuck",
                reason=f"截图在两种状态间交替（2帧前 {sim_2back:.2%}，相邻帧 {sim_adj:.2%}）",
                stuck_reason="屏幕在两种状态间振荡，操作陷入 AB 交替循环",
                issues=["截图在两个视觉状态间交替出现"],
                summary="屏幕在两种状态间振荡",
            )
        return None

    def _check_instruction_repetition(
        self,
        history: list[PolicyTurn],
        milestone_id: str,
    ) -> Optional[_SingleCheckResult]:
        recent_insts = [
            t.supervisor.instruction
            for t in history[-STUCK_REPEAT_WINDOW:]
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone_id
        ]
        if len(recent_insts) < STUCK_REPEAT_WINDOW:
            return None
        base_words = set(recent_insts[-1].split())
        sims = [
            len(base_words & set(inst.split())) / max(len(base_words), len(set(inst.split())), 1)
            for inst in recent_insts[:-1]
        ]
        if all(s >= STUCK_REPEAT_WORD_OVERLAP for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            print(f"  [RepStuck] {sim_str} → 指令连续重复")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步指令词语重叠 [{sim_str}]，操作策略未变化",
                stuck_reason="连续相似指令，重复操作未生效",
                issues=["supervisor 指令持续重复"],
                summary="操作陷入重复循环",
            )
        return None

    # ── Decompose & routing ───────────────────────────────────────────

    _MAX_DECOMPOSE_RETRIES = 2

    def _decompose(self, goal: str, observation: Observation) -> None:
        cfg = resolve_llm_config("supervisor.decompose")
        if not cfg.model:
            cfg = resolve_llm_config("supervisor")
        print(f"Supervisor: {cfg.provider} / {cfg.model}")
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

        # Decompose → validate → retry with feedback if needed
        issues: list[str] = []
        for attempt in range(self._MAX_DECOMPOSE_RETRIES + 1):
            self._do_decompose(llm, goal, observation, issues)
            issues = self._validate_decomposition(goal)
            if not issues:
                break
            if attempt < self._MAX_DECOMPOSE_RETRIES:
                print(f"  [Guard] 分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{self._MAX_DECOMPOSE_RETRIES})...")
                for i in issues:
                    print(f"  [Guard]   {i}")

        # Final structural fixes for anything the retry couldn't resolve
        self._patch_decomposition(llm, goal)

        # Re-evaluate _current_id after patching may have removed milestones
        if self._current_id not in self._milestones:
            self._current_id = self._next_milestone()

        print(f"任务分解为 {len(self._milestones)} 个子目标：")
        for mid in self._order:
            m = self._milestones[mid]
            deps = f" (依赖: {m.depends_on})" if m.depends_on else ""
            machine = "loop" if _is_loop(m) else "single"
            print(f"  [{m.id}][{machine}] {m.name}{deps}")
            print(f"       验收：{m.success_condition}")
            if m.scroll_stop_condition:
                print(f"       停止条件：{m.scroll_stop_condition}")

    def _do_decompose(
        self, llm: ChatOpenAI, goal: str, observation: Observation,
        feedback: list[str],
    ) -> None:
        msgs = self._msgs(DECOMPOSE_PROMPT, observation)
        user_parts: list[dict] = [{"type": "text", "text": f"用户任务：{goal}"}]
        if self._app_knowledge:
            user_parts.append({"type": "text", "text": f"\n## 应用导航知识\n{self._app_knowledge}"})
        if feedback:
            fb = "\n".join(f"  - {i}" for i in feedback)
            user_parts.append({"type": "text", "text": f"\n上一轮分解存在以下问题，请修正：\n{fb}"})
        msgs[1].content = user_parts + msgs[1].content
        resp = invoke_structured(llm, msgs, _DecomposeResponse)

        self._global_constraints = resp.global_constraints
        self.task_type = resp.task_type
        self._milestones = {m.id: m for m in resp.milestones}
        self._order = [m.id for m in resp.milestones]
        self._current_id = self._next_milestone()

    def _validate_decomposition(self, goal: str) -> list[str]:
        """Check all invariants WITHOUT modifying state. Returns list of issues."""
        issues = []
        all_ids = set(self._milestones.keys())

        # 1. depends_on references must exist
        for m in self._milestones.values():
            for dep in m.depends_on:
                if dep not in all_ids:
                    issues.append(f"子目标「{m.name}」的 depends_on 包含不存在的 ID: {dep}")

        # 2. DAG must not have cycles
        visited: set[str] = set()
        in_stack: set[str] = set()
        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False
        for mid in list(self._order):
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                issues.append(f"子目标之间存在循环依赖（从 {mid} 开始）")

        # 3. success_condition must not be empty
        for m in self._milestones.values():
            if not m.success_condition.strip():
                issues.append(f"子目标「{m.name}」的验收条件为空")

        # 4. kind=collection must pair with read_once or scroll_until_boundary
        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                issues.append(f"子目标「{m.name}」kind=collection 但 completion_strategy={m.completion_strategy}，应为 read_once 或 scroll_until_boundary")

        # 5. scroll_until_boundary must have scroll_stop_condition
        for m in self._milestones.values():
            if m.completion_strategy == "scroll_until_boundary" and not m.scroll_stop_condition:
                issues.append(f"子目标「{m.name}」使用 scroll_until_boundary 但缺少 scroll_stop_condition")

        # 6. task_type heuristic
        analysis_keywords = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
        if self.task_type == "action" and any(kw in goal for kw in analysis_keywords):
            issues.append(f"task_type=action 但目标含查询关键词（{', '.join(kw for kw in analysis_keywords if kw in goal)}），应为 analysis")

        return issues

    def _patch_decomposition(self, llm: ChatOpenAI, goal: str) -> None:
        """Apply structural fixes for issues that survive retry. Last resort."""
        fixes = []

        # 0. Remove verification milestones (verification is never valid —
        # action tasks bake verification into action's success_condition,
        # analysis tasks rely on collection completeness)
        verification_ids = [
            mid for mid, m in self._milestones.items()
            if m.kind == "verification"
        ]
        for vid in verification_ids:
            removed = self._milestones.pop(vid)
            self._order.remove(vid)
            for m in self._milestones.values():
                if vid in m.depends_on:
                    m.depends_on.remove(vid)
                    m.depends_on.extend(removed.depends_on)
            fixes.append(f"子目标「{removed.name}」（verification）已移除")

        # 1. Remove invalid depends_on
        all_ids = set(self._milestones.keys())
        for m in self._milestones.values():
            invalid = [d for d in m.depends_on if d not in all_ids]
            if invalid:
                m.depends_on = [d for d in m.depends_on if d in all_ids]
                fixes.append(f"子目标「{m.name}」移除无效依赖 {invalid}")

        # 2. Break cycles
        visited: set[str] = set()
        in_stack: set[str] = set()
        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False
        for mid in self._order:
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                self._milestones[mid].depends_on = []
                fixes.append(f"清除子目标「{self._milestones[mid].name}」的依赖以打破循环")

        # 3. Fill empty success_condition
        for m in self._milestones.values():
            if not m.success_condition.strip():
                m.success_condition = f"完成「{m.name}」"
                fixes.append(f"子目标「{m.name}」补全空的验收条件")

        # 4. Fix collection completion_strategy
        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                m.completion_strategy = "scroll_until_boundary"
                fixes.append(f"子目标「{m.name}」策略修正为 scroll_until_boundary")

        # 5. Derive stop_condition from dependency chain for all scroll milestones
        scroll_milestones = [
            m for m in self._milestones.values()
            if m.completion_strategy == "scroll_until_boundary"
        ]
        for m in scroll_milestones:
            dep_context = ""
            if m.depends_on:
                dep_lines = []
                for dep_id in m.depends_on:
                    dep = self._milestones.get(dep_id)
                    if dep:
                        dep_lines.append(f"  - 前置子目标「{dep.name}」验收条件：{dep.success_condition}")
                if dep_lines:
                    dep_context = "\n".join(dep_lines)
            existing = f"\n当前停止条件：{m.scroll_stop_condition}" if m.scroll_stop_condition else "\n当前停止条件：（空）"
            patch = invoke_structured(
                llm,
                [
                    SystemMessage(content=STOP_CONDITION_PATCH_PROMPT),
                    HumanMessage(content=(
                        f"用户目标：{goal}\n"
                        f"子目标名称：{m.name}\n"
                        f"子目标描述：{m.description}\n"
                        f"本子目标验收条件：{m.success_condition}\n"
                        f"{dep_context}\n"
                        f"全局约束：{json.dumps(self._global_constraints, ensure_ascii=False)}"
                        f"{existing}"
                    )),
                ],
                _StopConditionPatch,
            )
            if patch.scroll_stop_condition != m.scroll_stop_condition:
                fixes.append(
                    f"子目标「{m.name}」停止条件修正：{m.scroll_stop_condition or '（空）'} → {patch.scroll_stop_condition}"
                )
                m.scroll_stop_condition = patch.scroll_stop_condition
                m.observable_boundary = patch.observable_boundary

        # 6. Fix task_type
        analysis_keywords = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
        if self.task_type == "action" and any(kw in goal for kw in analysis_keywords):
            self.task_type = "analysis"
            fixes.append("task_type 从 action 修正为 analysis")

        if fixes:
            print(f"  [Guard] 补丁修复 {len(fixes)} 项：")
            for f in fixes:
                print(f"  [Guard]   {f}")

    def _next_milestone(self) -> Optional[str]:
        for mid in self._order:
            m = self._milestones[mid]
            if m.status != "pending":
                continue
            if all(self._milestones[dep].status == "done" for dep in m.depends_on):
                return mid
        return None

    def _llm(self) -> ChatOpenAI:
        return _make_llm()

    def _msgs(self, system_prompt: str, observation: Observation) -> list:
        return _build_msgs(system_prompt, observation.png_bytes)

    @staticmethod
    def _is_sequence(instruction: str) -> bool:
        text = instruction.strip()
        markers = ("操作序列", "步骤", "\n1.", "\n2.", "1.", "2.", "；2", ";2")
        return any(m in text for m in markers)

    def _is_repeated_instruction(
        self, instruction: str, milestone_id: str, history: list[PolicyTurn],
    ) -> bool:
        """Check if the instruction repeats a previously tried one for the same milestone."""
        from difflib import SequenceMatcher
        # Only check against instructions that LED TO STUCK, not all executed ones.
        # A normally executed instruction can be retried (e.g. same tap on same screen).
        tried = set()
        for idx, t in enumerate(history):
            sv = t.supervisor
            if not sv or not sv.instruction or sv.milestone_id != milestone_id:
                continue
            # Check if the NEXT turn detected stuck for the same milestone
            next_sv = history[idx + 1].supervisor if idx + 1 < len(history) else None
            if (
                next_sv
                and next_sv.milestone_id == milestone_id
                and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
            ):
                tried.add(sv.instruction)
        if not tried:
            return False
        n_new = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", instruction.strip())
        for old in tried:
            n_old = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", old.strip())
            if n_new and n_old:
                ratio = SequenceMatcher(None, n_new, n_old).ratio()
                if ratio >= 0.6:
                    return True
        return False


# ── Helpers ───────────────────────────────────────────────────────────


def _is_loop(milestone: Milestone) -> bool:
    return (
        milestone.kind == "collection"
        and milestone.completion_strategy == "scroll_until_boundary"
    )


def _last_scroll_was_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return bool(
        history
        and history[-1].supervisor.milestone_id == milestone_id
        and history[-1].action_decision
        and history[-1].action_decision.action.action_type == "scroll"
        and history[-1].executed
    )


def _has_collected(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id and t.read_added_content
        for t in history
    )


def _is_blank_screen(png_bytes: bytes) -> bool:
    """Return True if screenshot is a blank/loading white screen.

    Checks the ratio of near-white pixels (> 250) rather than mean brightness,
    because the iPhone Mirroring frame and status bar icons drag the mean down
    even when the app content area is fully white.
    Blank/loading screens have >80% near-white pixels; normal pages have <75%.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    pixels = img.tobytes()
    near_white = sum(1 for p in pixels if p > 250)
    return near_white / len(pixels) > BLANK_SCREEN_RATIO


def _default_read_instruction(milestone: Milestone) -> str:
    return (
        f"提取当前屏幕中与「{milestone.name}」相关的所有可见内容，"
        "保留名称/标题、时间/位置、目标相关数值、状态、类别等字段；如果是列表，逐条提取。"
    )


def _ctx(milestone: Milestone, read_instruction: Optional[str], collection_scope=None) -> dict:
    allow_read = milestone.kind in {"collection", "verification"}
    return {
        "read_instruction": read_instruction,
        "allow_read": bool(read_instruction and allow_read),
        "milestone_id": milestone.id,
        "milestone_kind": milestone.kind,
        "completion_strategy": milestone.completion_strategy,
        "collection_scope": collection_scope,
    }


def _png_sim(png1: bytes, png2: bytes, size: int = 64) -> float:
    img1 = Image.open(io.BytesIO(png1)).convert("L").resize((size, size))
    img2 = Image.open(io.BytesIO(png2)).convert("L").resize((size, size))
    total = sum(abs(int(a) - int(b)) for a, b in zip(img1.getdata(), img2.getdata()))
    return 1.0 - total / (255 * size * size)
