"""Runtime repair for `data_query` SQL using the actual collected table data.

The decomposer writes an initial SQL plan before the browser has collected the
final table snapshot. This module is a narrow execution-time recovery layer:
when that SQL fails or produces an implausibly empty result on non-empty data,
first ask whether the collected table is the right source for the task, then
rewrite only the SQL from the real schema/sample values. The rewritten SQL still
runs through the same restricted `execute_data_query` sandbox.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured


class DataQueryRepair(BaseModel):
    source_ok: bool = Field(
        default=True,
        description="当前已采集表格是否看起来就是用户任务要求的数据源口径",
    )
    source_issue: str = Field(
        default="",
        description="若 source_ok=false，说明 UI/表格状态与任务意图的冲突，以及需要回到界面修正什么",
    )
    reason: str = Field(default="", description="为什么需要改写，以及如何使用真实表格字段/样例值")
    sql: str = Field(default="", description="只读 SELECT 或 WITH ... SELECT SQL")


_SYSTEM = load_prompt_text("task.orchestrator.data_query_repair")


def repair_data_query_sql(
    *,
    goal: str,
    run_name: str,
    requested_returns: list[str],
    original_sql: str,
    tables: list[dict[str, Any]] | None,
    failure: str = "",
    recent_ui_context: str = "",
) -> DataQueryRepair | None:
    """Return a candidate replacement SQL, or None if repair cannot be attempted."""
    profile = _tables_profile(tables)
    if not profile:
        return None
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    prompt = {
        "goal": goal,
        "data_query_step": run_name,
        "requested_returns": requested_returns,
        "original_sql": original_sql,
        "failure_or_suspicious_result": failure,
        "recent_ui_context": recent_ui_context,
        "actual_tables": profile,
    }
    try:
        repair = invoke_structured(
            llm,
            [
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=json.dumps(prompt, ensure_ascii=False, indent=2)),
            ],
            DataQueryRepair,
        )
    except Exception:
        return None
    if not repair.source_ok:
        return repair
    sql = (repair.sql or "").strip()
    if not re.match(r"^\s*(select|with)\b", sql, flags=re.IGNORECASE):
        return None
    return repair


def _tables_profile(tables: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, table in enumerate(tables or [], start=1):
        if not isinstance(table, dict):
            continue
        rows = [r for r in (table.get("rows") or []) if isinstance(r, dict)]
        if not rows:
            continue
        headers = table.get("headers") or _headers_from_rows(rows)
        columns = _unique_identifiers(headers)
        sample_rows = []
        for row in rows[:5]:
            sample_rows.append({str(k): _cut(v) for k, v in list(row.items())[:24]})
        distinct: dict[str, list[str]] = {}
        ranges: dict[str, dict[str, str]] = {}
        for header, column in zip(headers[:24], columns[:24]):
            values: list[str] = []
            all_values: list[str] = []
            for row in rows[:200]:
                raw = _lookup(row, header, column)
                if raw in (None, ""):
                    continue
                text = _cut(raw, 80)
                all_values.append(text)
                if text not in values and len(values) < 8:
                    values.append(text)
            if values:
                distinct[column] = values
            ranged = _value_range(all_values)
            if ranged:
                ranges[column] = ranged
        aliases = [f"table_{idx}"]
        if idx == 1:
            aliases.append("data")
        caption_alias = _identifier(table.get("caption") or "")
        if caption_alias and caption_alias not in aliases:
            aliases.append(caption_alias)
        out.append(
            {
                "caption": table.get("caption") or "",
                "aliases": aliases,
                "row_count": table.get("row_count") or table.get("totalRecords") or table.get("total_records") or len(rows),
                "partial": bool(table.get("partial")),
                "sql_columns": columns,
                "source_labels": {column: str(header) for header, column in zip(headers, columns)},
                "sample_rows": sample_rows,
                "distinct_sample_values": distinct,
                "value_ranges": ranges,
            }
        )
    return out


def _headers_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(str(key))
    return headers


def _unique_identifiers(headers: list[Any]) -> list[str]:
    out: list[str] = []
    seen: dict[str, int] = {}
    for idx, header in enumerate(headers, 1):
        base = _identifier(header) or f"col_{idx}"
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}_{n}")
    return out


def _identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text


def _lookup(row: dict[str, Any], header: Any, column: str) -> Any:
    if header in row:
        return row.get(header)
    by_norm = {_identifier(k): v for k, v in row.items()}
    return by_norm.get(column)


def _cut(value: Any, limit: int = 160) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _value_range(values: list[str]) -> dict[str, str]:
    clean = [v for v in values if v]
    if len(clean) < 2:
        return {}
    if not any(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}", v) for v in clean[:20]):
        return {}
    ordered = sorted(clean)
    return {"min": ordered[0], "max": ordered[-1]}
