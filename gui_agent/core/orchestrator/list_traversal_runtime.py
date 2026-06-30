"""Generic multi-turn traversal runtime for DSL ``list_read`` runs.

The runtime is deliberately domain-neutral. It only observes table/grid rows,
deduplicates visible records, and asks the planner to perform the next
pagination or scrolling operation recommended by the traversal sensor. It does
not open row details, read detail pages, or decide per-row workflows. Those
steps belong in explicit DSL structure such as ``foreach`` body runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from gui_agent.core.schemas import Observation

from .traversal_controller import TraversalController


Action = Literal[
    "paginate_next",
    "paginate_prev",
    "scroll_down",
    "schema_mismatch",
    "done",
    "fallback",
]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _tokens(value: Any) -> list[str]:
    return [p for p in re.split(r"[^a-z0-9]+", str(value or "").strip().lower()) if p]


def _display(value: Any, *, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class ColumnResolution:
    field: str
    header: str | None
    status: Literal["resolved", "unresolved", "ambiguous"]
    confidence: float
    reason: str = ""


def _is_id_like_header(header: str) -> bool:
    normalized = _norm(header)
    return normalized == "id" or normalized.endswith("id") or normalized in {"identifier", "number", "no"}


def _resolve_field(headers: list[str], field: str) -> ColumnResolution:
    """Resolve a requested field to a visible table header without site-specific facts."""

    wanted = _norm(field)
    if not wanted:
        return ColumnResolution(field, None, "unresolved", 0.0, "empty requested field")

    normalized_headers = [(_norm(header), header) for header in headers]
    for normalized, header in normalized_headers:
        if normalized == wanted:
            return ColumnResolution(field, header, "resolved", 1.0, "exact normalized match")

    field_tokens = _tokens(field)
    if field_tokens and field_tokens[-1] == "id":
        id_headers = [header for _, header in normalized_headers if _is_id_like_header(header)]
        if len(id_headers) == 1:
            return ColumnResolution(
                field,
                id_headers[0],
                "resolved",
                0.88,
                "requested *_id and table has one ID-like column",
            )
        return ColumnResolution(
            field,
            None,
            "ambiguous" if id_headers else "unresolved",
            0.0,
            f"ID-like columns: {id_headers}",
        )

    field_token_set = set(field_tokens)
    if field_token_set:
        candidates: list[tuple[float, str]] = []
        for _, header in normalized_headers:
            header_token_set = set(_tokens(header))
            if not header_token_set:
                continue
            overlap = field_token_set & header_token_set
            if not overlap:
                continue
            score = len(overlap) / max(len(field_token_set), len(header_token_set), 1)
            if score >= 0.67:
                candidates.append((score, header))
        candidates.sort(reverse=True)
        if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
            score, header = candidates[0]
            return ColumnResolution(field, header, "resolved", min(0.95, 0.7 + score * 0.2), "token overlap")
        if candidates:
            return ColumnResolution(field, None, "ambiguous", 0.0, f"candidate columns: {[h for _, h in candidates]}")

    return ColumnResolution(field, None, "unresolved", 0.0, "no reliable column match")


def _resolve_columns(headers: list[str], returns: list[str]) -> list[ColumnResolution]:
    return [_resolve_field(headers, field) for field in returns]


def _field_for(headers: list[str], field: str) -> str | None:
    resolved = _resolve_field(headers, field)
    return resolved.header if resolved.status == "resolved" else None


def _has_row_action(row: dict[str, str]) -> bool:
    for key, value in row.items():
        nk = _norm(key)
        nv = _norm(value)
        if nk in {"action", "actions", "operation", "operations"} and nv:
            return True
        if nv in {"edit", "view", "open", "details", "detail"}:
            return True
    return False


def _best_table(
    tables: list[dict[str, Any]] | None,
    returns: list[str],
) -> tuple[dict[str, Any], list[ColumnResolution]] | None:
    """Pick the table most likely to back the collection and return column resolutions."""
    if not tables:
        return None
    best: tuple[int, int, int, dict[str, Any], list[ColumnResolution]] | None = None
    for table in tables:
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        headers = [str(h) for h in (table.get("headers") or [])]
        if not rows or not headers:
            continue
        resolutions = _resolve_columns(headers, returns)
        matched_count = sum(1 for resolution in resolutions if resolution.status == "resolved")
        action_score = 1 if any(_has_row_action(row) for row in rows[:5]) else 0
        total_score = 1 if table.get("total_records") else 0
        score = (matched_count * 10 + action_score * 3 + total_score, len(rows), len(headers))
        if best is None or score > (best[0], best[1], best[2]):
            best = (score[0], score[1], score[2], table, resolutions)
    return (best[3], best[4]) if best else None


def rows_from_tables(
    tables: list[dict[str, Any]] | None,
    returns: list[str],
) -> list[dict[str, str]] | None:
    """Project the best DOM table onto ``returns`` when it covers any requested field."""
    picked = _best_table(tables, returns)
    if picked is None:
        return None
    table, resolutions = picked
    if not any(resolution.status == "resolved" for resolution in resolutions):
        return None
    out: list[dict[str, str]] = []
    for raw in table.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        row: dict[str, str] = {}
        for resolution in resolutions:
            row[resolution.field] = str(raw.get(resolution.header, "") if resolution.header else "").strip()
        if any(row.values()):
            out.append(row)
    return out


def expected_total_from_tables(
    tables: list[dict[str, Any]] | None,
    returns: list[str],
) -> int | None:
    picked = _best_table(tables, returns)
    if picked is None:
        return None
    try:
        total = int(picked[0].get("total_records") or 0)
    except (TypeError, ValueError):
        return None
    return total if total > 0 else None


@dataclass
class ListTraversalRow:
    key: str
    ordinal: int
    page_key: str
    table_values: dict[str, str]
    result_values: dict[str, str]
    label: str


@dataclass
class ListTraversalDecision:
    action: Action
    reason: str
    instruction: str


@dataclass
class ListTraversalRuntime:
    """Stateful traversal and passive row accumulator for one ``list_read`` run."""

    var: str
    returns: list[str]
    read_spec: str = ""
    rows: list[dict[str, str]] = field(default_factory=list)
    _seen_rows: set[str] = field(default_factory=set)
    _traversal: TraversalController = field(init=False)
    _last_decision: ListTraversalDecision = field(
        default_factory=lambda: ListTraversalDecision("fallback", "尚未观察集合", "观察当前集合")
    )
    expected_total: int | None = None
    _last_visible_rows: int = 0
    _last_resolutions: list[ColumnResolution] = field(default_factory=list)
    # True once a structured observation.tables was seen (Browser DOM). Stays False on
    # vision-only platforms (Android/iPhone), where completion defers to the LLM checker —
    # see MilestoneSupervisorPolicy.react_until_collected.
    ever_saw_table: bool = False

    def __post_init__(self) -> None:
        self._traversal = TraversalController(self.var)

    @property
    def done(self) -> bool:
        return self._last_decision.action == "done"

    def update(
        self,
        observation: Observation,
        *,
        vision_rows: list[dict[str, str]] | None = None,
    ) -> ListTraversalDecision:
        picked = _best_table(getattr(observation, "tables", None), self.returns)
        table = picked[0] if picked else None
        resolutions = picked[1] if picked else []
        if table is not None:
            self.ever_saw_table = True
            visible_rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
            self._last_visible_rows = len(visible_rows)
            self._last_resolutions = resolutions
            if visible_rows and not any(resolution.status == "resolved" for resolution in resolutions):
                decision = ListTraversalDecision(
                    "schema_mismatch",
                    (
                        "已看到集合表格行，但 requested fields 与表头没有可靠映射；"
                        f"字段解析：{self._resolution_summary(resolutions)}"
                    ),
                    "停止翻页或滚动；需要先修正读取字段与表头映射，或使用视觉列表读取兜底",
                )
            else:
                before = len(self.rows)
                self._observe_table(table, resolutions)
                if visible_rows and len(self.rows) == before and not self.rows and any(
                    resolution.status == "resolved" for resolution in resolutions
                ):
                    decision = ListTraversalDecision(
                        "schema_mismatch",
                        (
                            "目标字段已映射到表头，但可见行在这些字段下没有读到任何值；"
                            f"字段解析：{self._resolution_summary(resolutions)}"
                        ),
                        "停止翻页或滚动；需要先修正读取字段与表头映射，或使用视觉列表读取兜底",
                    )
                else:
                    decision = self._decide_from_list(table)
        elif vision_rows:
            # Table-less platform (Android/iPhone vision): the deterministic table sensor has no
            # signal here, so accumulate the rows the vision reader extracted this frame instead.
            decision = self._observe_vision_rows(vision_rows)
        else:
            decision = ListTraversalDecision(
                "fallback",
                "当前帧没有可识别的集合表格",
                "根据当前页面判断如何回到目标列表或显示列表",
            )
        self._last_decision = decision
        return decision

    def _observe_vision_rows(self, vision_rows: list[dict[str, str]]) -> ListTraversalDecision:
        """Table-less platform (Android/iPhone): accumulate vision-extracted rows.

        The traversal sensor is table-backed and unavailable here, so this path only dedups +
        accumulates the rows the vision reader extracted this frame — giving the downstream
        ``foreach`` real row values. Completion on a table-less platform is the LLM checker's
        traversal verdict (policy's ``react_until_collected`` trusts checker-done when there is
        no structural signal); the plateau hint below is a secondary signal that also lets
        ``runtime.done`` flip to True once a scroll yields no new rows."""
        new = 0
        for raw in vision_rows:
            if not isinstance(raw, dict):
                continue
            values = {str(k): str(v or "").strip() for k, v in raw.items()}
            key = self._vision_row_key(values)
            if not key or key in self._seen_rows:
                continue
            self._seen_rows.add(key)
            self.rows.append({field: values.get(field, "") for field in self.returns})
            new += 1
        self._last_visible_rows = len(vision_rows)
        if new == 0 and self.rows:
            return ListTraversalDecision(
                "done",
                f"视觉读取本帧无新增行，已累计 {len(self.rows)} 行（疑似已到集合末尾；以验收为准）",
                "停止滚动；若仍有目标行未采集请继续滚动",
            )
        return ListTraversalDecision(
            "fallback",
            f"视觉读取本帧新增 {new} 行，已累计 {len(self.rows)} 行",
            "若界面仍有未采集的目标行则向下滚动，否则停止",
        )

    def _vision_row_key(self, values: dict[str, str]) -> str:
        """Dedup key for a vision row: primary return-field value when present, else joined values."""
        for field in self.returns:
            normalized = _norm(values.get(field))
            if normalized:
                return f"{_norm(field)}:{normalized}"
        joined = "|".join(_norm(v) for v in values.values() if v)
        return f"row:{joined}" if joined else ""

    def prompt_text(self) -> str:
        return (
            "【集合遍历状态（系统结构化感知 + 状态机，权威事实）】"
            f"已累计 {len(self.rows)} 行；"
            f"下一步意图：{self._last_decision.action}。"
            f"原因：{self._last_decision.reason}。"
            f"请只执行一个原子操作：{self._last_decision.instruction}"
        )

    def _observe_table(self, table: dict[str, Any], resolutions: list[ColumnResolution]) -> None:
        headers = [str(h) for h in (table.get("headers") or [])]
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        page_key = self._page_key(table)
        try:
            total = int(table.get("total_records") or 0)
        except (TypeError, ValueError):
            total = 0
        if not any(resolution.status == "resolved" for resolution in resolutions):
            return
        if total and total >= len(rows) and (self.expected_total is None or total > self.expected_total):
            self.expected_total = total
        for ordinal, raw in enumerate(rows, start=1):
            table_values = {str(k): str(v or "").strip() for k, v in raw.items()}
            key = self._row_key(headers, table_values, page_key=page_key, ordinal=ordinal)
            if key in self._seen_rows:
                continue
            result_values = self._result_values(headers, table_values)
            row = ListTraversalRow(
                key=key,
                ordinal=ordinal,
                page_key=page_key,
                table_values=table_values,
                result_values=result_values,
                label=self._row_label(table_values, ordinal=ordinal),
            )
            self._seen_rows.add(key)
            self.rows.append({field: row.result_values.get(field, "") for field in self.returns})

    def _decide_from_list(self, table: dict[str, Any]) -> ListTraversalDecision:
        if self.expected_total and len(self.rows) >= self.expected_total:
            return ListTraversalDecision(
                "done",
                f"已累计 {len(self.rows)} 行，达到列表声明总数 {self.expected_total}",
                "停止，不要继续翻页或滚动",
            )

        traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else None
        action = self._traversal.update(traversal)
        if action == "paginate_next":
            return ListTraversalDecision("paginate_next", "感知到下一页可用", "点击下一页")
        if action == "paginate_prev":
            return ListTraversalDecision("paginate_prev", "采集从列表中段开始，先回到第一页", "点击上一页或第 1 页")
        if action == "scroll_down":
            return ListTraversalDecision("scroll_down", "列表还能继续滚动", "向下滚动加载更多行")
        if action == "done":
            if self.expected_total and len(self.rows) < self.expected_total:
                if self.rows:
                    return ListTraversalDecision(
                        "done",
                        (
                            f"已到集合末尾，累计 {len(self.rows)} 行；列表声明总数 {self.expected_total} "
                            "与可采集唯一行不一致，按遍历边界完成并保留差异证据"
                        ),
                        "停止，不要仅因总数提示不一致而继续翻页、滚动或调整每页条数",
                    )
                return ListTraversalDecision(
                    "schema_mismatch",
                    f"遍历传感器认为已到末尾，但累计 0 行，少于列表声明总数 {self.expected_total}",
                    "停止翻页或滚动；需要先修正读取字段与表头映射，或使用视觉列表读取兜底",
                )
            return ListTraversalDecision("done", f"无可用后续页/滚动；累计 {len(self.rows)} 行", "停止")
        return ListTraversalDecision(
            "fallback",
            "遍历状态不完整",
            "判断是否还有下一页/更多行；若有则前进，否则停止",
        )

    def _result_values(self, headers: list[str], table_values: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for resolution in _resolve_columns(headers, self.returns):
            out[resolution.field] = table_values.get(resolution.header, "") if resolution.header else ""
        return out

    def _row_key(self, headers: list[str], values: dict[str, str], *, page_key: str, ordinal: int) -> str:
        for candidate in ("id", "sku", "code", "email", "order", "number", "name"):
            header = _field_for(headers, candidate)
            if header and values.get(header):
                return f"{_norm(header)}:{_norm(values[header])}"
        return f"page:{page_key}:row:{ordinal}"

    def _row_label(self, values: dict[str, str], *, ordinal: int) -> str:
        parts: list[str] = []
        for wanted in ("ID", "SKU", "Code", "Name", "Email", "Title"):
            for key, value in values.items():
                if _norm(key) == _norm(wanted) and value:
                    parts.append(f"{key}={_display(value, limit=50)}")
                    break
        if not parts:
            for key, value in values.items():
                if value:
                    parts.append(f"{key}={_display(value, limit=50)}")
                if len(parts) >= 2:
                    break
        return f"第 {ordinal} 行" + (f"（{'; '.join(parts[:3])}）" if parts else "")

    def _page_key(self, table: dict[str, Any]) -> str:
        traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
        page_index = traversal.get("page_index")
        if page_index:
            return str(page_index)
        page = table.get("page") if isinstance(table.get("page"), dict) else {}
        url = str(page.get("url") or "")
        return f"{table.get('index', '')}:{url}"

    @staticmethod
    def _resolution_summary(resolutions: list[ColumnResolution]) -> str:
        if not resolutions:
            return "无 requested fields"
        parts: list[str] = []
        for resolution in resolutions:
            target = resolution.header or "未映射"
            parts.append(f"{resolution.field}->{target}({resolution.status})")
        return "；".join(parts)
