"""Unified reply generation for policy experiment runs."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from policy_expr.config import resolve_llm_config


_ACTION_SYSTEM = """\
你是 iPhone 自动化任务的最终结果总结助手。
你会收到一次策略运行的完整 context，包括用户目标、停止原因、每轮动作和执行状态。
请基于这些事实判断任务最终状态，并用中文输出给用户看的简短摘要。

要求：
- 不要输出详细 Markdown 报告，不要逐轮罗列日志。
- 控制在 3-6 句话。
- 必须说明任务是否已完成、关键依据。
- 如果 context 无法确认完成，不要猜测，明确说"未确认"或"未完成"。
- 不要提及停止原因、运行模式、日志目录或日志保存位置。
- 不要在结尾追加"任务因...停止""完整日志保存在..."之类的运行说明。
"""

_ANALYSIS_SYSTEM = """\
你是 iPhone 信息收集任务的最终结果整理助手。
用户让 agent 在手机上浏览并收集信息，agent 已逐页提取了屏幕上的原始文字内容。
你的任务是从这些原始内容中筛选、整理出直接回答用户目标的信息。

要求：
- 原始内容是逐帧提取的屏幕文字，可能包含无关内容（导航栏、按钮文字、广告等），你需要根据用户目标筛选出相关部分
- 直接呈现筛选后的信息，不要描述 agent 的操作过程
- 合并重复内容，保留关键细节
- 如果用户目标包含数量/条件限制（如「最近3条」「金额大于100」），按条件过滤
- 如果信息不完整，如实说明，不要补充截图中没有的内容
- 不要提及"agent"、"截图"、"收集"等操作性词汇，直接给出答案
- 不要在结尾追加运行说明
- 不要加免责声明或多余的解释（如"无法确认""缺乏定义"），直接给结论
- 如果用户目标包含「上周」「本周」「今天」「昨天」等相对日期，优先使用补充上下文中给出的解析日期范围；禁止说原始数据未明确该相对日期的定义
- 如果页面筛选范围比用户目标更宽，只从原始内容中筛选符合用户目标日期范围的记录，不要把宽筛选范围说成答案范围
- 如果是金额/交易/订单类数据，用 Markdown 表格呈现，列包含：日期、描述、金额
- 表格后附一行总计
"""

_CHAT_SYSTEM = """\
你是 Lucas，一个 iPhone GUI Agent。根据对话历史和执行上下文，用简洁自然的中文回复用户。

规则：
- 执行了手机操作：说明结果（成功/失败）和关键信息，简洁即可
- 未执行操作（询问身份、历史回顾、闲聊等）：直接回答，不要解释内部细节
- 语气自然友好，不要啰嗦，不要重复用户的问题

重要：当执行上下文显示「仅执行了导航，目标内容已存在」时，说明用户要求的写入/发送操作结果（消息、记录、设置）在本次会话启动前就已存在，智能体本次没有新写入任何内容。此时必须如实告知用户目标内容已存在（例如"发现文件传输助手里已有这条记录"），不能说"已经帮你完成了"或"已经发送了"。
"""


def generate_reply(
    goal: str,
    result: dict | None,
    *,
    session: list[dict] | None = None,
    non_action_reason: str = "",
    content_notes: list[str] | None = None,
    collection_context: str | None = None,
) -> str:
    """Generate a user-facing reply.

    session=None  → runner mode (ACTION / ANALYSIS prompt)
    session=list  → chat mode (CHAT prompt with session context)
    """
    cfg = resolve_llm_config("output")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url, extra_body={"enable_thinking": False})

    if content_notes:
        messages = _analysis_messages(goal, result, content_notes, collection_context)
    elif session is not None:
        messages = _chat_messages(goal, result, session, non_action_reason)
    else:
        messages = _action_messages(goal, result or {})

    return _message_text(llm.invoke(messages).content).strip()


# ── Message builders ───────────────────────────────────────────────────────


def _analysis_messages(
    goal: str,
    result: dict | None,
    content_notes: list[str],
    collection_context: str | None,
) -> list:
    notes_text = "\n\n".join(f"[片段 {i+1}]\n{n}" for i, n in enumerate(content_notes))
    reply_context = _build_reply_context(goal, result, content_notes, collection_context)
    if reply_context:
        notes_text = f"[补充上下文]\n{reply_context}\n\n以下为逐帧提取的内容片段：\n\n{notes_text}"
    stop_reason = (result or {}).get("stop_reason", "")
    return [
        SystemMessage(content=_ANALYSIS_SYSTEM),
        HumanMessage(content=f"用户目标：{goal}\n\n运行结论：{stop_reason}\n\n收集到的内容片段：\n{notes_text}"),
    ]


def _chat_messages(
    goal: str,
    result: dict | None,
    session: list[dict],
    non_action_reason: str,
) -> list:
    history = _fmt_session(session)
    if result is None:
        exec_text = f"本次未执行手机操作。原因：{non_action_reason or '未说明'}"
    else:
        status = "成功" if result.get("goal_completed") else "失败"
        turns_detail = result.get("turns_detail", [])
        last_action = ""
        for t in reversed(turns_detail):
            if t.get("action_type") and t.get("executed"):
                last_action = f"[{t['action_type']}] {t['action_desc']}"
                break
        # pre_existing is set by the supervisor when a milestone was found done
        # without the agent executing any actions for it (target state already existed).
        pre_existing = result.get("pre_existing", False)
        if pre_existing:
            exec_text = (
                f"⚠️ 智能体本次未执行用户要求的核心操作\n"
                f"实际执行：仅导航（{last_action}），无 type/send 动作\n"
                f"发现：目标内容在本次会话启动前就已存在\n"
                f"回复要求：直接说「发现XX已存在/已有这条记录」，禁止以「已帮你…」开头"
            )
        else:
            exec_text = (
                f"执行状态：{status}\n"
                f"轮数：{result.get('turns_count', 0)}\n"
                f"摘要：{result.get('result_summary', '')}\n"
                f"停止原因：{result.get('stop_reason', '')}\n"
                f"最后执行动作：{last_action or '无'}"
            )
    return [
        SystemMessage(content=_CHAT_SYSTEM),
        HumanMessage(content=f"对话历史：\n{history}\n\n执行上下文：\n{exec_text}\n\n用户说：{goal}"),
    ]


def _action_messages(goal: str, result: dict) -> list:
    ctx = {
        "goal": goal,
        "stop_reason": result.get("stop_reason", ""),
        "goal_completed": result.get("goal_completed", False),
        "turn_count": result.get("turns_count", 0),
        "summary": result.get("result_summary", ""),
        "turns": result.get("turns_detail", []),
    }
    return [
        SystemMessage(content=_ACTION_SYSTEM),
        HumanMessage(content=f"请根据以下运行 context 生成最终摘要：\n{json.dumps(ctx, ensure_ascii=False, indent=2)}"),
    ]


# ── Helpers ────────────────────────────────────────────────────────────────


def _build_reply_context(
    goal: str,
    result: dict | None,
    content_notes: list[str],
    collection_context: str | None,
) -> str:
    lines = [f"当前日期：{_today().strftime('%Y-%m-%d')}"]
    goal_period = _infer_goal_period(goal, content_notes, (result or {}).get("collection_scope"))
    if goal_period:
        start, end, source = goal_period
        lines.append(f"用户目标日期范围：{start} 至 {end}（{source}）")
        lines.append("输出时只保留该日期范围内的记录；不要声称“上周”定义不明确。")
    if collection_context:
        lines.append(f"采集状态：{collection_context}")
    collection_scope = (result or {}).get("collection_scope")
    if collection_scope:
        lines.append("采集范围元数据：" + json.dumps(collection_scope, ensure_ascii=False))
    return "\n".join(lines)


def _infer_goal_period(
    goal: str,
    content_notes: list[str],
    collection_scope: dict | None = None,
) -> tuple[str, str, str] | None:
    # Check for pre-resolved ISO date range in goal (e.g. "2026-05-18至2026-05-24")
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})", goal)
    if iso_match:
        return iso_match.group(1), iso_match.group(2), "goal 中已解析的日期范围"

    if "上周" not in goal:
        return None

    if (
        collection_scope
        and "上周" in str(collection_scope.get("label", ""))
        and collection_scope.get("start")
        and collection_scope.get("end")
    ):
        return str(collection_scope["start"]), str(collection_scope["end"]), "采集范围元数据"

    joined = "\n".join(content_notes)
    metadata_match = re.search(
        r"范围:\{[^}\n]*?\"label\"\s*:\s*\"[^\"]*上周[^\"]*\"[^}\n]*?\"start\"\s*:\s*\"(\d{4}-\d{2}-\d{2})\"[^}\n]*?\"end\"\s*:\s*\"(\d{4}-\d{2}-\d{2})\"",
        joined,
    )
    if metadata_match:
        return metadata_match.group(1), metadata_match.group(2), "采集元数据"

    text_match = re.search(r"上周[^\d]*(\d{4}-\d{2}-\d{2})\s*(?:至|到|-)\s*(\d{4}-\d{2}-\d{2})", joined)
    if text_match:
        return text_match.group(1), text_match.group(2), "采集上下文"

    # Fallback for common colloquial usage in this assistant: the seven days
    # before today. Explicit context from the supervisor/reader always wins.
    today = _today()
    start = today - timedelta(days=7)
    end = today - timedelta(days=1)
    return start.isoformat(), end.isoformat(), "按当前日期推导（最近7天，不含今天）"


def _today() -> date:
    return datetime.now().astimezone().date()


def _fmt_session(session: list[dict]) -> str:
    if not session:
        return "（无）"
    lines = []
    for i, e in enumerate(session, 1):
        status = "✓" if e.get("goal_completed") else "✗"
        lines.append(f"{i}. 用户说「{e['user_msg']}」→ {status} {e['result_summary']}")
    return "\n".join(lines)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)
