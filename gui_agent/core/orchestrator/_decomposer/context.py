"""Context-block helpers for orchestrator decompose/redecompose."""

from __future__ import annotations

from gui_agent.context import ContextBlock

from ..sql_utils import sql_identifier
from .sql import _schema_typed_shadow_candidates


def _corrective_directive_block(corrective_directive: str) -> "ContextBlock | None":
    """Runtime correction block with authority above app knowledge."""
    if not corrective_directive.strip():
        return None
    return ContextBlock(
        id="runtime.corrective_directive",
        budget="required",
        source_type="runtime_state",
        source="feasibility_kickback",
        ttl="task",
        priority=15,
        content=(
            "## ⚠️ 上层纠正指令\n"
            "【来源：上层运行时纠正（基于真实界面观察）｜权威级别：最高｜必须服从】\n"
            + corrective_directive.strip()
            + "\n\n依据上下文优先级裁决规则，本指令高于应用知识与默认习惯：与它们冲突时一律以本指令为准。"
        ),
    )


def _page_and_table_blocks(
    current_url: str, current_site: str, current_title: str, table_summaries: list[dict] | None
) -> list["ContextBlock"]:
    """Ground-truth front-tab identity plus current table inventory."""
    blocks: list[ContextBlock] = []
    if current_url or current_site:
        _parts = []
        if current_site:
            _parts.append(f"站点：{current_site}（已知应用）")
        if current_title:
            _parts.append(f"页面：{current_title}")
        if current_url:
            _parts.append(f"url：{current_url}")
        page = "\n## 当前前台页面（以此为准，截图看不到地址栏）：" + " · ".join(_parts)
        page += (
            "\n若当前已在任务目标站点，可省略『打开该站点』这类重复 milestone；"
            "但不要省略必要的页内定位/切换 tab/打开目标页面，也不要省略清除或设置筛选、搜索、排序等会改变数据源口径的 UI 步骤。"
            "视觉 read 前必须先让目标区域处于当前可见终态；"
            "若目标表格已出现在『当前结构化表格』列表中，只有当它已经处于任务要求的筛选/排序/范围终态时，data_query 才可直接查询该表格；否则先规划 UI 步骤准备数据源。"
        )
        blocks.append(ContextBlock(
            id="runtime.observation.browser_page",
            budget="high",
            source_type="runtime_state",
            source="observation",
            ttl="turn",
            priority=30,
            content=page,
        ))
    table_hint = _table_schema_prompt(table_summaries)
    if table_hint:
        blocks.append(ContextBlock(
            id="runtime.observation.table_schema",
            budget="high",
            source_type="runtime_state",
            source="browser_tables",
            ttl="turn",
            priority=35,
            content=table_hint,
        ))
    return blocks


def _prior_experience_block(prior_experience: str) -> "ContextBlock | None":
    """Executed steps + outcomes as experience, not a to-do list."""
    if not prior_experience.strip():
        return None
    return ContextBlock(
        id="runtime.prior_experience",
        budget="high",
        source_type="runtime_state",
        source="redecompose_progress",
        ttl="task",
        priority=16,
        content=(
            "## 已执行步骤与结果（经验，勿重做）\n"
            "【这是本次执行已经跑完的部分及其结果——是你的经验/上下文，不是要重做的清单。"
            "默认这些步骤的终态已达成；据此避开已被证伪的路径。】\n"
            + prior_experience.strip()
        ),
    )


def _remaining_plan_block(remaining_plan: str) -> "ContextBlock | None":
    """The unexecuted steps that redecompose must re-plan from the current page."""
    if not remaining_plan.strip():
        return None
    return ContextBlock(
        id="runtime.remaining_plan",
        budget="required",
        source_type="runtime_state",
        source="redecompose_progress",
        ttl="task",
        priority=17,
        content=(
            "## 剩余计划（重排目标）\n"
            "【以下是原计划里还没执行、需要你重新规划的部分。你的输出 steps 只覆盖这些剩余工作——"
            "从当前真实页面继续、服从上层纠正指令、吸收上面的经验把它们重新展开成可执行步骤。】\n"
            + remaining_plan.strip()
        ),
    )


def _table_schema_prompt(tables: list[dict] | None) -> str:
    """Compact table inventory for planning SQL, without row/cell values."""
    if not tables:
        return ""
    lines: list[str] = []
    used_aliases: set[str] = set()
    for idx, table in enumerate(tables[:12], start=1):
        if not isinstance(table, dict):
            continue
        headers = table.get("headers") if isinstance(table.get("headers"), list) else []
        aliases = [f"table_{idx}"]
        used_aliases.add(f"table_{idx}")
        if idx == 1:
            aliases.append("data")
            used_aliases.add("data")
        caption = str(table.get("caption") or "").strip()
        caption_alias = sql_identifier(caption)
        if caption_alias and caption_alias not in used_aliases:
            aliases.append(caption_alias)
            used_aliases.add(caption_alias)
        sql_columns = []
        labels = []
        for header in headers[:24]:
            label = str(header or "").strip()
            column = sql_identifier(label)
            if not column:
                continue
            sql_columns.append(column)
            if label and label != column:
                labels.append(f'{column} from "{label}"')
        row_count = table.get("row_count")
        try:
            row_text = str(int(str(row_count).replace(",", ""))) if row_count is not None else "?"
        except ValueError:
            row_text = "?"
        completeness = "partial" if table.get("partial") else "complete"
        caption_text = f' caption="{caption}";' if caption else ""
        typed_shadows = _schema_typed_shadow_candidates(headers, sql_columns)
        column_text = ", ".join(sql_columns) if sql_columns else "(no headers)"
        typed_text = f"; typed shadows if parseable: {', '.join(typed_shadows)}" if typed_shadows else ""
        labels_text = f"; source labels: {', '.join(labels)}" if labels else ""
        lines.append(
            f"- {'/'.join(aliases)};{caption_text} sql columns: {column_text}{typed_text}{labels_text}; rows: {row_text}; {completeness}"
        )
    if not lines:
        return ""
    return (
        "\n## 当前结构化表格（仅 schema，不含行数据）\n"
        "这些表格来自当前界面已采集的表格快照；用于规划 data_query 的表名和列名。"
        "这里故意不提供行数据，实际查询由受限 SQLite primitive 在运行时读取。\n"
        + "\n".join(lines)
        + "\n若这些表格已经是任务要求的数据源终态，可生成 data_query；否则先规划导航、筛选/搜索/排序、清除旧筛选或完整采集步骤。SQL 只能使用表名、sql columns 中列出的 snake_case 标识符，以及运行时可解析的 typed shadows。source labels 只是人类可读说明，不是 SQL 语法。"
    )
