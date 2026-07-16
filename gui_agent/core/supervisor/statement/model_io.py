"""Model I/O for the single live Statement Transition decision."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    active_filters_block,
    applied_filter_state_block,
    browser_page_block,
    constraints_block,
    extra_instruction_block,
    filter_residual_block,
    form_controls_block,
    grid_status_block,
    knowledge_block,
    page_title_block,
    statement_memory_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages, prepare_prompt_png
from gui_agent.core.run.statement_memory import StatementMemoryView
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .observation_state import RuntimeFilterIntent, filter_residual_labels
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


def _evidence_pack_block(
    *,
    evaluation_reason: str = "",
    evaluation_status: str = "",
    evaluation_verification: str = "",
    persistence_summary: str = "",
) -> ContextBlock | None:
    lines = ["## 结构化证据摘要（非路线指令）"]
    if evaluation_status:
        lines.append(f"- evidence.status：{evaluation_status}")
    if evaluation_verification:
        lines.append(f"- evidence.completion_status：{evaluation_verification}")
    if evaluation_reason:
        lines.append(f"- evidence.reason：{evaluation_reason}")
    if persistence_summary:
        lines.append(f"- persistence：{persistence_summary}")
    lines.append(
        "- 说明：以上是 Runtime 从 Journal/观察投影的事实评估，"
        "只用于校验 complete，不规定下一动作。请结合 StatementMemory 自行决定。"
    )
    if len(lines) <= 2:
        return None
    return ContextBlock(
        id="runtime.evidence_pack",
        budget="required",
        source_type="runtime_state",
        source="execution_coordinator",
        ttl="turn",
        priority=24,
        content="\n".join(lines),
    )


def _semantic_actions_block(nodes: list[dict] | None) -> ContextBlock | None:
    """Expose positive AX navigation/action affordances without claiming full coverage."""
    interactive_roles = {
        "button",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "treeitem",
    }
    rows: list[str] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        role = str(node.get("role") or "").strip().lower()
        label = str(node.get("key") or "").strip()
        if role not in interactive_roles or not label:
            continue
        rows.append(f"- {role}: {label}")
        if len(rows) >= 120:
            break
    if not rows:
        return None
    rows.append(
        "- 注意：这是当前已暴露入口的正向清单，不保证覆盖折叠菜单或尚未渲染区域；"
        "缺失项本身不能证明 navigation infeasible。"
    )
    return ContextBlock(
        id="observation.semantic_actions",
        budget="high",
        source_type="observation",
        source="semantic_tree",
        ttl="turn",
        priority=31,
        content="## 当前语义交互入口\n" + "\n".join(rows),
    )


def run_statement_transition(
    statement: StatementContract,
    observation: Observation,
    *,
    memory_view: StatementMemoryView,
    constraints: Optional[list[str]] = None,
    extra: str = "",
    prompts: Optional[StatementPrompts] = None,
    context_reports: list[dict] | None = None,
    evaluation_reason: str = "",
    evaluation_status: str = "",
    evaluation_verification: str = "",
    persistence_summary: str = "",
    app_knowledge: Optional[str] = None,
    acceptance_knowledge: Optional[str] = None,
    elements_knowledge: Optional[str] = None,
    initial_filters: dict[str, str] | None = None,
    runtime_filter: RuntimeFilterIntent | None = None,
) -> _StatementTransitionResult:
    """Return one semantic Statement decision for the current observation."""
    prompts = prompts or StatementPrompts.neutral()
    prompt = (prompts.transition or "").strip() or load_prompt_text(
        "task.statement.transition"
    )
    mem_block = statement_memory_block(memory_view)
    if mem_block is None:
        raise ValueError("Statement Transition requires a non-empty StatementMemoryView")
    messages = assemble_messages(
        prompt,
        observation,
        system_blocks=[
            mem_block,
            _evidence_pack_block(
                evaluation_reason=evaluation_reason,
                evaluation_status=evaluation_status,
                evaluation_verification=evaluation_verification,
                persistence_summary=persistence_summary,
            ),
            constraints_block(constraints or []),
            extra_instruction_block(extra, source="transition_guard"),
            page_title_block(getattr(observation, "title", None)),
        ],
        human_blocks=[
            browser_page_block(getattr(observation, "url", None), None),
            active_filters_block(getattr(observation, "form_controls", None)),
            applied_filter_state_block(
                getattr(observation, "applied_filters", None),
                getattr(observation, "applied_filter_meta", None),
                initial_filters=initial_filters,
            ),
            filter_residual_block(
                filter_residual_labels(
                    getattr(observation, "applied_filters", None),
                    statement,
                    runtime_filter,
                ),
                getattr(observation, "applied_filters", None),
            ),
            form_controls_block(
                getattr(observation, "form_controls", None),
                getattr(observation, "form_controls_meta", None),
            ),
            _semantic_actions_block(getattr(observation, "semantic_tree", None)),
            grid_status_block(getattr(observation, "tables", None)),
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
    )
