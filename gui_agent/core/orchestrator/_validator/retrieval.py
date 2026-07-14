"""Retrieval retry validation rules for orchestrator programs."""

from __future__ import annotations

import re

from ..program import ForEach, If, Run, RunLike, Stmt
from .issue import IssueList

_RETRIEVAL_FIELD_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{0,30}|[\u4e00-\u9fff]{1,12})\s*"
    # `name`/`名` act as field designators ("Customer name 列" / "产品名") — but NOT when another
    # designator immediately follows: in "面板的 Name 字段" the word "Name" IS the field itself,
    # and letting it match as designator captures the possessive prose "面板的" instead.
    r"(?:列|字段|输入框|筛选框|搜索框|下拉框|column|field|filter"
    r"|name(?!\s*(?:字段|列|column|field))|名(?!\s*(?:字段|列)))",
    re.IGNORECASE,
)

_RETRIEVAL_FIELD_EQ_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{0,30}|[\u4e00-\u9fff]{1,12})\s*(?:=|:|：)",
    re.IGNORECASE,
)

_RETRIEVAL_RETRY_CUE_RE = re.compile(
    r"0\s*条|无结果|空结果|关键词|模糊|放宽|包含|contains|fuzzy|broaden|partial",
    re.IGNORECASE,
)

_RETRIEVAL_ACTION_CUE_RE = re.compile(
    r"搜索|筛选|检索|查找|重筛|重搜|filter|search|query",
    re.IGNORECASE,
)
_SAME_RETRIEVAL_TARGET_RE = re.compile(
    # Allow the concrete field name between 同一/same and the designator:
    # "同一 Name 字段", "同一名称字段", "同一产品名称字段", "same Status column".
    # Chinese field labels must be accepted — live decomposer often writes 「同一名称字段」
    # after 「在产品名称字段…」; the previous ASCII-only gap rejected that as a field drop.
    r"同一(?:个)?\s*(?:[A-Za-z0-9 _./-]|[\u4e00-\u9fff]){0,20}?"
    r"(?:字段|列|输入框|筛选框|搜索框|下拉框)|"
    r"same\s+(?:[A-Za-z0-9 _./-]+\s+)?(?:field|column|input|filter|search\s*box|dropdown)",
    re.IGNORECASE,
)

_RETRIEVAL_FIELD_STOPWORDS = {
    "当前", "目标", "搜索", "筛选", "关键", "关键词", "记录", "列表", "结果", "相关",
    "使用", "输入", "提交", "页面", "顶部", "底部", "上方", "下方", "左侧", "右侧",
    "同一", "同一个", "the", "same", "target", "filter", "search",
}

# Known entity field roots. The greedy Chinese capture before 字段/列 can swallow preceding prose
# (e.g. "清除精确值后在产品字段" extracts "清除精确值后在产品" instead of "产品"); reduce to the root.
_KNOWN_FIELD_ROOTS = (
    "产品", "商品", "product", "客户", "customer", "昵称", "nickname",
    "标题", "title", "状态", "status", "订单", "order", "评论", "review",
)

_RETRIEVAL_FIELD_PREFIX_RE = re.compile(
    r"^(?:先用|使用|用|在|按|以|将|把|从|当前|目标|same|target|in|on|by|using)\s*",
    re.IGNORECASE,
)


def _normalize_retrieval_field(raw: str) -> str:
    field = re.sub(r"\s+", " ", str(raw or "").strip(" '\"「」『』“”‘’")).strip()
    previous = None
    while field and field != previous:
        previous = field
        field = _RETRIEVAL_FIELD_PREFIX_RE.sub("", field).strip()
    lowered = field.lower()
    if not field or lowered in _RETRIEVAL_FIELD_STOPWORDS:
        return ""
    if "输入精确" in field and any(root in field for root in ("客户", "产品", "商品", "订单", "记录")):
        return ""
    for stopword in _RETRIEVAL_FIELD_STOPWORDS:
        if stopword and len(lowered) > len(stopword) and lowered.endswith(stopword):
            return ""
    for root in _KNOWN_FIELD_ROOTS:
        if len(lowered) > len(root) and lowered.endswith(root):
            return root
    return lowered


def _retrieval_field_aliases(field: str) -> set[str]:
    norm = _normalize_retrieval_field(field)
    aliases = {norm} if norm else set()
    bilingual = {
        "产品": {"product", "产品", "商品"},
        "商品": {"product", "产品", "商品"},
        "product": {"product", "产品", "商品"},
        "客户": {"customer", "客户"},
        "customer": {"customer", "客户"},
        "昵称": {"nickname", "昵称"},
        "nickname": {"nickname", "昵称"},
        "标题": {"title", "标题"},
        "title": {"title", "标题"},
        "状态": {"status", "状态"},
        "status": {"status", "状态"},
    }
    aliases.update(bilingual.get(norm, set()))
    return {_normalize_retrieval_field(alias) for alias in aliases if _normalize_retrieval_field(alias)}


def _extract_retrieval_fields(text: str) -> list[str]:
    fields: list[str] = []
    for pattern in (_RETRIEVAL_FIELD_RE, _RETRIEVAL_FIELD_EQ_RE):
        for raw in pattern.findall(text or ""):
            field = _normalize_retrieval_field(raw)
            if field and field not in fields:
                fields.append(field)
    return fields


def _retrieval_fields_overlap(left: list[str], right: list[str]) -> bool:
    for a in left:
        aliases = _retrieval_field_aliases(a)
        na = _normalize_retrieval_field(a)
        for b in right:
            if aliases and (aliases & _retrieval_field_aliases(b)):
                return True
            # Suffix-tolerance for fields outside the bilingual dict: an over-captured prefix on
            # one side ("…后订单") still overlaps the bare field ("订单"). Min-length guards trivials.
            nb = _normalize_retrieval_field(b)
            if na and nb and min(len(na), len(nb)) >= 2 and (na.endswith(nb) or nb.endswith(na)):
                return True
    return False


def _flatten_branch_runs(stmts: list[Stmt]) -> list[Run]:
    out: list[Run] = []
    for item in stmts:
        if isinstance(item, RunLike):
            out.append(item)
        elif isinstance(item, If):
            out.extend(_flatten_branch_runs(item.then))
            out.extend(_flatten_branch_runs(item.otherwise))
        elif isinstance(item, ForEach):
            out.extend(_flatten_branch_runs(item.body))
    return out


def _looks_like_fuzzy_retry(text: str) -> bool:
    return bool(_RETRIEVAL_RETRY_CUE_RE.search(text or "") and _RETRIEVAL_ACTION_CUE_RE.search(text or ""))


def _mentions_same_retrieval_target(text: str) -> bool:
    return bool(_SAME_RETRIEVAL_TARGET_RE.search(text or ""))


def check_retrieval_retry_preserves_field(stmts: list[Stmt], issues: IssueList) -> None:
    """Exact->fuzzy retry branches must keep the same target field/column.

    This is a generic data-source guard, not a site rule: when a search/filter step already
    identifies the target field, an empty-result retry that only says "use keyword K again" lets
    the planner substitute any visible input. Keep the field in the branch so DOM-level field
    guards can enforce it.
    """

    def _walk_seq(seq: list[Stmt]) -> None:
        previous_fields: list[str] = []
        previous_label = ""
        for item in seq:
            if isinstance(item, RunLike):
                text = f"{item.name}\n{item.success_condition}\n{item.read_spec}"
                field_text = f"{item.name}\n{item.success_condition}"
                fields = _extract_retrieval_fields(field_text)
                if item.kind in {"filter", "action"} and fields and _RETRIEVAL_ACTION_CUE_RE.search(text):
                    previous_fields = fields
                    previous_label = item.name
                else:
                    previous_fields = []
                    previous_label = ""
                continue
            if isinstance(item, If):
                if previous_fields:
                    for branch_name, branch in (("then", item.then), ("otherwise", item.otherwise)):
                        for run in _flatten_branch_runs(branch):
                            if run.kind not in {"filter", "action"}:
                                continue
                            text = f"{run.name}\n{run.success_condition}\n{run.read_spec}"
                            field_text = f"{run.name}\n{run.success_condition}"
                            if not _looks_like_fuzzy_retry(text):
                                continue
                            fields = _extract_retrieval_fields(field_text)
                            if _mentions_same_retrieval_target(field_text):
                                continue
                            if not _retrieval_fields_overlap(previous_fields, fields):
                                issues.add(
                                    "RETRIEVAL_RETRY_DROPS_FIELD",
                                    f"if 分支 {branch_name} 的检索回退步骤「{run.name}」没有保留上一检索步骤"
                                    f"「{previous_label}」的目标字段/列 {previous_fields}。"
                                    "精确→关键词/模糊重试必须继续点名同一个字段/列（例如「在同一字段输入关键词并提交」），"
                                    "不能退化成泛关键词搜索，否则执行层可能把值填进相近但错误的字段。"
                                )
                _walk_seq(item.then)
                _walk_seq(item.otherwise)
                previous_fields = []
                previous_label = ""
            elif isinstance(item, ForEach):
                _walk_seq(item.body)
                previous_fields = []
                previous_label = ""

    _walk_seq(stmts)
