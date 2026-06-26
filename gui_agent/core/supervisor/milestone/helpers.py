import re
from difflib import SequenceMatcher
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

from .schemas import (
    MilestonePrompts,
    _LoopFrameResult,
    _PlanResult,
    _SelectorResult,
    _SingleCheckResult,
)


def _default_milestone_prompts() -> MilestonePrompts:
    """Lazy iphone-prompts default: keeps every no-prompts caller (iphone factory,
    evals, tests, scripts) working unchanged while prompt bodies live as Markdown
    assets loaded by the iphone adapter. A platform that wants its own prompts
    injects them."""
    from gui_agent.adapters.iphone.supervisor.milestone.prompts import (
        IPHONE_MILESTONE_PROMPTS,
    )
    return IPHONE_MILESTONE_PROMPTS


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
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


def _prepare_prompt_png(png_bytes: bytes, image_resize: str = "retina") -> bytes:
    return prepare_prompt_png(png_bytes, image_resize=image_resize)


def _build_msgs(system_prompt: str, png_bytes: bytes, *, image_resize: str = "retina") -> list:
    return assemble_messages(system_prompt, png_bytes, image_resize=image_resize)


def _format_form_controls(form_controls: list[dict] | None) -> str:
    return format_form_controls_text(form_controls)


_OPEN_SELECT_RE = re.compile(r"点击|打开|展开|激活|下拉箭头|选项列表")


def _guard_native_select_plan(
    plan: _PlanResult,
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
) -> _PlanResult:
    """Rewrite native-select open-click plans into value-selection instructions."""
    instruction = plan.instruction or ""
    form_controls = getattr(observation, "form_controls", None)
    if not instruction or not form_controls or not _OPEN_SELECT_RE.search(instruction):
        return plan
    context = " ".join([
        milestone.name or "",
        milestone.description or "",
        milestone.success_condition or "",
        check.reason or "",
        check.summary or "",
        " ".join(check.issues or []),
        " ".join(check.missing_evidence or []),
    ])
    context_norm = _norm_text(context)
    instruction_norm = _norm_text(instruction)
    for item in form_controls:
        if not isinstance(item, dict) or item.get("kind") != "native_select":
            continue
        label = str(item.get("label") or item.get("name") or item.get("id") or "").strip()
        if not label or _norm_text(label) not in instruction_norm:
            continue
        options = item.get("options")
        if not isinstance(options, list):
            continue
        target = ""
        for opt in sorted((str(o).strip() for o in options if str(o).strip()), key=len, reverse=True):
            opt_norm = _norm_text(opt)
            if opt_norm and opt_norm in context_norm:
                target = opt
                break
        if not target:
            continue
        target_norm = _norm_text(target)
        if target_norm in instruction_norm and re.search(r"选择|选中|设置|设为", instruction):
            return plan
        return plan.model_copy(update={
            "instruction": f"在 {label} 下拉框选择 {target}",
            "summary": (
                f"{plan.summary}；DOM 表明 {label} 是 native select，直接选择目标值 {target}。"
                if plan.summary else
                f"DOM 表明 {label} 是 native select，直接选择目标值 {target}。"
            ),
        })
    return plan


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


_FIELD_SUFFIX_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{0,30}|[\u4e00-\u9fff]{1,12})\s*"
    r"(?:列|字段|输入框|筛选框|搜索框|下拉框|column|field|filter)",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"'「『“‘]([^\"'」』”’]{1,80})[\"'」』”’]")
_GENERIC_CONTROL_LABELS = {
    "undefined",
    "from",
    "to",
    "perpage",
    "of",
    "actions",
    "searchglobal",
    "selectall",
    "unselectall",
}


def _field_aliases(field: str) -> set[str]:
    norm = _norm_text(field)
    aliases = {norm} if norm else set()
    bilingual = {
        "产品": {"product", "产品"},
        "商品": {"product", "商品"},
        "昵称": {"nickname", "昵称"},
        "评论": {"review", "detail", "评论"},
        "状态": {"status", "状态"},
        "可见性": {"visibility", "visiblein", "可见性"},
        "类型": {"type", "类型"},
        "标题": {"title", "标题"},
    }
    aliases.update(bilingual.get(norm, set()))
    return {_norm_text(a) for a in aliases if _norm_text(a)}


def _extract_target_fields(milestone: Milestone) -> list[str]:
    text = " ".join([milestone.name or "", milestone.description or ""])
    fields: list[str] = []
    for raw in _FIELD_SUFFIX_RE.findall(text):
        field = str(raw or "").strip(" '\"「」『』")
        norm = _norm_text(field)
        if not norm or norm in {"当前", "目标", "搜索", "筛选", "关键词", "读取"}:
            continue
        if field not in fields:
            fields.append(field)
    return fields[:3]


def _control_label(item: dict) -> str:
    return str(item.get("label") or item.get("name") or item.get("id") or item.get("placeholder") or "").strip()


def _visible_field_controls(form_controls: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for item in form_controls or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if "input" not in kind and "select" not in kind and "textarea" not in kind:
            continue
        label = _control_label(item)
        label_norm = _norm_text(label)
        if not label_norm or label_norm in _GENERIC_CONTROL_LABELS:
            continue
        out.append(item)
    return out


def _instruction_mentions_control(instruction: str, controls: list[dict]) -> dict | None:
    inst_norm = _norm_text(instruction)
    for item in controls:
        label_norm = _norm_text(_control_label(item))
        if label_norm and len(label_norm) >= 3 and label_norm in inst_norm:
            return item
    return None


def _find_matching_control(field: str, controls: list[dict]) -> dict | None:
    aliases = _field_aliases(field)
    for item in controls:
        label_norm = _norm_text(_control_label(item))
        if label_norm and label_norm in aliases:
            return item
    return None


def _extract_input_value(plan: _PlanResult, milestone: Milestone) -> str:
    for text in (plan.instruction or "", milestone.name or "", milestone.description or ""):
        match = re.search(r"(?:输入|填写|搜索|筛选)(?!框)\s*[\"'「『“‘]?([^\"'」』”’，。;；]{1,80})", text)
        if match:
            return match.group(1).strip()
    for text in (plan.instruction or "", milestone.name or "", milestone.description or ""):
        for value in _QUOTED_RE.findall(text):
            value = value.strip()
            if value:
                return value
    return ""


def _extract_input_value_from_text(text: str) -> str:
    match = re.search(r"(?:输入|填写|搜索|筛选|设置为|设为)(?!框)\s*[\"'「『“‘]?([^\"'」』”’，。;；]{1,80})", text or "")
    if match:
        return match.group(1).strip()
    for value in _QUOTED_RE.findall(text or ""):
        value = value.strip()
        if value:
            return value
    return ""


_SUBMIT_STALE_INPUT_RE = re.compile(r"Search|Apply|提交|应用|按回车|回车|Enter|搜索按钮|筛选按钮", re.IGNORECASE)
_CLEAR_FILTER_RE = re.compile(r"Reset Filter|清除|重置|reset", re.IGNORECASE)


def _control_current_value(item: dict) -> str:
    return str(item.get("selected_text") or item.get("value") or "").strip()


def _extract_target_value_for_field(milestone: Milestone, field: str) -> str:
    aliases = _field_aliases(field)
    texts = [milestone.success_condition or "", milestone.name or "", milestone.description or ""]
    patterns = [
        r"(?:current|当前值)\s*=\s*[\"'「『“‘]([^\"'」』”’]{1,80})[\"'」』”’]",
        r"(?:改用|改为|改成|设置为|设为|输入|填写|使用关键词|关键词)\s*[\"'「『“‘]([^\"'」』”’]{1,80})[\"'」』”’]",
        r"(?:按|关键词)\s+([A-Za-z0-9][A-Za-z0-9 _/-]{0,60})\s*(?:筛选|搜索|检索|$)",
    ]
    for text in texts:
        norm_text = _norm_text(text)
        if aliases and not any(alias in norm_text for alias in aliases):
            continue
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            value = match.group(1).strip(" '\"「」『』“”‘’")
            if value and _norm_text(value) not in {"current", "目标", "关键词"}:
                return value
    return ""


def _guard_stale_text_filter_plan(
    plan: _PlanResult,
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
) -> _PlanResult:
    """Do not submit a text filter while its DOM current value is still stale."""
    instruction = plan.instruction or ""
    if not instruction or _CLEAR_FILTER_RE.search(instruction):
        return plan
    controls = _visible_field_controls(getattr(observation, "form_controls", None))
    if not controls:
        return plan
    targets = _extract_target_fields(milestone)
    if not targets:
        return plan
    inst_norm = _norm_text(instruction)
    plan_value = _extract_input_value_from_text(instruction)
    for field in targets:
        target_control = _find_matching_control(field, controls)
        if target_control is None:
            continue
        kind = str(target_control.get("kind") or "")
        if "input" not in kind and "textarea" not in kind:
            continue
        target_value = _extract_target_value_for_field(milestone, field)
        if not target_value:
            continue
        if _norm_text(_control_current_value(target_control)) == _norm_text(target_value):
            continue
        label = _control_label(target_control) or field
        label_mentioned = any(alias in inst_norm for alias in _field_aliases(label) | _field_aliases(field))
        typed_stale_value = (
            bool(plan_value)
            and _norm_text(plan_value) != _norm_text(target_value)
            and (label_mentioned or re.search(r"输入|填写|搜索|筛选|设置", instruction))
        )
        submits_stale_value = bool(_SUBMIT_STALE_INPUT_RE.search(instruction))
        if not typed_stale_value and not submits_stale_value:
            continue
        return plan.model_copy(update={
            "instruction": f"在 {label} 输入框输入 {target_value}",
            "summary": (
                f"子目标要求把「{field}」字段改为「{target_value}」，但 DOM 当前值仍是"
                f"「{_control_current_value(target_control)}」；先修正字段值，不能提交旧值。"
            ),
        })
    return plan


def _guard_named_field_substitution_plan(
    plan: _PlanResult,
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
) -> _PlanResult:
    """Do not substitute a different visible field for the field named by the milestone."""
    instruction = plan.instruction or ""
    if not instruction or not re.search(r"输入|填写|选择|设置|筛选|搜索", instruction):
        return plan
    controls = _visible_field_controls(getattr(observation, "form_controls", None))
    if not controls:
        return plan
    mentioned = _instruction_mentions_control(instruction, controls)
    if mentioned is None:
        return plan
    targets = _extract_target_fields(milestone)
    if not targets:
        return plan
    inst_norm = _norm_text(instruction)
    for field in targets:
        aliases = _field_aliases(field)
        if aliases and any(alias in inst_norm for alias in aliases):
            return plan
        target_control = _find_matching_control(field, controls)
        value = _extract_input_value(plan, milestone)
        if target_control is not None:
            label = _control_label(target_control) or field
            instruction = f"在 {label} 输入框输入 {value}" if value else f"操作 {label} 字段"
            return plan.model_copy(update={
                "instruction": instruction,
                "summary": (
                    f"子目标要求操作「{field}」字段，原计划指向了「{_control_label(mentioned)}」，"
                    "已改为目标字段。"
                ),
            })
        return plan.model_copy(update={
            "instruction": f"横向滚动表格或筛选行，显示「{field}」列的筛选框",
            "summary": (
                f"子目标要求操作「{field}」字段，当前可见 DOM 控件未提供该字段；"
                f"不能把值填入「{_control_label(mentioned)}」，需先定位目标列。"
            ),
            "direction": "right",
        })
    return plan


_ROUTE_TARGET_IDENTITY_MARKER = "必须对应子目标指定对象"
_ROUTE_TARGET_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,}")
_IDENTITY_ONLY_MISSING_RE = re.compile(
    r"身份|标识|对象|对应|指定|当前.*ID|评论\s*ID|记录\s*ID|URL|url|路由|路径|route|"
    r"不匹配|错误|wrong|mismatch|confirm",
    re.IGNORECASE,
)
_FIELD_VALUE_MISSING_RE = re.compile(
    r"产品名|评分|星级|昵称|字段|取值|product_name|rating_stars|customer_nickname|"
    r"summary|review|status|visibility",
    re.IGNORECASE,
)


def _route_target_matches(milestone: Milestone, observation: Observation) -> list[str]:
    if _ROUTE_TARGET_IDENTITY_MARKER not in (milestone.success_condition or ""):
        return []
    url = str(getattr(observation, "url", "") or "")
    if not url:
        return []
    text = f"{milestone.name}\n{milestone.success_condition}"
    matches: list[str] = []
    seen: set[str] = set()
    for token in _ROUTE_TARGET_TOKEN_RE.findall(text):
        if not any(ch.isdigit() for ch in token):
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", url):
            matches.append(token)
    return matches


def _missing_is_only_target_identity(missing_evidence: list[str]) -> bool:
    if not missing_evidence:
        return False
    missing_text = " ".join(str(item) for item in missing_evidence)
    if not _IDENTITY_ONLY_MISSING_RE.search(missing_text):
        return False
    field_value_hits = _FIELD_VALUE_MISSING_RE.search(missing_text)
    identity_hits = _IDENTITY_ONLY_MISSING_RE.search(missing_text)
    return bool(identity_hits) and not field_value_hits


def _apply_route_identity_checker_guard(
    result: _SingleCheckResult,
    milestone: Milestone,
    observation: Observation,
) -> _SingleCheckResult:
    """Accept route-backed target identity when only object identity is disputed."""
    if result.status == "done":
        return result
    matches = _route_target_matches(milestone, observation)
    if not matches or not _missing_is_only_target_identity(result.missing_evidence):
        return result
    page_identity = result.page_identity or ""
    claims_text = f"{result.reason} {result.summary} {page_identity}"
    if not re.search(r"详情|编辑|detail|edit|结果页|result", claims_text, re.IGNORECASE):
        return result
    target = "、".join(matches[:3])
    visible = list(result.visible_evidence or [])
    visible.append(f"URL/路由包含目标标识 {target}")
    return result.model_copy(update={
        "status": "done",
        "reason": (
            f"当前页为详情/结果页，且 URL/路由包含当前子目标的目标标识「{target}」；"
            "缺失证据只是在要求再次确认对象身份，已由机器路由证据满足。"
        ),
        "missing_evidence": [],
        "visible_evidence": visible,
        "stuck_reason": "",
    })


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


_CLICKED_OPTION_RE = re.compile(
    r"点击.{0,20}(?:候选|选项|条目).{0,8}[「『“\"'`]([^」』”\"'`]+)[」』”\"'`]"
)
_CLICKED_OPTION_DETAIL_RE = re.compile(
    r"点击(?P<context>.{0,40}?)(?:候选|选项|条目).{0,12}[「『“\"'`](?P<value>[^」』”\"'`]+)[」』”\"'`]"
)
_CLICK_DROPDOWN_FIELD_RE = re.compile(
    r"点击(?P<context>.{0,40}?)(?:下拉框|选择框|搜索框|输入框|框).{0,30}(?:候选|选项|列表|展开|确认|选择|重选)"
)
_BUTTON_CLAIM_RE = re.compile(r"(?:确认|提交|执行)按钮|点击.{0,4}(?:确认|提交|执行)")
_QUOTED_VALUE_RE = re.compile(r"[「『“\"'`]([^」』”\"'`]{2,80})[」』”\"'`]")


def _quoted_values(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTED_VALUE_RE.finditer(text or "") if m.group(1).strip()]


def _normalize_control_context(text: str) -> str:
    quoted = _quoted_values(text or "")
    context = quoted[-1] if quoted else (text or "")
    context = re.sub(r"[「『“\"'`].*$", "", context).strip()
    context = re.sub(r"(当前|该|这个|此|下拉|搜索框|输入框|框|列表|中的?|里的?|精确|匹配)", "", context)
    context = re.sub(r"\s+", "", context)
    return context


_TRAILING_FIELD_RE = re.compile(r"(?:以|来)(?:选择|确认|选定|设置|完成)(?P<tail>[^，。;；「『]{2,24})")


def _trailing_field_context(instruction: str, start: int) -> str:
    """「点击候选…『X』以选择<字段>」式指令把字段名放在尾部——前置 context 缺失时从尾部提取，
    让同字段的重复点击能被守卫识别（多选项重点击可能取消选中，回归 20260612_205558）。
    纯动词尾巴（「以完成选定」）不算字段名。"""
    m = _TRAILING_FIELD_RE.search(instruction[start:] if start < len(instruction) else "")
    if not m:
        return ""
    context = _normalize_control_context(m.group("tail"))
    return "" if context in ("", "选定", "选择", "确认", "操作") else context


def _clicked_option_detail(instruction: str) -> Optional[tuple[str, str]]:
    match = _CLICKED_OPTION_DETAIL_RE.search(instruction or "")
    if not match:
        fallback = _CLICKED_OPTION_RE.search(instruction or "")
        if not fallback:
            return None
        return fallback.group(1).strip(), _trailing_field_context(instruction, fallback.end())
    value = match.group("value").strip()
    context = _normalize_control_context(match.group("context"))
    if not context:
        context = _trailing_field_context(instruction, match.end())
    return value, context


def _typed_value_in_context(instruction: str, value: str, context: str) -> bool:
    if "输入" not in (instruction or "") or value not in instruction:
        return False
    prefix = instruction.split("输入", 1)[0]
    return _normalize_control_context(prefix) == context


def _clicked_dropdown_field_context(instruction: str) -> str:
    match = _CLICK_DROPDOWN_FIELD_RE.search(instruction or "")
    if not match:
        return ""
    return _normalize_control_context(match.group("context"))


def _guard_exact_dropdown_target(plan: _PlanResult, milestone: Milestone) -> _PlanResult:
    """Repair a narrow class of searchable-dropdown hallucinations.

    Vision models sometimes click the closest visible option even when the milestone's
    success condition names a different exact value. If the planned clicked option is
    similar to, but not exactly, a quoted target in the success condition, prefer typing
    the target to filter the dropdown. This leaves normal candidate clicks untouched.
    """
    match = _CLICKED_OPTION_RE.search(plan.instruction or "")
    if not match:
        return plan
    clicked = match.group(1).strip()
    targets = _quoted_values(milestone.success_condition)
    if not targets or clicked in targets:
        return plan

    best = max(targets, key=lambda t: SequenceMatcher(None, clicked, t).ratio())
    if SequenceMatcher(None, clicked, best).ratio() < 0.55:
        return plan

    plan.instruction = f"在当前下拉搜索框中输入「{best}」"
    plan.summary = (
        f"{plan.summary}；候选「{clicked}」与目标「{best}」不完全一致，改为先输入目标全文过滤"
    )
    return plan


def _repeated_candidate_click(
    instruction: str, milestone_id: Optional[str], history: list[PolicyTurn]
) -> Optional[str]:
    """Detect the dropdown re-selection loop signature (20260612_184401).

    A candidate click closes the list; the box keeps the chosen text (still with the
    search icon), which the checker sometimes misreads as "not selected yet". The planner
    then obeys missing_evidence and clicks again — REOPENING the list and making the
    misjudgment true for another round (4 wasted turns live; prompt rules alone did not
    break the lock: 0/10 with the checker text asserting the list is open).

    Signature: the planned instruction clicks candidate X while an already-EXECUTED turn
    of the SAME milestone clicked candidate X, with no re-typing of X into the dropdown
    in between (re-typing reopens the list, making a follow-up click legitimate).
    Returns X when the signature matches, else None.
    """
    current = _clicked_option_detail(instruction)
    if not current:
        return None
    clicked, current_context = current
    if not current_context:
        return None
    last_click_idx = None
    for i, t in enumerate(history):
        if not (t.executed and t.supervisor and t.supervisor.milestone_id == milestone_id):
            continue
        detail = _clicked_option_detail(t.supervisor.instruction or "")
        if detail and detail[0] == clicked and detail[1] == current_context:
            last_click_idx = i
    if last_click_idx is None:
        return None
    for t in history[last_click_idx + 1:]:
        if not (t.executed and t.supervisor and t.supervisor.milestone_id == milestone_id):
            continue
        instr = t.supervisor.instruction or ""
        if _typed_value_in_context(instr, clicked, current_context):
            return None
    return clicked


def _reopens_selected_dropdown(
    instruction: str, milestone_id: Optional[str], history: list[PolicyTurn]
) -> Optional[tuple[str, str]]:
    """Detect attempts to reopen a dropdown field after its candidate was selected.

    This is the sibling of `_repeated_candidate_click`: some plans do not click the
    same candidate again directly; they first click the filled input/search box to
    reopen the candidate list. The guard only fires when the field context matches a
    prior executed candidate click in the same milestone.
    """
    context = _clicked_dropdown_field_context(instruction)
    if not context:
        return None
    last_click_idx = None
    last_value = ""
    for i, t in enumerate(history):
        if not (t.executed and t.supervisor and t.supervisor.milestone_id == milestone_id):
            continue
        detail = _clicked_option_detail(t.supervisor.instruction or "")
        if detail and detail[1] == context:
            last_click_idx = i
            last_value = detail[0]
    if last_click_idx is None:
        return None
    for t in history[last_click_idx + 1:]:
        if not (t.executed and t.supervisor and t.supervisor.milestone_id == milestone_id):
            continue
        if _typed_value_in_context(t.supervisor.instruction or "", last_value, context):
            return None
    return context, last_value


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
) -> _SingleCheckResult:
    """Run the single-step milestone checker. Used by both production and evals.

    Pure verification: knowledge-section selection lives in :func:`run_selector` (a separate
    cached micro-decision), so the checker prompt carries no section manifest.

    ``check_knowledge``（_check.md）= 动态验收知识：该 app 界面的实际显示形态/完成标志
    （列渲染短形式、成功提示样式、错误 toast 语义等）。静态 checker prompt 只保留跨 app
    通用验收原则；app 特定事实按 app 从这里注入，避免静态规则膨胀与过拟合。"""
    if prompts is None:
        prompts = _default_milestone_prompts()
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
            extra_instruction_block(extra, source="checker_guard"),
            page_title_block(title),
            acceptance_items_block(accept_items),
            knowledge_block("check_rules", check_knowledge),
            checker_kind_rules_block(kind_section),
        ],
        human_blocks=[
            active_filters_block(getattr(observation, "form_controls", None)),
            form_controls_block(getattr(observation, "form_controls", None)),
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
    result = _apply_route_identity_checker_guard(result, milestone, observation)

    claims_text = f"{result.reason} {result.summary} " + " ".join(result.missing_evidence)
    if result.status == "in_progress" and not _is_retry and _BUTTON_CLAIM_RE.search(claims_text):
        print("  [SingleCheck] 提到了确认/提交/执行按钮，重核按钮是否真实可见...")
        result = run_checker(
            milestone, observation, history,
            app_name=app_name, task_type=task_type, constraints=constraints,
            extra=(
                "你刚才提到了确认/提交/执行按钮。请重新核对当前截图：只有按钮在截图中真实可见，"
                "且当前子目标明确要求点击它时，才可以把按钮写进 reason/summary/missing_evidence。"
                "如果当前截图没有这类按钮，不要提按钮，也不要要求点击按钮；只根据目标值、目标状态、"
                "成功提示或结果区域是否可见来判断。"
            ),
            _is_retry=True,
            prompts=prompts,
            check_knowledge=check_knowledge,
            context_reports=context_reports,
        )
        _strip_progress_evidence(result)
        result = _apply_route_identity_checker_guard(result, milestone, observation)

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
        result = _apply_route_identity_checker_guard(result, milestone, observation)
    if result.status == "done" and _still_invalid(result):
        return _SingleCheckResult(
            status="stuck",
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
) -> _PlanResult:
    """Run the step planner. Used by both production and evals."""
    if prompts is None:
        prompts = _default_milestone_prompts()
    if constraints is None:
        constraints = []
    if milestone.retry_count > 0 and not extra:
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
                f"⚠️ 该子目标已尝试 {milestone.retry_count} 次。以下操作在本子目标中已经尝试过但尚未达成验收条件，"
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
            form_controls_block(getattr(observation, "form_controls", None)),
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
    plan = _guard_native_select_plan(plan, milestone, check, observation)
    plan = _guard_named_field_substitution_plan(plan, milestone, check, observation)
    plan = _guard_stale_text_filter_plan(plan, milestone, check, observation)
    plan = _guard_exact_dropdown_target(plan, milestone)
    # Selection re-entry loop breaker: clicking an already-clicked candidate again
    # (without re-typing/searching in between) often means the checker misread an
    # already-selected field as incomplete. Re-invoke once with corrective context;
    # `extra` doubles as the recursion brake (a corrective call won't re-fire).
    if not extra:
        repeated = _repeated_candidate_click(plan.instruction, milestone.id, history)
        if repeated:
            print(f"  [Planner] 候选「{repeated}」此前已点击执行过，疑似重选已完成的下拉框，纠偏重试...")
            return run_planner(
                milestone, check, observation, history,
                constraints=constraints,
                extra=(
                    f"你计划点击的候选/选项「{repeated}」在之前轮次已经点击执行过。"
                    "请重新只看当前截图：只有当前仍明确显示可选候选列表、且该候选尚未呈现已选状态时，才再次点击。"
                    "若对应字段/控件已经显示目标值、已选标记或完整选择结果，该字段通常已完成；"
                    "不要为了确认已选项而重新打开或重复点击它，直接处理其他真正未完成字段。"
                    "若该控件允许多选，重复点击已选项可能会取消选中；看到已选标记时应继续下一步，"
                    "除非任务明确要求取消该项。"
                ),
                app_knowledge=app_knowledge,
                elements_knowledge=elements_knowledge,
                prompts=prompts,
                context_reports=context_reports,
            )
        reopened = _reopens_selected_dropdown(plan.instruction, milestone.id, history)
        if reopened:
            context, value = reopened
            print(f"  [Planner] 字段「{context}」此前已选择候选「{value}」，疑似重开已完成的下拉框，纠偏重试...")
            return run_planner(
                milestone, check, observation, history,
                constraints=constraints,
                extra=(
                    f"你计划点击字段/控件「{context}」以展开或确认候选，但它此前已经点击候选「{value}」执行过。"
                    "请重新只看当前截图：若该控件附近没有候选列表、且控件已显示目标值或已选状态，它通常已经完成。"
                    "不要为了确认已选项而重新打开该控件；这可能把已完成字段重新置为编辑态。"
                    "请直接处理当前截图中其他真正未完成的字段。"
                ),
                app_knowledge=app_knowledge,
                elements_knowledge=elements_knowledge,
                prompts=prompts,
                context_reports=context_reports,
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
        prompts = _default_milestone_prompts()
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
