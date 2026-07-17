"""Content Reader: extract relevant information from a screenshot during browsing."""

import base64
import json
from collections.abc import Callable

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import CollectionScope, SupervisorStep
from gui_agent.prompts import load_prompt_text

load_dotenv()

SYSTEM_PROMPT = load_prompt_text("task.reader.screenshot_text")


class ContentReader:
    """Extract content notes from a screenshot relevant to the user's goal."""

    def __init__(
        self,
        prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    ) -> None:
        from llm.provider_config import dashscope_extra_body

        cfg = resolve_llm_config("reader")
        self._llm = ChatOpenAI(
            model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
            timeout=cfg.timeout_s, max_retries=cfg.max_retries,
            extra_body=dashscope_extra_body(cfg.model),
        )
        self._prepare_vision_prompt_png = prepare_vision_prompt_png or (lambda b: b)

    def read(self, png_bytes: bytes, goal: str) -> str:
        """Return a brief content summary extracted from the screenshot."""
        b64 = base64.b64encode(self._prepare_vision_prompt_png(png_bytes)).decode()
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
    """Build the extraction prompt declared by Transition."""
    return sv_step.read_instruction or original_goal


def annotate_content_note(
    note: str,
    *,
    turn_no: int,
    sv_step: SupervisorStep,
    collection_scope: CollectionScope | None,
) -> str:
    """Prepend collection metadata to a content note for traceability."""
    metadata = [f"[turn{turn_no} {sv_step.statement_id or '?'}]"]
    if collection_scope:
        metadata.append(
            "范围:" + json.dumps(collection_scope.model_dump(exclude_none=True), ensure_ascii=False)
        )
    return " ".join(metadata) + "\n" + note
