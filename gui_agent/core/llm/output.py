"""Unified reply generation for policy experiment runs."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text


_ACTION_SYSTEM = load_prompt_text("task.output.action_summary")

_CHAT_SYSTEM = load_prompt_text("task.output.chat_reply")

_ORCH_SYSTEM = load_prompt_text("task.output.orchestration_reply")


def generate_reply(
    goal: str,
    result: dict | None,
    *,
    session: list[dict] | None = None,
    non_action_reason: str = "",
) -> str:
    """Generate a user-facing reply.

    session=None  → runner mode (ACTION / ANALYSIS prompt)
    session=list  → chat mode (CHAT prompt with session context)
    """
    from llm.provider_config import dashscope_extra_body

    cfg = resolve_llm_config("output")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
        extra_body=dashscope_extra_body(cfg.model),
    )

    if session is not None:
        messages = _chat_messages(goal, result, session, non_action_reason)
    else:
        messages = _action_messages(goal, result or {})

    return _message_text(llm.invoke(messages).content).strip()


def compose_orchestration_reply(
    goal: str,
    run_log: list[dict],
    *,
    current: str = "",
    terminal: str = "",
) -> str:
    """Comprehensive final reply for DSL orchestrator runs: synthesize 已完成 / 读取发现 /
    未完成 / 结论 from the WHOLE program's structured state, not just the last finish line.

    run_log: ordered statements [{name, executor, phase, verification, outputs:{字段:值}, summary}].
    current: the in-progress (uncompleted) statement name when interrupted (max_turns), else "".
    terminal: how the program ended — the finish/failure reply, or "达到最大轮数 N" etc."""
    from llm.provider_config import dashscope_extra_body

    cfg = resolve_llm_config("output")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
        extra_body=dashscope_extra_body(cfg.model),
    )
    lines = []
    for i, r in enumerate(run_log, 1):
        phase = r.get("phase")
        verification = r.get("verification")
        mark = (
            "△ 已派发，结果未验证"
            if verification == "accepted_unverified"
            else ("✓ 完成" if phase == "completed" else "✗ 未完成")
        )
        lines.append(f"{i}. {r.get('name', '')} — {mark}")
        outputs = {k: v for k, v in (r.get("outputs") or {}).items() if v is not None}
        if outputs:
            lines.append("   输出：" + "；".join(f"{k}={v}" for k, v in outputs.items()))
    if current:
        lines.append(f"（当前进行中、尚未完成：{current}）")
    digest = "\n".join(lines) or "（无已完成步骤）"
    human = f"用户目标：{goal}\n\n执行轨迹：\n{digest}\n\n结束原因：{terminal or '程序正常结束'}"
    return _message_text(
        llm.invoke([SystemMessage(content=_ORCH_SYSTEM), HumanMessage(content=human)]).content
    ).strip()


# ── Message builders ───────────────────────────────────────────────────────


def _chat_messages(
    goal: str,
    result: dict | None,
    session: list[dict],
    non_action_reason: str,
) -> list:
    history = _fmt_session(session)
    if result is None:
        exec_text = f"本次未执行操作。原因：{non_action_reason or '未说明'}"
    else:
        phase = result.get("phase") or "stopped"
        verification = result.get("verification")
        status = (
            "成功"
            if phase == "completed" and verification == "confirmed"
            else "已执行但未独立验真"
            if phase == "completed"
            else "失败"
        )
        turns_detail = result.get("turns_detail", [])
        last_action = ""
        for t in reversed(turns_detail):
            if t.get("action_type") and t.get("executed"):
                last_action = f"[{t['action_type']}] {t['action_desc']}"
                break
        # pre_existing is set by the supervisor when a statement was found done
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
                f"输出：{result.get('output', '')}\n"
                f"运行结论：{result.get('summary', '')}\n"
                f"最后执行动作：{last_action or '无'}"
            )
    return [
        SystemMessage(content=_CHAT_SYSTEM),
        HumanMessage(content=f"对话历史：\n{history}\n\n执行上下文：\n{exec_text}\n\n用户说：{goal}"),
    ]


def _action_messages(goal: str, result: dict) -> list:
    ctx = {
        "goal": goal,
        "summary": result.get("summary", ""),
        "phase": result.get("phase", "stopped"),
        "verification": result.get("verification"),
        "turn_count": result.get("turns_count", 0),
        "output": result.get("output", ""),
        "turns": result.get("turns_detail", []),
    }
    return [
        SystemMessage(content=_ACTION_SYSTEM),
        HumanMessage(content=f"请根据以下运行 context 生成最终摘要：\n{json.dumps(ctx, ensure_ascii=False, indent=2)}"),
    ]


# ── Helpers ────────────────────────────────────────────────────────────────


def _fmt_session(session: list[dict]) -> str:
    if not session:
        return "（无）"
    lines = []
    for i, e in enumerate(session, 1):
        status = (
            "✓"
            if e.get("phase") == "completed" and e.get("verification") == "confirmed"
            else "~"
            if e.get("phase") == "completed"
            else "✗"
        )
        lines.append(f"{i}. 用户说「{e['user_msg']}」→ {status} {e['output']}")
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
