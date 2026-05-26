"""Content Reader: extract relevant information from a screenshot during browsing."""

import base64
import json

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from policy_expr.config import resolve_llm_config
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.schemas import CollectionScope, SupervisorStep

load_dotenv()

SYSTEM_PROMPT = """\
从截图中提取所有可见的文字内容。
- 每条记录一行，字段用|分隔（时间|名称|内容|状态 等）
- 保留所有可见字段，不做筛选、不汇总、不解释
- 最多200字
- 无可见文字内容则回复"无相关内容"
"""


class ContentReader:
    """Extract content notes from a screenshot relevant to the user's goal."""

    def __init__(self) -> None:
        cfg = resolve_llm_config("reader")
        self._llm = ChatOpenAI(
            model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
            extra_body={"enable_thinking": False},
        )

    def read(self, png_bytes: bytes, goal: str) -> str:
        """Return a brief content summary extracted from the screenshot."""
        b64 = base64.b64encode(resize_to_logical_png(png_bytes)).decode()
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=[
                {"type": "text", "text": "请提取当前截图中所有可见的文字内容："},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]),
        ]
        response = self._llm.invoke(messages)
        text = response.content if isinstance(response.content, str) else str(response.content)
        return text.strip()


def build_reader_instruction(original_goal: str, sv_step: SupervisorStep) -> str:
    """Build the extraction prompt for ContentReader based on milestone kind."""
    instruction = sv_step.read_instruction or original_goal
    if sv_step.milestone_kind != "collection":
        return instruction
    return (
        f"目标：{original_goal}\n"
        f"采集要求：{instruction}\n"
        "逐条提取当前屏幕可见记录，每条一行。记录可见范围/边界。"
    )


def annotate_content_note(
    note: str,
    *,
    turn_no: int,
    sv_step: SupervisorStep,
    collection_scope: CollectionScope | None,
) -> str:
    """Prepend collection metadata to a content note for traceability."""
    if sv_step.milestone_kind != "collection":
        return note
    metadata = [f"[turn{turn_no} {sv_step.milestone_id or '?'}]"]
    if collection_scope:
        metadata.append(
            "范围:" + json.dumps(collection_scope.model_dump(exclude_none=True), ensure_ascii=False)
        )
    return " ".join(metadata) + "\n" + note
