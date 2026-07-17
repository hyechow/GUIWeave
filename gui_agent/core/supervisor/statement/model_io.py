"""Model I/O for the single live Statement Transition decision."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    constraints_block,
    knowledge_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages, prepare_prompt_png
from gui_agent.core.run.statement_memory import StatementMemoryView
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .observation_view import StatementObservationView, build_observation_view
from .schemas import StatementPrompts, _StatementTransitionResult


load_dotenv()


# `@<path>` file references inside the goal text (e.g. 「按 @tmp_scripts/sim.json 的配置新建」).
# A token runs until whitespace / CJK punctuation / quotes; CJK chars themselves are allowed.
_TOKEN_BREAK = "，。！？；：、()（）【】《》<>[]" + "\"'" + "“”‘’"
_FILE_REF_RE = re.compile(rf"@([^\s@{re.escape(_TOKEN_BREAK)}]+)")
_FILE_REF_MAX_CHARS = 50_000
_FILE_REF_TOTAL_MAX_CHARS = 60_000


def resolve_file_refs(goal: str, base: Optional[Path] = None) -> str:
    """Return one bounded prompt section for resolvable ``@<path>`` references."""
    base = base or Path.cwd()
    sections: list[str] = []
    seen: set[str] = set()
    total_chars = 0
    omitted: list[str] = []
    for raw in _FILE_REF_RE.findall(goal):
        cand = raw.rstrip(".,;:!?")
        path: Optional[Path] = None
        while cand:
            candidate = Path(cand).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            if candidate.is_file():
                path = candidate
                break
            cand = cand[:-1]
        if path is None:
            print(f"  [FileRef] @{raw} 未解析到文件，按普通文本处理")
            continue
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            with path.open("rb") as source:
                head = source.read(8192)
        except OSError as exc:
            print(f"  [FileRef] 读取失败 {path}：{exc}")
            continue

        def binary_section() -> str:
            print(f"  [FileRef] @{cand} 是二进制文件，作为上传/导入目标路径处理（不注入内容）")
            return (
                f"### @{cand}\n二进制文件（上传/导入的目标）。"
                f"本地完整路径，上传时原样使用：\n{path}"
            )

        if b"\x00" in head:
            sections.append(binary_section())
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            sections.append(binary_section())
            continue
        except OSError as exc:
            print(f"  [FileRef] 读取失败 {path}：{exc}")
            continue
        if len(text) > _FILE_REF_MAX_CHARS:
            text = text[:_FILE_REF_MAX_CHARS] + "\n…（文件过长，已截断）"
        remaining = _FILE_REF_TOTAL_MAX_CHARS - total_chars
        if remaining <= 0:
            omitted.append(cand)
            print(f"  [FileRef] @{cand} 跳过：引用文件总量已达上限 {_FILE_REF_TOTAL_MAX_CHARS} 字符")
            continue
        if len(text) > remaining:
            text = text[:remaining] + "\n…（引用文件总量超上限，已截断）"
            omitted.append(cand)
        total_chars += len(text)
        print(f"  [FileRef] 注入 @{cand}（{len(text)} 字符）")
        sections.append(f"### @{cand}\n{text}")
    if not sections:
        return ""
    if omitted:
        sections.append(
            "### ⚠️ 引用文件总量超上限\n"
            f"以下 @ 引用因总量超过 {_FILE_REF_TOTAL_MAX_CHARS} 字符被截断或省略，"
            f"如需其字段值请拆分任务或精简文件：{'、'.join(dict.fromkeys(omitted))}"
        )
    return (
        "## 引用文件内容（任务中 @ 引用的文件；其中的字段值须严格按原文使用，不得改动或省略）\n"
        + "\n\n".join(sections)
    )


def _make_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
    )


def _prepare_prompt_png(png_bytes: bytes, image_resize: str = "retina") -> bytes:
    return prepare_prompt_png(png_bytes, image_resize=image_resize)


def _build_msgs(
    system_prompt: str,
    png_bytes: bytes,
    *,
    image_resize: str = "retina",
) -> list:
    """Small compatibility-free wrapper shared by image-resize unit tests."""
    return assemble_messages(system_prompt, png_bytes, image_resize=image_resize)


def _last_action_result(memory: StatementMemoryView) -> str:
    if not memory.recent_steps and not memory.durable_facts:
        return "none"
    if memory.recent_steps and "no_effect" in memory.recent_steps[-1].text:
        return "no_effect"
    for fact in reversed(memory.durable_facts):
        if fact.kind.startswith("effect_satisfied"):
            return "effective"
        if fact.kind != "action_receipt":
            continue
        response = str(fact.metadata.get("response") or "")
        if response == "none_observed":
            return "no_effect"
        if response == "observed":
            return "effective"
        return "unknown"
    return "unknown"


def _compact_control_state(observation: Observation) -> list[dict]:
    """Remove geometry and duplicated adapter detail from the semantic state channel."""
    source = observation.form_control_state or observation.form_controls or []
    keys = (
        "kind",
        "label",
        "name",
        "id",
        "value",
        "selected_text",
        "checked",
        "required",
        "in_viewport",
        "viewport_pos",
        "group_id",
        "group_index",
        "group_field",
        "options",
    )
    return [
        {key: item[key] for key in keys if key in item}
        for item in source
        if isinstance(item, dict)
    ]


def _compact_affordances(view: StatementObservationView) -> list[dict]:
    """Keep target identity and capability; geometry stays in the adapter observation."""
    keys = ("label", "ref", "role", "visibility", "supported_operations")
    return [
        {key: item[key] for key in keys if key in item}
        for item in view.affordances
    ]


def _transition_frame_block(
    statement: StatementContract,
    observation: Observation,
    memory: StatementMemoryView,
    view: StatementObservationView,
    *,
    initial_filters: dict[str, str] | None,
) -> ContextBlock:
    """Build the one decision packet; it contains facts, never a Runtime verdict."""
    durable = [
        {
            "kind": fact.kind,
            "event_ref": fact.event_ref,
            "text": fact.text,
            "metadata": fact.metadata,
        }
        for fact in memory.durable_facts
    ]
    frame = {
        "contract": statement.model_dump(mode="json", exclude_none=True),
        "memory": {
            "instance_id": memory.instance_id,
            "durable_facts": durable,
            "recent_steps": [
                {"event_ref": step.event_ref, "text": step.text}
                for step in memory.recent_steps
            ],
            "compressed_history": list(memory.compressed_history),
            "last_action_result": _last_action_result(memory),
        },
        "observation": {
            "title": observation.title,
            "url": observation.url,
            "affordance_coverage": view.affordance_coverage,
            "control_state": _compact_control_state(observation),
            "applied_filters": observation.applied_filters or {},
            "initial_filters": initial_filters or {},
            "tables": observation.tables or [],
            "affordances": _compact_affordances(view),
        },
    }
    return ContextBlock(
        id="runtime.transition_frame",
        budget="required",
        source_type="decision_frame",
        source="journal+observation+contract",
        ttl="turn",
        priority=20,
        content=(
            "## TransitionFrame（本帧唯一决策包）\n"
            "以下是合同、Journal 事实和当前观察；其中没有预先计算的完成状态或路线。\n"
            + json.dumps(
                frame,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        ),
    )


def run_statement_transition(
    statement: StatementContract,
    observation: Observation,
    *,
    memory_view: StatementMemoryView,
    observation_view: StatementObservationView | None = None,
    constraints: Optional[list[str]] = None,
    prompts: Optional[StatementPrompts] = None,
    context_reports: list[dict] | None = None,
    app_knowledge: Optional[str] = None,
    acceptance_knowledge: Optional[str] = None,
    elements_knowledge: Optional[str] = None,
    initial_filters: dict[str, str] | None = None,
) -> _StatementTransitionResult:
    """Return one semantic Statement decision for the current observation."""
    observation_view = observation_view or build_observation_view(
        statement, observation, []
    )
    prompts = prompts or StatementPrompts.neutral()
    prompt = (prompts.transition or "").strip() or load_prompt_text(
        "task.statement.transition"
    )
    messages = assemble_messages(
        prompt,
        observation,
        system_blocks=[
            _transition_frame_block(
                statement,
                observation,
                memory_view,
                observation_view,
                initial_filters=initial_filters,
            ),
            constraints_block(constraints or []),
        ],
        human_blocks=[
            knowledge_block("app_navigation", app_knowledge),
            knowledge_block("completion_evidence", acceptance_knowledge),
            knowledge_block("page_elements", elements_knowledge),
        ],
        image_resize=prompts.image_resize,
        label="transition",
        context_reports=context_reports,
    )
    return invoke_structured(
        _make_llm(),
        messages,
        _StatementTransitionResult,
        trace_sink=context_reports,
        trace_label="transition",
        fallback_on_invalid=False,
    )
