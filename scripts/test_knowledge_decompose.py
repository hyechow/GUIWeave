"""Compare task decomposition with and without app knowledge.

Tests several WeChat-related goals, calls _do_decompose with/without
_app.md injected, and prints side-by-side milestone comparison.

Usage:
    uv run python scripts/test_knowledge_decompose.py
"""

import base64
import io
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from policy_expr.core.config import resolve_llm_config
from policy_expr.core.policies.base import resize_to_logical_png
from policy_expr.core.schemas import Observation
from policy_expr.core.self_learning.app_summary import auto_discover_knowledge
from policy_expr.core.supervisor.milestone import (
    DECOMPOSE_PROMPT,
    _DecomposeResponse,
    Milestone,
)

# ── Goals covering different navigation depths in WeChat ──
GOALS = [
    # 简单: 2-3 步操作
    "在微信里发消息给张三",
    "查看微信朋友圈",
    # 中等: 跨 tab 导航 + 操作
    "在微信通讯录里找到一个联系人并发送好友申请",
    "在微信搜索一篇关于AI的公众号文章并收藏",
    # 复杂: 深层路径，需要经过多个页面
    "在微信设置里关闭朋友圈入口，然后回到聊天列表",
    "查看微信群里所有未读消息，统计有多少条@我的",
    "在微信个人中心里修改头像",
    # 跨模块: 需要切换多个 tab
    "在微信游戏里找一个游戏并分享给好友",
]

SCREENSHOT_PATH = Path(__file__).parent.parent / "logs" / "test_home.png"


def build_messages(
    goal: str, observation: Observation, knowledge: str | None = None,
) -> list:
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    b64 = base64.b64encode(resize_to_logical_png(observation.png_bytes)).decode()

    msgs = [
        SystemMessage(content=f"{DECOMPOSE_PROMPT}\n\n当前日期：{today}"),
        HumanMessage(content=[
            {"type": "text", "text": "请根据当前屏幕做出决策。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]

    user_parts: list[dict] = [{"type": "text", "text": f"用户任务：{goal}"}]
    if knowledge:
        user_parts.append({"type": "text", "text": f"\n## 应用导航知识\n{knowledge}"})
    msgs[1].content = user_parts + msgs[1].content
    return msgs


def decompose(
    goal: str, observation: Observation, knowledge: str | None = None,
) -> _DecomposeResponse:
    cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )
    msgs = build_messages(goal, observation, knowledge)
    return invoke_structured(llm, msgs, _DecomposeResponse)


def format_milestones(milestones: list[Milestone]) -> str:
    lines = []
    for m in milestones:
        deps = f" depends=[{', '.join(m.depends_on)}]" if m.depends_on else ""
        lines.append(f"  [{m.id}] {m.name} ({m.kind}/{m.completion_strategy}){deps}")
        lines.append(f"       验收: {m.success_condition}")
    return "\n".join(lines)


def find_screenshot() -> bytes:
    if SCREENSHOT_PATH.exists():
        print(f"Using screenshot: {SCREENSHOT_PATH}")
        return SCREENSHOT_PATH.read_bytes()

    logs = Path(__file__).parent.parent / "logs" / "policy_expr"
    candidates = sorted(logs.rglob("screenshot*.png")) if logs.exists() else []
    if candidates:
        print(f"Using screenshot: {candidates[-1]}")
        return candidates[-1].read_bytes()

    print("No screenshot found. Creating blank 1170x2532.")
    from PIL import Image

    img = Image.new("RGB", (1170, 2532), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main():
    png_bytes = find_screenshot()
    observation = Observation(png_bytes=png_bytes, source="test")
    knowledge, _ = auto_discover_knowledge("微信")

    print(f"Knowledge: {'loaded' if knowledge else 'none'} ({len(knowledge or '')} chars)")
    print("=" * 80)

    for goal in GOALS:
        print(f"\n{'=' * 80}")
        print(f"GOAL: {goal}")
        print("=" * 80)

        try:
            resp_no = decompose(goal, observation, knowledge=None)
        except Exception as e:
            print(f"WITHOUT knowledge ERROR: {e}")
            continue

        try:
            resp_yes = decompose(goal, observation, knowledge=knowledge)
        except Exception as e:
            print(f"WITH knowledge ERROR: {e}")
            continue

        # Side-by-side
        print(f"\n  {'WITHOUT':40s} | {'WITH':s}")
        print(f"  {'-' * 40} | {'-' * 40}")
        print(f"  task_type={resp_no.task_type:<28s} | task_type={resp_yes.task_type}")
        print(f"  milestones={len(resp_no.milestones):<27d} | milestones={len(resp_yes.milestones)}")

        # Detail
        print("\n--- WITHOUT ---")
        print(format_milestones(resp_no.milestones))
        print("\n--- WITH ---")
        print(format_milestones(resp_yes.milestones))

        # Diff
        print("\n--- DIFF ---")
        names_no = {m.name for m in resp_no.milestones}
        names_yes = {m.name for m in resp_yes.milestones}
        added = names_yes - names_no
        removed = names_no - names_yes
        if added:
            print(f"  新增: {', '.join(sorted(added))}")
        if removed:
            print(f"  减少: {', '.join(sorted(removed))}")

        by_name_no = {m.name: m for m in resp_no.milestones}
        by_name_yes = {m.name: m for m in resp_yes.milestones}
        for name in sorted(names_no & names_yes):
            m_no, m_yes = by_name_no[name], by_name_yes[name]
            diffs = []
            if m_no.success_condition != m_yes.success_condition:
                diffs.append(f"验收: 「{m_no.success_condition}」→「{m_yes.success_condition}」")
            if m_no.kind != m_yes.kind:
                diffs.append(f"kind: {m_no.kind}→{m_yes.kind}")
            if m_no.completion_strategy != m_yes.completion_strategy:
                diffs.append(f"strategy: {m_no.completion_strategy}→{m_yes.completion_strategy}")
            if diffs:
                print(f"  [{name}] {'; '.join(diffs)}")

        if not added and not removed:
            common = names_no & names_yes
            has_diffs = any(
                by_name_no[n].success_condition != by_name_yes[n].success_condition
                or by_name_no[n].kind != by_name_yes[n].kind
                or by_name_no[n].completion_strategy != by_name_yes[n].completion_strategy
                for n in common
            )
            if not has_diffs:
                print("  (结构相同，无显著差异)")


if __name__ == "__main__":
    main()
