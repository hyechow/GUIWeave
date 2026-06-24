"""Generic multi-turn collection runtime for DSL ``list_read`` runs.

The runtime is deliberately domain-neutral. It knows about collections, rows,
row actions, detail pages, fields, and pagination/scroll traversal. It does not
know site names, page names, or business fields. A product grid whose rows open
an edit page and a review grid whose rows are already complete are both the same
shape:

* read current table rows from ``Observation.tables``;
* choose stable row keys from common identity columns when available;
* if requested fields are already present in the table, accumulate rows directly;
* otherwise ask the planner to open one row's row-level action, read missing
  detail fields from the resulting page, then return to the list;
* when the current page is exhausted, ask the planner to paginate or scroll.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from gui_agent.core.schemas import Observation

from .traversal_controller import TraversalController


Action = Literal[
    "open_row",
    "read_detail",
    "return_to_list",
    "paginate_next",
    "paginate_prev",
    "scroll_down",
    "done",
    "fallback",
]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _display(value: Any, *, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field_for(headers: list[str], field: str) -> str | None:
    wanted = _norm(field)
    if not wanted:
        return None
    for header in headers:
        if _norm(header) == wanted:
            return header
    return None


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
) -> tuple[dict[str, Any], list[str]] | None:
    """Pick the table most likely to back the collection and return matched fields."""
    if not tables:
        return None
    best: tuple[int, int, int, dict[str, Any], list[str]] | None = None
    for table in tables:
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        headers = [str(h) for h in (table.get("headers") or [])]
        if not rows or not headers:
            continue
        matched = [field for field in returns if _field_for(headers, field)]
        action_score = 1 if any(_has_row_action(row) for row in rows[:5]) else 0
        total_score = 1 if table.get("total_records") else 0
        score = (len(matched) * 10 + action_score * 3 + total_score, len(rows), len(headers))
        if best is None or score > (best[0], best[1], best[2]):
            best = (score[0], score[1], score[2], table, matched)
    return (best[3], best[4]) if best else None


def rows_from_tables(
    tables: list[dict[str, Any]] | None,
    returns: list[str],
) -> list[dict[str, str]] | None:
    """Project the best DOM table onto ``returns`` when it covers any requested field."""
    picked = _best_table(tables, returns)
    if picked is None:
        return None
    table, matched = picked
    if not matched:
        return None
    headers = [str(h) for h in (table.get("headers") or [])]
    out: list[dict[str, str]] = []
    for raw in table.get("rows") or []:
        if not isinstance(raw, dict):
            continue
        row: dict[str, str] = {}
        for field in returns:
            header = _field_for(headers, field)
            row[field] = str(raw.get(header, "") if header else "").strip()
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
class CollectionRow:
    key: str
    ordinal: int
    page_key: str
    table_values: dict[str, str]
    result_values: dict[str, str]
    label: str


@dataclass
class CollectionDecision:
    action: Action
    reason: str
    instruction: str


@dataclass
class CollectionRuntime:
    """Stateful controller for one ``list_read`` run."""

    var: str
    returns: list[str]
    read_spec: str = ""
    rows: list[dict[str, str]] = field(default_factory=list)
    _seen_rows: set[str] = field(default_factory=set)
    _completed_rows: set[str] = field(default_factory=set)
    _current: CollectionRow | None = None
    _awaiting_return: bool = False
    _detail_attempts: int = 0
    _current_table_page_keys: set[str] = field(default_factory=set)
    _traversal: TraversalController = field(init=False)
    _last_decision: CollectionDecision = field(
        default_factory=lambda: CollectionDecision("fallback", "尚未观察集合", "观察当前集合")
    )
    expected_total: int | None = None

    def __post_init__(self) -> None:
        self._traversal = TraversalController(self.var)

    @property
    def done(self) -> bool:
        return self._last_decision.action == "done"

    def update(
        self,
        observation: Observation,
        *,
        read_detail: Callable[[list[str]], dict[str, str]],
    ) -> CollectionDecision:
        picked = _best_table(getattr(observation, "tables", None), self.returns)
        table = picked[0] if picked else None
        matched_fields = picked[1] if picked else []
        if (
            table is not None
            and self._current is not None
            and not self._table_contains_row(table, self._current)
            and not (self._awaiting_return and self._looks_like_collection_table(table))
        ):
            table = None
            matched_fields = []
        if table is not None:
            self._observe_table(table, matched_fields)
            if self._awaiting_return:
                self._current = None
                self._awaiting_return = False
                self._detail_attempts = 0
            decision = self._decide_from_list(table)
        elif self._current is not None:
            decision = self._decide_from_detail(observation, read_detail=read_detail)
        else:
            decision = CollectionDecision(
                "fallback",
                "当前帧没有可识别的集合表格，也没有正在处理的行",
                "根据当前页面判断如何回到目标列表或显示列表",
            )
        self._last_decision = decision
        return decision

    def _table_contains_row(self, table: dict[str, Any], row: CollectionRow) -> bool:
        headers = [str(h) for h in (table.get("headers") or [])]
        page_key = self._page_key(table)
        for ordinal, raw in enumerate(table.get("rows") or [], start=1):
            if not isinstance(raw, dict):
                continue
            table_values = {str(k): str(v or "").strip() for k, v in raw.items()}
            key = self._row_key(headers, table_values, page_key=page_key, ordinal=ordinal)
            if key == row.key:
                return True
        return False

    def _looks_like_collection_table(self, table: dict[str, Any]) -> bool:
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        if not rows:
            return False
        traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
        if traversal.get("type") in {"paged", "scroll"}:
            return True
        try:
            if int(table.get("total_records") or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        return any(_has_row_action(row) for row in rows[:5])

    def prompt_text(self) -> str:
        cur = f"当前行：{self._current.label}。" if self._current else "当前没有活动行。"
        return (
            "【集合遍历状态（系统结构化感知 + 状态机，权威事实）】"
            f"已完成 {len(self.rows)} 行；{cur}"
            f"下一步意图：{self._last_decision.action}。"
            f"原因：{self._last_decision.reason}。"
            f"请只执行一个原子操作：{self._last_decision.instruction}"
        )

    def _observe_table(self, table: dict[str, Any], matched_fields: list[str]) -> None:
        headers = [str(h) for h in (table.get("headers") or [])]
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        page_key = self._page_key(table)
        page_keys: set[str] = set()
        try:
            total = int(table.get("total_records") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and total >= len(rows) and (self.expected_total is None or total > self.expected_total):
            self.expected_total = total
        for ordinal, raw in enumerate(rows, start=1):
            table_values = {str(k): str(v or "").strip() for k, v in raw.items()}
            key = self._row_key(headers, table_values, page_key=page_key, ordinal=ordinal)
            page_keys.add(key)
            if key in self._seen_rows:
                continue
            result_values = self._result_values(headers, table_values)
            row = CollectionRow(
                key=key,
                ordinal=ordinal,
                page_key=page_key,
                table_values=table_values,
                result_values=result_values,
                label=self._row_label(table_values, ordinal=ordinal),
            )
            self._seen_rows.add(key)
            self._current_table_page_keys.add(key)
            # Direct table collection: all requested fields are present in this table.
            if set(self.returns).issubset(set(matched_fields)):
                self._complete_row(row, {})
            else:
                self._current_table_page_keys.add(key)
        self._current_table_page_keys = page_keys

    def _decide_from_list(self, table: dict[str, Any]) -> CollectionDecision:
        if self.expected_total and len(self.rows) >= self.expected_total:
            return CollectionDecision(
                "done",
                f"已累计 {len(self.rows)} 行，达到列表声明总数 {self.expected_total}",
                "停止，不要继续翻页或打开行",
            )

        if self._current is not None and self._current.key not in self._completed_rows:
            return CollectionDecision(
                "open_row",
                f"上一轮仍需处理行 {self._current.label}",
                f"打开这一行的详情/编辑入口：{self._current.label}",
            )

        next_row = self._next_pending_row(table)
        if next_row is not None:
            self._current = next_row
            self._detail_attempts = 0
            return CollectionDecision(
                "open_row",
                f"当前页还有未处理行 {next_row.label}",
                f"打开当前表格第 {next_row.ordinal} 行的详情/编辑入口（行标识：{next_row.label}）",
            )

        traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else None
        action = self._traversal.update(traversal)
        if action == "paginate_next":
            return CollectionDecision("paginate_next", "当前页行已处理完，感知到下一页可用", "点击下一页")
        if action == "paginate_prev":
            return CollectionDecision("paginate_prev", "采集从列表中段开始，先回到第一页", "点击上一页或第 1 页")
        if action == "scroll_down":
            return CollectionDecision("scroll_down", "当前页行已处理完，列表还能继续滚动", "向下滚动加载更多行")
        if action == "done":
            return CollectionDecision("done", f"当前页已处理完且无可用后续页/滚动；累计 {len(self.rows)} 行", "停止")
        return CollectionDecision(
            "fallback",
            "当前页行已处理完，但遍历状态不完整",
            "判断是否还有下一页/更多行；若有则前进，否则停止",
        )

    def _decide_from_detail(
        self,
        observation: Observation,
        *,
        read_detail: Callable[[list[str]], dict[str, str]],
    ) -> CollectionDecision:
        assert self._current is not None
        missing = [field for field in self.returns if not self._current.result_values.get(field)]
        detail_values = self._read_from_form_controls(observation, missing)
        still_missing = [field for field in missing if not detail_values.get(field)]
        if still_missing and self._detail_attempts == 0:
            vision_values = read_detail(still_missing)
            detail_values.update({k: v for k, v in vision_values.items() if v})
            still_missing = [field for field in missing if not detail_values.get(field)]
        if missing and still_missing:
            self._detail_attempts += 1
            return CollectionDecision(
                "read_detail",
                f"当前在行详情页，但字段 {', '.join(still_missing)} 尚未读到",
                f"在当前详情页显示/定位字段 {', '.join(still_missing)}；必要时滚动或展开相关区域",
            )
        self._complete_row(self._current, detail_values)
        self._awaiting_return = True
        return CollectionDecision(
            "return_to_list",
            f"已读取 {self._current.label} 的详情字段",
            "返回上一页/返回列表，回到原集合继续下一行",
        )

    def _complete_row(self, row: CollectionRow, detail_values: dict[str, str]) -> None:
        values = {field: row.result_values.get(field, "") for field in self.returns}
        for field in self.returns:
            if detail_values.get(field):
                values[field] = detail_values[field]
        if row.key not in self._completed_rows:
            self._completed_rows.add(row.key)
            self.rows.append(values)

    def _next_pending_row(self, table: dict[str, Any]) -> CollectionRow | None:
        headers = [str(h) for h in (table.get("headers") or [])]
        page_key = self._page_key(table)
        for ordinal, raw in enumerate(table.get("rows") or [], start=1):
            if not isinstance(raw, dict):
                continue
            table_values = {str(k): str(v or "").strip() for k, v in raw.items()}
            key = self._row_key(headers, table_values, page_key=page_key, ordinal=ordinal)
            if key in self._completed_rows:
                continue
            return CollectionRow(
                key=key,
                ordinal=ordinal,
                page_key=page_key,
                table_values=table_values,
                result_values=self._result_values(headers, table_values),
                label=self._row_label(table_values, ordinal=ordinal),
            )
        return None

    def _result_values(self, headers: list[str], table_values: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for field in self.returns:
            header = _field_for(headers, field)
            out[field] = table_values.get(header, "") if header else ""
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

    def _read_from_form_controls(self, observation: Observation, fields: list[str]) -> dict[str, str]:
        controls = getattr(observation, "form_controls", None) or []
        out: dict[str, str] = {}
        for field in fields:
            wanted = _norm(field)
            if not wanted:
                continue
            best: tuple[int, str] | None = None
            for control in controls:
                if not isinstance(control, dict):
                    continue
                haystack = " ".join(
                    str(control.get(k, ""))
                    for k in ("label", "name", "id", "placeholder")
                )
                nh = _norm(haystack)
                if not nh:
                    continue
                if nh == wanted:
                    score = 3
                elif wanted in nh:
                    score = 2
                else:
                    continue
                value = str(control.get("selected_text") or control.get("value") or "").strip()
                if value and (best is None or score > best[0]):
                    best = (score, value)
            out[field] = best[1] if best else ""
        return out
