import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    acceptance_items_block,
    app_identity_block,
    checker_kind_rules_block,
    active_filters_block,
    applied_filter_state_block,
    filter_residual_block,
    checker_result_block,
    constraints_block,
    extra_instruction_block,
    form_controls_block,
    format_form_controls_text,
    format_history_text,
    grid_status_block,
    history_block,
    knowledge_block,
    milestone_block,
    browser_page_block,
    page_title_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages, prepare_prompt_png
from gui_agent.core.schemas import (
    Milestone,
    Observation,
    PolicyTurn,
    split_acceptance_items,
)
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .observation_state import (
    RuntimeFilterIntent,
    filter_residual_labels,
)

from .schemas import (
    MilestonePrompts,
    _LoopFrameResult,
    _PlanResult,
    _SelectorResult,
    _SingleCheckResult,
)


load_dotenv()


def _format_history(history: list[PolicyTurn]) -> str:
    return format_history_text(history)


# `@<path>` file references inside the goal text (e.g. 「按 @tmp_scripts/sim.json 的配置新建」).
# A token runs until whitespace / CJK punctuation / quotes; CJK chars themselves are allowed
# (filenames like 交管测试_1楼.json). Disambiguation from plain @-mentions is by existence:
# the resolver tries the token, then progressively trims trailing chars (handles prose glued
# to the path, e.g. 「@sim.json的配置」), and gives up quietly if nothing on disk matches.
# Chars that terminate a path token: CJK/ASCII punctuation, brackets, straight & curly quotes
# (curly via escapes — literal quote chars inside the pattern string are too error-prone).
_TOKEN_BREAK = "，。！？；：、()（）【】《》<>[]" + "\"'" + "“”‘’"
_FILE_REF_RE = re.compile(rf"@([^\s@{re.escape(_TOKEN_BREAK)}]+)")
_FILE_REF_MAX_CHARS = 50_000
# Aggregate cap across ALL @file refs in one goal. file_reference_block is a `required` context
# block (never dropped by the budgeter — it carries load-bearing task data), so without a total
# cap several large @files would push the required portion past the context ceiling and defeat
# the hard cap. Bounding the total here keeps the required portion deterministically small enough
# that the budgeter's drop-droppable pass can always bring the whole context under budget.
_FILE_REF_TOTAL_MAX_CHARS = 60_000


def resolve_file_refs(goal: str, base: Optional[Path] = None) -> str:
    """Read the files referenced by ``@<path>`` tokens in the goal and return ONE labeled
    prompt section with their contents ("" when the goal has no resolvable refs).

    This is how config-heavy tasks get their field values in: a dozen form fields live in a
    file, the spoken goal just points at it. Resolved at DECOMPOSE time (the only consumer of
    the full goal), so both runner and chat get it with no CLI plumbing, and the router never
    paraphrases file contents — it only ever sees the @token."""
    base = base or Path.cwd()
    sections: list[str] = []
    seen: set[str] = set()
    total_chars = 0          # running total of injected TEXT content (binary path stubs excluded)
    omitted: list[str] = []  # @refs skipped/truncated once the aggregate cap is hit
    for raw in _FILE_REF_RE.findall(goal):
        cand = raw.rstrip(".,;:!?")  # plain trailing ASCII punctuation is prose, not path
        path: Optional[Path] = None
        while cand:
            p = Path(cand).expanduser()
            if not p.is_absolute():
                p = base / p
            if p.is_file():
                path = p
                break
            cand = cand[:-1]
        if path is None:
            print(f"  [FileRef] @{raw} 未解析到文件，按普通文本处理")
            continue
        if str(path) in seen:
            continue
        seen.add(str(path))
        # An @<path> ref can be a CONFIG file (inject its field values) OR an upload TARGET (a
        # binary the executor uploads by path — no content to inject). Sniff the head for a NUL
        # byte: binary → skip quietly (don't alarm, and don't read a large binary fully just to
        # fail decode). Text that isn't valid UTF-8 falls through to the decode-error skip.
        try:
            with path.open("rb") as _fh:
                _head = _fh.read(8192)
        except OSError as exc:
            print(f"  [FileRef] 读取失败 {path}：{exc}")
            continue
        # Binary (NUL byte, or non-UTF-8): an upload TARGET, not config. Don't inject content,
        # but DO surface the PATH so the planner can hand it to the upload action (the executor
        # uploads by path; without this the file path is lost and the agent can't upload).
        def _binary_section() -> str:
            print(f"  [FileRef] @{cand} 是二进制文件，作为上传/导入目标路径处理（不注入内容）")
            return f"### @{cand}\n二进制文件（上传/导入的目标）。本地完整路径，上传时原样使用：\n{path}"
        if b"\x00" in _head:
            sections.append(_binary_section())
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            sections.append(_binary_section())
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
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                      timeout=cfg.timeout_s, max_retries=cfg.max_retries)


def _prepare_prompt_png(png_bytes: bytes, image_resize: str = "retina") -> bytes:
    return prepare_prompt_png(png_bytes, image_resize=image_resize)


def _build_msgs(system_prompt: str, png_bytes: bytes, *, image_resize: str = "retina") -> list:
    return assemble_messages(system_prompt, png_bytes, image_resize=image_resize)


def _format_form_controls(form_controls: list[dict] | None) -> str:
    return format_form_controls_text(form_controls)


def _normalize_picker_plan_direction(plan: _PlanResult) -> _PlanResult:
    """Make structured picker direction consistent with current/target values.

    The LLM sometimes chooses the right picker column and values but flips the
    value direction wording. The executor relies on direction for scroll polarity,
    so normalize known numeric picker columns here; policy.py also recomputes the
    step count later.
    """
    column = (getattr(plan, "drag_column", None) or "").strip().lower()
    cur = getattr(plan, "drag_current_value", None)
    tgt = getattr(plan, "drag_target_value", None)
    if not column or cur is None or tgt is None or cur == tgt:
        return plan
    if column == "minute":
        forward = (tgt - cur) % 60
        backward = (cur - tgt) % 60
        plan.direction = "increase" if forward <= backward else "decrease"
    elif column == "hour":
        forward = (tgt - cur) % 12
        backward = (cur - tgt) % 12
        plan.direction = "increase" if forward <= backward else "decrease"
    else:
        plan.direction = "increase" if tgt > cur else "decrease"
    return plan


def run_checker(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    app_name: str = "",
    task_type: str = "action",
    constraints: Optional[list[str]] = None,
    extra: str = "",
    _is_retry: bool = False,
    prompts: Optional[MilestonePrompts] = None,
    check_knowledge: str = "",
    context_reports: list[dict] | None = None,
    state_trace_text: str = "",
    last_action_response: str = "",
    initial_filters: dict[str, str] | None = None,
    runtime_filter: RuntimeFilterIntent | None = None,
) -> _SingleCheckResult:
    """Run the single-step milestone checker. Used by both production and evals.

    Pure verification: knowledge-section selection lives in :func:`run_selector` (a separate
    cached micro-decision), so the checker prompt carries no section manifest.

    ``check_knowledge``（_check.md）= 动态验收知识：该 app 界面的实际显示形态/完成标志
    （列渲染短形式、成功提示样式、错误 toast 语义等）。静态 checker prompt 只保留跨 app
    通用验收原则；app 特定事实按 app 从这里注入，避免静态规则膨胀与过拟合。"""
    if prompts is None:
        prompts = MilestonePrompts.neutral()
    if constraints is None:
        constraints = []
    kind_section = prompts.check_kind_sections.get(milestone.kind, prompts.check_section_default)
    # 连续调值类（picker 收敛）在 kind 段之上叠加专用段：当前值以滚轮中心带为准、强制输出
    # 当前值/目标值。这是连续操作进展传感器的基础——避免把已推进的拖动误读为"没动"。
    if milestone.is_converge:
        kind_section = kind_section + prompts.check_section_converge
    prompt = prompts.single_checker

    # Inject the tab TITLE (the viewport-language page name the screenshot doesn't show) as
    # an auxiliary identity signal, so the checker does not need to infer it from pixels. The URL is deliberately NOT
    # injected — a machine URL adds little discriminating value as LLM text and costs tokens; it
    # is consumed programmatically instead (url-change = navigation, in the supervisor). Only
    # browser perception supplies a title; iphone/android leave it None and nothing is injected.
    title = getattr(observation, "title", None)
    # Per-item checklist: enumerate the acceptance sub-conditions and ask the checker to judge each
    # independently (met + evidence) into item_verdicts. Drives the checklist's per-item status; the
    # overall `status` (which gates advance/replan) is unchanged.
    accept_items = split_acceptance_items(milestone.success_condition, milestone.name)
    msgs = assemble_messages(
        prompt, observation,
        system_blocks=[
            app_identity_block(app_name),
            milestone_block(milestone, task_type=task_type),
            constraints_block(constraints),
            history_block(history, current_milestone_id=milestone.id),
            (ContextBlock(
                id="runtime.state_trace", budget="high", source_type="runtime_state",
                source="state_trace", ttl="turn", priority=28,
                content=("## 任务进展轨迹（状态→决策，越下越新）\n"
                         "标⚠️重复=同一页面上重复了之前做过的同一决策(在打转，不是推进)。"
                         "据此判断任务是在推进(不断到达新状态)还是在少数状态里打转。\n" + state_trace_text),
            ) if state_trace_text.strip() else None),
            (ContextBlock(
                id="runtime.last_action_response", budget="high", source_type="rt.execution",
                source="progress_monitor", ttl="turn", priority=29,
                # Deterministic post-action response (url/dom delta): authoritative for whether the
                # last action was dispatched and whether it produced a navigation/DOM change —
                # NOT for whether the business RESULT is correct. freshness=post_action: it
                # describes the just-executed action's execution/response signals.
                authoritative_for=(
                    "action.execution.dispatched",
                    "action.execution.not_dispatched",
                    "action.response.url_changed",
                    "action.response.dom_changed",
                    "action.response.none_observed",
                ),
                not_authoritative_for=("business.result", "target.state"),
                freshness="post_action",
                coverage="complete",
                content=last_action_response,
            ) if last_action_response.strip() else None),
            extra_instruction_block(extra, source="checker_guard"),
            page_title_block(title),
            acceptance_items_block(accept_items),
            knowledge_block("check_rules", check_knowledge),
            checker_kind_rules_block(kind_section),
        ],
        human_blocks=[
            browser_page_block(
                getattr(observation, "url", None),
                None,
            ),
            active_filters_block(getattr(observation, "form_controls", None)),
            applied_filter_state_block(
                getattr(observation, "applied_filters", None),
                getattr(observation, "applied_filter_meta", None),
                initial_filters=initial_filters,
            ),
            filter_residual_block(
                filter_residual_labels(
                    getattr(observation, "applied_filters", None),
                    milestone,
                    runtime_filter,
                ),
                getattr(observation, "applied_filters", None),
            ),
            form_controls_block(
                getattr(observation, "form_controls", None),
                getattr(observation, "form_controls_meta", None),
            ),
            grid_status_block(getattr(observation, "tables", None)),
        ],
        image_resize=prompts.image_resize,
        label="checker",
        context_reports=context_reports,
    )
    result = invoke_structured(
        _make_llm(),
        msgs,
        _SingleCheckResult,
        trace_sink=context_reports,
        trace_label="checker",
    )

    def _strip_progress_evidence(r: _SingleCheckResult) -> None:
        # 连续调值类(is_converge)的 checker section 要求把「当前值=/目标值=」写进 missing_evidence
        # 作进展传感器。这些在 done(值已达标)时是冗余的、不是真正缺失的验收证据；若留着会被下面
        # 的 done 守卫当成「证据不足」而每次 done 都误触发一次重试(实测频繁,~1s/次)。done 时剔除它们。
        if milestone.is_converge and r.status == "done" and r.missing_evidence:
            r.missing_evidence = [
                e for e in r.missing_evidence if "当前值" not in e and "目标值" not in e
            ]

    _strip_progress_evidence(result)

    # Validate a done verdict in two stages, because the retry and the force-stuck
    # play different roles:
    #
    # _retry_worthy — triggers exactly ONE re-verification. For non-navigation kinds
    # an empty visible_evidence is included here: a *wrong* done on a pre-action
    # screen (e.g. send button visible but not yet sent) typically can't cite real
    # evidence, and forcing a re-check makes the model recant to in_progress
    # (measured: send-screen wrong-done 4/10 → 0/10). This is the actual
    # hallucination catcher — the recant on re-verify, not the force-stuck.
    #
    # _still_invalid — after the retry, only HARD contradictions force stuck:
    # missing_evidence non-empty (self-contradiction) or a too-thin reason. We do
    # NOT force stuck on empty visible_evidence here: the prompt declares that field
    # optional, so a legitimate done that survives re-verification (date really IS
    # set, page identity really IS right) but cited its evidence in reason/summary
    # rather than the optional array must be accepted — else we kill a correct done
    # and lock the subgoal (observed: 20260530_094941 turn7 → cascaded task failure).
    def _retry_worthy(r: _SingleCheckResult) -> bool:
        if r.missing_evidence or len((r.reason or "").strip()) < 10:
            return True
        # 连续调值类(converge)的 done 由「滚轮中间行值 == success_condition 目标」直接验证，
        # 证据是客观可读的(写在 reason 里)，不需要 visible_evidence 数组——豁免该条，否则每个
        # converge done 都会因 visible_evidence 空而白白重试一次。
        if milestone.is_converge:
            return False
        return milestone.kind != "navigation" and not r.visible_evidence

    def _still_invalid(r: _SingleCheckResult) -> bool:
        return bool(r.missing_evidence) or len((r.reason or "").strip()) < 10

    if not _is_retry and result.status == "done" and _retry_worthy(result):
        # Retry exactly once. The retry passes _is_retry=True so it skips this
        # block — without that the recursion would re-trigger and retry unboundedly
        # (observed up to 4×). Capped at 2 LLM calls total.
        print("  [SingleCheck] done 证据不足，重试...")
        result = run_checker(
            milestone, observation, history,
            app_name=app_name, task_type=task_type, constraints=constraints,
            extra=(
                "你刚才判定为 done，请重新核对截图确认验收条件是否*已经发生*（而非仅具备执行条件）。"
                "若确实满足，请在 reason 里写清你看到的具体依据（标题文字、高亮选中项、已设定的值、"
                "结果提示），并清空 missing_evidence；若截图只显示「可以执行」但结果尚未出现，改判 in_progress。"
            ),
            _is_retry=True,
            prompts=prompts,
            check_knowledge=check_knowledge,
            context_reports=context_reports,
        )
        _strip_progress_evidence(result)
    if result.status == "done" and _still_invalid(result):
        return _SingleCheckResult(
            status="stuck",
            effect_status="unverified",
            reason="当前验收结论缺少可见依据或存在自相矛盾",
            stuck_reason="当前页面仍缺少足够的验收依据，需要继续确认可见状态",
            summary=result.summary,
        )
    return result


_SELECTOR_PROMPT = load_prompt_text("task.milestone.knowledge_selector")


def run_selector(
    goal: str,
    milestone: Milestone,
    page_identity: str,
    manifest: str,
    *,
    prompts: Optional[MilestonePrompts] = None,
    context_reports: list[dict] | None = None,
) -> _SelectorResult:
    """KnowledgeSelector: a dedicated text-only micro-decision picking which knowledge
    sections the upcoming planner should read.

    Deliberately NOT folded into the checker (it verifies; selection diluted it and its
    paraphrases broke fuzzy name-matching) and NOT vision: page identity comes as text from
    the checker, which keeps this call small. The policy caches the result per
    (milestone, page_identity), so it only fires on page/milestone changes."""
    template = (prompts.selector if prompts and prompts.selector else _SELECTOR_PROMPT)
    prompt = template.format(
        goal=goal,
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        success_condition=milestone.success_condition,
        page_identity=page_identity or "（未识别）",
        manifest=manifest,
    )
    decision_text = "请选择章节并输出 section_ids。"
    if context_reports is not None:
        context_reports.append({
            "kind": "prompt_snapshot",
            "label": "selector",
            "roles": [
                {
                    "role": "system",
                    "parts": [
                        {
                            "label": "task_prompt",
                            "source_type": "prompt_asset",
                            "source": "task.milestone.knowledge_selector",
                            "type": "text",
                            "text": prompt,
                            "chars": len(prompt),
                        },
                    ],
                },
                {
                    "role": "human",
                    "parts": [
                        {
                            "label": "decision_text",
                            "source_type": "runtime_state",
                            "source": "run_selector",
                            "type": "text",
                            "text": decision_text,
                            "chars": len(decision_text),
                        },
                    ],
                },
            ],
        })
    msgs = [
        SystemMessage(content=prompt),
        HumanMessage(content=decision_text),
    ]
    return invoke_structured(
        _make_llm(),
        msgs,
        _SelectorResult,
        trace_sink=context_reports,
        trace_label="selector",
    )


def run_planner(
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    constraints: Optional[list[str]] = None,
    extra: str = "",
    app_knowledge: Optional[str] = None,
    elements_knowledge: Optional[str] = None,
    prompts: Optional[MilestonePrompts] = None,
    context_reports: list[dict] | None = None,
    initial_filters: dict[str, str] | None = None,
    runtime_filter: RuntimeFilterIntent | None = None,
) -> _PlanResult:
    """Run the step planner. Used by both production and evals."""
    if prompts is None:
        prompts = MilestonePrompts.neutral()
    if constraints is None:
        constraints = []
    _retry = int(getattr(milestone, "retry_count", 0) or 0)
    if _retry > 0 and not extra:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        # Collect dead-end paths from ALL milestones (replan diagnoses)
        dead_ends: list[str] = []
        for t in history:
            if t.replan and t.replan.get("diagnosis"):
                dead_ends.append(t.replan["diagnosis"])
        if tried:
            tried_lines = "\n".join(f"  - 「{i}」" for i in tried)
            extra = (
                f"⚠️ 该子目标已尝试 {_retry} 次。以下操作在本子目标中已经尝试过但尚未达成验收条件，"
                f"请优先选择当前截图中不同的可见入口或下一步元素：\n{tried_lines}"
            )
        if dead_ends:
            dedup = list(dict.fromkeys(dead_ends))
            dead_end_lines = "\n".join(f"  - {d}" for d in dedup)
            extra_text = (
                "⚠️ 以下路径之前未达成目标，除非当前截图出现新的明确证据，否则不要重复：\n"
                f"{dead_end_lines}"
            )
            extra = f"{extra}\n\n{extra_text}" if extra else extra_text
    prompt = prompts.plan
    msgs = assemble_messages(
        prompt, observation,
        system_blocks=[
            milestone_block(milestone),
            constraints_block(constraints),
            checker_result_block(check),
            history_block(history, current_milestone_id=milestone.id),
            extra_instruction_block(extra, source="planner_guard"),
        ],
        human_blocks=[
            active_filters_block(getattr(observation, "form_controls", None)),
            applied_filter_state_block(
                getattr(observation, "applied_filters", None),
                getattr(observation, "applied_filter_meta", None),
                initial_filters=initial_filters,
            ),
            filter_residual_block(
                filter_residual_labels(
                    getattr(observation, "applied_filters", None),
                    milestone,
                    runtime_filter,
                ),
                getattr(observation, "applied_filters", None),
            ),
            form_controls_block(
                getattr(observation, "form_controls", None),
                getattr(observation, "form_controls_meta", None),
            ),
            knowledge_block("app_navigation", app_knowledge),
            knowledge_block("page_elements", elements_knowledge),
        ],
        image_resize=prompts.image_resize,
        label="planner",
        context_reports=context_reports,
    )
    plan_schema = prompts.plan_result_schema or _PlanResult
    plan = invoke_structured(
        _make_llm(),
        msgs,
        plan_schema,
        trace_sink=context_reports,
        trace_label="planner",
    )
    return _normalize_picker_plan_direction(plan)


def run_loop_check(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    constraints: Optional[list[str]] = None,
    prompts: Optional[MilestonePrompts] = None,
    context_reports: list[dict] | None = None,
) -> _LoopFrameResult:
    """Run the per-frame scroll_until_boundary assessment. Used by both production and evals."""
    if prompts is None:
        prompts = MilestonePrompts.neutral()
    prompt = prompts.loop_frame
    return invoke_structured(
        _make_llm(),
        assemble_messages(
            prompt,
            observation,
            system_blocks=[
                milestone_block(
                    milestone,
                    scroll_stop_condition=milestone.scroll_stop_condition or "滚动至列表物理底部时停止",
                ),
                constraints_block(constraints or []),
                history_block(history, current_milestone_id=milestone.id),
            ],
            image_resize=prompts.image_resize,
            label="loop_check",
            context_reports=context_reports,
        ),
        _LoopFrameResult,
        trace_sink=context_reports,
        trace_label="loop_check",
    )
