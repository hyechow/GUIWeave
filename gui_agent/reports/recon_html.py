"""HTML rendering for recon reports."""

from __future__ import annotations

from .models import AppReconData, NavNode, ReconPageInfo

def _json_val_html(v) -> str:
    if v is None:
        return '<span class="jv-null">null</span>'
    if isinstance(v, bool):
        return f'<span class="jv-bool">{"true" if v else "false"}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="jv-num">{v}</span>'
    if isinstance(v, str):
        safe = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<span class="jv-str">"{safe}"</span>'
    return str(v)


_OVERLAY_FORMS = frozenset("CDEFG")
_FORM_LABELS = {"C": "弹窗", "D": "底部面板", "E": "侧边抽屉", "F": "菜单", "G": "键盘页"}


def _form_from_fingerprint(fingerprint: str) -> str | None:
    """Extract form letter from fingerprint text '...页面形态：X'."""
    import re as _re
    m = _re.search(r'页面形态[：:]\s*([A-G])', fingerprint or "")
    return m.group(1) if m else None


def _render_identity_json(identity: dict) -> str:
    items = list(identity.items())
    rows = ""
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        rows += (
            f'<div class="jrow">'
            f'<span class="jk">"{k}"</span>'
            f'<span class="jpunct">: </span>'
            f'{_json_val_html(v)}'
            f'<span class="jpunct">{comma}</span>'
            f'</div>'
        )
    return f'<div class="jobj"><span class="jpunct">{{</span>{rows}<span class="jpunct">}}</span></div>'

def _build_nav_tree(pages: list[ReconPageInfo], trace: list[dict] | None = None) -> list[NavNode]:
    """Build navigation tree. Uses trace if available, otherwise infers from flows."""
    node_map: dict[str, NavNode] = {p.name: NavNode(name=p.name, page=p) for p in pages}
    has_parent: set[str] = set()

    if trace:
        # Identify root pages (parent is None/missing) upfront so re-visits
        # as children of other pages don't prevent them from being roots.
        root_pages: set[str] = set()
        for entry in trace:
            if not entry.get("parent"):
                root_pages.add(entry.get("page", ""))

        # Accurate: use recorded BFS parent-child relationships
        for entry in trace:
            page_name = entry.get("page", "")
            parent_name = entry.get("parent")
            via_tap = entry.get("via_tap")
            if page_name not in node_map or not parent_name or parent_name not in node_map:
                continue
            # Never assign a parent to a root page — it stays at the top of the tree.
            if page_name in root_pages:
                continue
            child = node_map[page_name]
            parent = node_map[parent_name]
            # Only assign one parent per node to keep a proper tree (no cycles).
            if page_name not in has_parent and child not in parent.children:
                child.via_tap = via_tap
                parent.children.append(child)
                has_parent.add(page_name)
    # Fallback: leaf/overlay pages not in trace — attach via page.parent
    for p in pages:
        if p.name in has_parent or not p.parent:
            continue
        if p.parent in node_map and p.name in node_map:
            node_map[p.parent].children.append(node_map[p.name])
            has_parent.add(p.name)

    return [node_map[p.name] for p in pages if p.name not in has_parent]


def _render_tree_html(nodes: list[NavNode], _visited: frozenset[str] = frozenset()) -> str:
    if not nodes:
        return ""
    items = ""
    for node in nodes:
        if node.page is not None:
            if node.page.name in _visited:
                continue  # cycle guard
            is_leaf = node.page.page_type == "leaf"
            type_label = PAGE_TYPE_LABELS.get(node.page.page_type, node.page.page_type)
            slug = _slug(node.page.name)
            badge = f'<span class="tree-chip">{type_label}</span>' if type_label and not is_leaf else ''
            child_visited = _visited | {node.page.name}
            sub = f'<ul>{_render_tree_html(node.children, child_visited)}</ul>' if node.children else ''
            link_cls = "tree-link tree-link-error" if node.page.error else "tree-link tree-link-leaf" if is_leaf else "tree-link"
            error_dot = '<span class="tree-error-dot">⚠</span>' if node.page.error else ''
            items += (
                f'<li class="tree-node">'
                f'<a class="{link_cls}" href="#{slug}">{error_dot}{node.page.title}{badge}</a>'
                f'{sub}</li>'
            )
        else:
            items += (
                f'<li class="tree-node">'
                f'<span class="tree-leaf">· {node.name}</span>'
                f'</li>'
            )
    return items


def _dfs_order(nodes: list[NavNode], depth: int = 0) -> list[tuple[NavNode, int]]:
    """DFS traversal returning (node, depth) pairs in pre-order."""
    result = []
    for node in nodes:
        result.append((node, depth))
        if node.children:
            result.extend(_dfs_order(node.children, depth + 1))
    return result

def _render_error_html(error: dict | None, annotated_url: str = "") -> str:
    if not error:
        return ""
    msg = error.get("message", "探测中断")
    failed_tap = error.get("failed_tap")
    failed_el = error.get("failed_element", "")
    attempts: list[dict] = error.get("back_attempts", [])

    meta = ""
    if failed_tap and failed_tap > 0 and failed_el:
        meta = f'<div class="error-meta">第 {failed_tap} 个元素「{failed_el}」点击后无法返回</div>'

    timeline = ""
    if attempts:
        steps = ""
        for i, a in enumerate(attempts, 1):
            strategy = a.get("strategy", "?")
            coords = a.get("coords", [])
            coord_str = f"({coords[0]},{coords[1]})" if coords else ""
            result_text = a.get("result", "")
            score = a.get("score")
            score_str = f"{score:.3f}" if score is not None else ""
            score_html = f'<span class="back-score">{score_str}</span>' if score_str else ""
            success = a.get("success", False)
            if strategy == "retry":
                steps += (
                    f'<div class="back-step back-step-retry">'
                    f'<span class="back-num">↻</span>'
                    f'<span class="back-strategy">retry</span>'
                    f'<span class="back-coords"></span>'
                    f'<span class="back-result">{result_text}</span>'
                    f'{score_html}'
                    f'</div>'
                )
                continue
            step_cls = "back-step back-step-ok" if success else "back-step"
            steps += (
                f'<div class="{step_cls}">'
                f'<span class="back-num">{i}</span>'
                f'<span class="back-strategy">{strategy}</span>'
                f'<span class="back-coords">{coord_str}</span>'
                f'<span class="back-result">{result_text}</span>'
                f'{score_html}'
                f'</div>'
            )
        timeline = f'<div class="back-timeline">{steps}</div>'

    img_html = ""
    if annotated_url:
        img_html = (
            f'<div class="error-screenshot">'
            f'<img src="{annotated_url}" title="点击放大" onclick="openModal(this.src)">'
            f'<div class="error-screenshot-caption">返回尝试位置（数字=尝试顺序）</div>'
            f'</div>'
        )

    detail_html = (
        f'{meta}'
        f'<div class="error-body">'
        f'{img_html}'
        f'{timeline}'
        f'</div>'
    ) if (meta or img_html or timeline) else ""

    if detail_html:
        return (
            f'<details class="error-banner">'
            f'<summary class="error-title">⚠ 探测中断：{msg}</summary>'
            f'{detail_html}'
            f'</details>'
        )
    return f'<div class="error-banner"><div class="error-title">⚠ 探测中断：{msg}</div></div>'

# ── Recon HTML generator ───────────────────────────────────────

PAGE_TYPE_LABELS: dict[str, str] = {
    "list": "列表",
    "chat": "聊天",
    "detail": "详情",
    "form": "表单",
    "modal": "弹窗",
    "home": "首页",
    "other": "其他",
}

RECON_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{app_name} — Recon Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --green: #22c55e;
    --gray: #9ca3af;
    --blue: #3b82f6;
    --bg: #f1f5f9;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --muted: #64748b;
    --radius: 12px;
  }}
  body {{ font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; background: var(--bg); color: var(--text); }}

  /* ── Layout ── */
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 220px; flex-shrink: 0; position: sticky; top: 0; height: 100vh;
    background: var(--card); border-right: 1px solid var(--border);
    overflow-y: auto; padding: 20px 0;
  }}
  .main {{ flex: 1; padding: 24px; max-width: 900px; }}

  /* ── Sidebar ── */
  .sidebar-title {{ font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; padding: 0 16px 10px; }}
  .sidebar-stats {{ padding: 14px 16px; margin-top: 8px; border-top: 1px solid var(--border); font-size: 12px; color: var(--muted); line-height: 2; }}
  .sidebar-stats strong {{ color: var(--text); }}
  .sidebar-warnings {{ margin-top: 8px; border-top: 1px solid var(--border); }}
  .sidebar-warnings-title {{ padding: 10px 16px; font-size: 11px; font-weight: 600; color: #b45309; cursor: pointer; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px; }}
  .sidebar-warnings-title::before {{ content: "▶"; font-size: 8px; transition: transform 0.15s; }}
  details.sidebar-warnings[open] .sidebar-warnings-title::before {{ transform: rotate(90deg); }}
  .warn-item {{ padding: 6px 16px; border-top: 1px solid var(--border); }}
  .warn-title {{ font-size: 11px; color: var(--text); font-weight: 500; }}
  .warn-detail {{ font-size: 10px; color: var(--muted); margin-top: 2px; }}

  /* ── Nav tree ── */
  .nav-tree, .nav-tree ul {{ list-style: none; margin: 0; padding: 0; }}
  .nav-tree {{ padding: 0 8px; }}
  .nav-tree > li {{ padding-left: 4px; }}
  .nav-tree ul {{
    margin-left: 14px;
    padding-left: 10px;
    border-left: 1px solid var(--border);
  }}
  .tree-node {{ position: relative; padding: 1px 0; }}
  .nav-tree ul > .tree-node::before {{
    content: "";
    position: absolute;
    left: -11px;
    top: 14px;
    width: 10px;
    height: 1px;
    background: var(--border);
  }}
  .tree-link {{
    display: flex; align-items: center; gap: 5px;
    padding: 5px 8px; border-radius: 6px;
    font-size: 12px; color: var(--text); text-decoration: none;
    transition: background 0.12s;
  }}
  .tree-link:hover {{ background: var(--bg); }}
  .tree-link-error {{ color: #dc2626; }}
  .tree-link-error:hover {{ background: #fef2f2; }}
  .tree-link-leaf {{ color: var(--gray); }}
  .tree-link-leaf:hover {{ background: #f9fafb; }}
  .tree-error-dot {{ font-size: 10px; flex-shrink: 0; }}
  .tree-leaf {{
    display: block; padding: 4px 8px;
    font-size: 11px; color: var(--gray); font-style: italic;
  }}
  .tree-chip {{
    font-size: 9px; padding: 1px 4px; border-radius: 3px; flex-shrink: 0;
    background: #e0e7ff; color: #4338ca;
  }}

  /* ── Header ── */
  .header {{ margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .subtitle {{ font-size: 13px; color: var(--muted); }}

  /* ── Tree connectors (right panel) ── */
  .tree-item {{ position: relative; }}
  .tree-children {{
    margin-left: 20px;
    padding-left: 28px;
    border-left: 2px solid var(--border);
  }}
  .tree-children > .tree-item {{ position: relative; }}
  .tree-children > .tree-item::before {{
    content: "";
    position: absolute;
    left: -28px;
    top: 32px;
    width: 28px;
    height: 2px;
    background: var(--border);
  }}

  /* ── Page card ── */
  .page-card {{
    background: var(--card); border-radius: var(--radius);
    border: 1px solid var(--border); margin-bottom: 16px;
    overflow: hidden;
  }}
  .page-card-error {{ border-color: #fca5a5; border-left: 3px solid #ef4444; }}
  .page-card-error .page-card-header {{ background: #fff5f5; }}
  .page-breadcrumb {{
    padding: 6px 20px;
    font-size: 11px; color: var(--muted);
    background: #f8fafc; border-bottom: 1px solid var(--border);
  }}
  .bc-sep {{ color: #cbd5e1; margin: 0 5px; }}
  .bc-current {{ color: var(--text); font-weight: 600; }}
  .page-card-header {{
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .page-card-header h2 {{ font-size: 15px; font-weight: 600; }}
  .type-badge {{
    font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 500;
    background: #dbeafe; color: #1d4ed8;
  }}
  .page-sig {{ font-size: 11px; color: var(--muted); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .via-tap {{ font-size: 11px; color: #7c3aed; background: #ede9fe; padding: 2px 7px; border-radius: 10px; white-space: nowrap; flex-shrink: 0; }}
  .page-card-desc {{ padding: 10px 20px; font-size: 13px; color: var(--muted); border-bottom: 1px solid var(--border); background: #f8fafc; }}

  /* ── Page body ── */
  .page-card-body {{ display: flex; gap: 0; }}
  .screenshot-col {{
    width: 260px; flex-shrink: 0; padding: 16px;
    border-right: 1px solid var(--border);
  }}
  .screenshot-col img {{
    width: 100%; border-radius: 8px; border: 1px solid var(--border);
    cursor: zoom-in; display: block;
  }}
  .legend {{ display: flex; gap: 12px; margin-top: 8px; font-size: 11px; color: var(--muted); justify-content: center; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; vertical-align: middle; margin-right: 3px; }}
  .info-col {{ flex: 1; min-width: 0; padding: 16px; display: flex; flex-direction: column; gap: 16px; }}

  /* ── Section labels ── */
  .section-label {{ font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}

  /* ── Tap list ── */
  .tap-list {{ display: flex; flex-direction: column; }}
  .tap-item {{ padding: 7px 2px; border-bottom: 1px solid var(--border); }}
  .tap-item:last-child {{ border-bottom: none; }}
  .tap-item-main {{ display: flex; align-items: center; gap: 8px; min-width: 0; }}
  .tap-item-detail {{
    margin-top: 4px; margin-left: 30px;
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  }}
  .tap-num {{ width: 22px; flex-shrink: 0; text-align: center; font-size: 11px; font-weight: 600; color: var(--muted); }}
  .tap-label {{ font-size: 13px; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .tap-nav {{ font-size: 12px; color: var(--green); flex-shrink: 0; }}
  .tap-none {{ font-size: 12px; color: var(--gray); flex-shrink: 0; }}
  .tap-identity {{
    font-size: 11px; padding: 1px 7px; border-radius: 8px; flex-shrink: 0;
  }}
  .tap-identity-new {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
  .tap-identity-dup {{ background: #fefce8; color: #a16207; border: 1px solid #fde68a; }}
  .tap-identity-overlay {{ background: #fdf4ff; color: #7e22ce; border: 1px solid #e9d5ff; }}
  .tap-identity-desc {{ font-size: 11px; color: var(--muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }}
  .tap-identity-scores {{ font-size: 11px; color: #94a3b8; font-family: monospace; flex-shrink: 0; }}
  .tap-preview-btn {{
    font-size: 11px; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border);
    background: var(--bg); color: var(--muted); cursor: pointer; flex-shrink: 0;
    transition: all 0.15s; white-space: nowrap; margin-left: auto;
  }}
  .tap-preview-btn:hover {{ background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; }}

  /* ── Identity JSON panel ── */
  .tap-identity {{ cursor: pointer; user-select: none; }}
  .tap-identity::after {{ content: " ▾"; font-size: 9px; opacity: 0.6; }}
  .tap-identity.open::after {{ content: " ▴"; }}
  .tap-json-panel {{
    display: none; margin: 6px 0 2px 30px;
    background: #f8fafc; border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; font-family: "SF Mono", "Fira Code", monospace; font-size: 11px;
    line-height: 1.7;
  }}
  .tap-json-panel.show {{ display: block; }}
  .jobj {{ display: flex; flex-direction: column; gap: 0; }}
  .jrow {{ padding-left: 14px; }}
  .jk {{ color: #7c3aed; }}
  .jpunct {{ color: #94a3b8; }}
  .jv-str {{ color: #16a34a; }}
  .jv-num {{ color: #2563eb; }}
  .jv-bool {{ color: #d97706; }}
  .jv-null {{ color: #94a3b8; font-style: italic; }}

  /* ── Error banner ── */
  .error-banner {{
    font-size: 12px; color: #991b1b;
    background: #fef2f2; border-bottom: 1px solid #fecaca;
  }}
  .error-title {{
    font-weight: 600; list-style: none;
    padding: 10px 20px;
    display: flex; align-items: center; gap: 6px; cursor: pointer;
  }}
  .error-title::-webkit-details-marker {{ display: none; }}
  .error-title::before {{ content: "▶"; font-size: 9px; transition: transform 0.15s; margin-right: 2px; }}
  details.error-banner[open] .error-title::before {{ transform: rotate(90deg); }}
  .error-meta {{ color: #b91c1c; margin-bottom: 8px; font-size: 11px; padding: 0 20px; }}
  .error-body {{ padding: 0 20px 12px; }}
  .error-body {{ display: flex; gap: 16px; align-items: flex-start; }}
  .error-screenshot {{ flex-shrink: 0; width: 120px; }}
  .error-screenshot img {{
    width: 100%; border-radius: 6px; border: 1px solid #fecaca;
    cursor: zoom-in; display: block;
  }}
  .error-screenshot-caption {{ font-size: 10px; color: #b91c1c; text-align: center; margin-top: 4px; }}
  .back-timeline {{ display: flex; flex-direction: column; gap: 4px; flex: 1; }}
  .back-step {{
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; padding: 4px 8px; border-radius: 6px;
    background: #fff7f7; border: 1px solid #fecaca;
  }}
  .back-step-ok {{ background: #f0fdf4; border-color: #bbf7d0; color: #166534; }}
  .back-step-retry {{ background: #fff7ed; border-color: #fed7aa; color: #9a3412; }}
  .back-num {{ width: 18px; height: 18px; border-radius: 50%; background: #fecaca; color: #991b1b; font-weight: 700; font-size: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
  .back-step-ok .back-num {{ background: #bbf7d0; color: #166534; }}
  .back-step-retry .back-num {{ background: #fed7aa; color: #c2410c; font-size: 12px; }}
  .back-strategy {{ font-weight: 600; width: 70px; flex-shrink: 0; color: #991b1b; }}
  .back-step-ok .back-strategy {{ color: #166534; }}
  .back-step-retry .back-strategy {{ color: #ea580c; }}
  .back-coords {{ color: #64748b; width: 72px; flex-shrink: 0; font-family: monospace; }}
  .back-result {{ flex: 1; color: #64748b; }}
  .back-score {{ font-family: monospace; color: #b91c1c; margin-left: auto; flex-shrink: 0; }}
  .back-step-ok .back-score {{ color: #166534; }}

  /* ── Flows ── */
  .flows-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .flow-item {{ padding: 8px 10px; background: #f0fdf4; border-radius: 8px; border: 1px solid #bbf7d0; }}
  .flow-target {{ font-size: 13px; font-weight: 600; color: #15803d; }}
  .flow-desc {{ font-size: 12px; color: #166534; margin-top: 2px; line-height: 1.4; }}

  /* ── Knowledge ── */
  details {{ border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  summary {{
    padding: 8px 12px; font-size: 12px; font-weight: 600; color: var(--muted);
    cursor: pointer; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px;
    background: #f8fafc;
  }}
  summary::before {{ content: "▶"; font-size: 9px; transition: transform 0.15s; }}
  details[open] summary::before {{ transform: rotate(90deg); }}
  .knowledge-body {{ padding: 12px; font-size: 12px; color: #475569; line-height: 1.7; white-space: pre-wrap; background: white; }}

  /* ── Modal ── */
  .modal {{
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.88); z-index: 1000;
    justify-content: center; align-items: center; gap: 16px;
  }}
  .modal.show {{ display: flex; }}
  #modal-simple {{ display: flex; align-items: center; gap: 16px; }}
  .modal-panel {{
    background: white; border-radius: 12px; padding: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    max-height: 90vh; border: 2px solid transparent;
  }}
  .modal-panel img {{ max-height: calc(90vh - 60px); max-width: 320px; border-radius: 8px; object-fit: contain; }}
  .modal-label {{ font-size: 11px; color: #64748b; text-align: center; max-width: 280px; }}
  .modal-close {{
    position: fixed; top: 20px; right: 24px; font-size: 28px; color: white;
    cursor: pointer; line-height: 1; opacity: 0.8; z-index: 1001;
  }}
  .modal-close:hover {{ opacity: 1; }}
  .tap-seq-btn {{ background: #ede9fe; border-color: #c4b5fd; color: #6d28d9; }}
  .tap-seq-btn:hover {{ background: #ddd6fe; border-color: #a78bfa; color: #5b21b6; }}
</style>
</head>
<body>

<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-title">{app_name}</div>
    {sidebar_items}
    <div class="sidebar-stats">
      <strong>{pages}</strong> 页面<br>
      <strong>{taps_probed}</strong> 点击测试<br>
      <span style="color:var(--green)">●</span> <strong>{navigated}</strong> 导航成功<br>
      <span style="color:var(--gray)">●</span> <strong>{no_change}</strong> 无变化
    </div>
    {warnings_html}
  </nav>

  <main class="main">
    <div class="header">
      <h1>{app_name}</h1>
      <div class="subtitle">Recon Report · {pages} 个页面 · {taps_probed} 次点击探测</div>
    </div>

    {pages_html}
  </main>
</div>

<!-- Modal -->
<div class="modal" id="modal" onclick="closeModal(event)">
  <div class="modal-close" onclick="document.getElementById('modal').classList.remove('show')">✕</div>
  <!-- Simple zoom mode -->
  <div id="modal-simple" style="display:none">
    <div class="modal-panel" id="modal-before">
      <img id="modal-before-img" src="">
      <div class="modal-label">操作前</div>
    </div>
    <div id="modal-arrow" style="font-size:28px;color:white;display:none">→</div>
    <div class="modal-panel" id="modal-after-panel" style="display:none">
      <img id="modal-after-img" src="">
      <div class="modal-label" id="modal-after-label"></div>
    </div>
  </div>
  <!-- Sequence mode -->
  <div id="modal-seq-wrap" style="display:none;overflow-x:auto;max-width:96vw;padding:8px">
    <div id="modal-seq" style="display:flex;align-items:flex-start;gap:12px;min-width:max-content"></div>
  </div>
</div>

<script>
function openModal(beforeSrc, afterSrc, label) {{
  document.getElementById('modal-simple').style.display = 'flex';
  document.getElementById('modal-seq-wrap').style.display = 'none';
  document.getElementById('modal-before-img').src = beforeSrc;
  var afterPanel = document.getElementById('modal-after-panel');
  var arrow = document.getElementById('modal-arrow');
  if (afterSrc) {{
    document.getElementById('modal-after-img').src = afterSrc;
    document.getElementById('modal-after-label').textContent = label || '操作后';
    afterPanel.style.display = '';
    arrow.style.display = '';
  }} else {{
    afterPanel.style.display = 'none';
    arrow.style.display = 'none';
  }}
  document.getElementById('modal').classList.add('show');
}}
function openSeq(key) {{
  var steps = (window._seqs || {{}})[key] || [];
  var seq = document.getElementById('modal-seq');
  seq.innerHTML = '';
  steps.forEach(function(step, i) {{
    if (i > 0) {{
      var arr = document.createElement('div');
      arr.style.cssText = 'font-size:22px;color:rgba(255,255,255,0.5);align-self:center;flex-shrink:0';
      arr.textContent = '→';
      seq.appendChild(arr);
    }}
    var panel = document.createElement('div');
    panel.className = 'modal-panel';
    if (step.is_retry) panel.style.borderColor = 'rgba(251,146,60,0.6)';
    else if (step.success === true) panel.style.borderColor = 'rgba(34,197,94,0.5)';
    else if (step.success === false) panel.style.borderColor = 'rgba(239,68,68,0.4)';
    var img = document.createElement('img');
    img.src = step.src;
    img.style.cursor = 'default';
    if (step.is_retry) img.style.opacity = '0.35';
    var lbl = document.createElement('div');
    lbl.className = 'modal-label';
    lbl.style.whiteSpace = 'nowrap';
    lbl.textContent = '步骤 ' + (i + 1) + (step.subtitle ? ': ' + step.subtitle : '');
    if (step.is_retry) lbl.style.color = '#fb923c';
    else if (step.success === true) lbl.style.color = '#4ade80';
    else if (step.success === false) lbl.style.color = '#f87171';
    panel.appendChild(img);
    panel.appendChild(lbl);
    seq.appendChild(panel);
  }});
  document.getElementById('modal-simple').style.display = 'none';
  document.getElementById('modal-seq-wrap').style.display = '';
  document.getElementById('modal').classList.add('show');
}}
function showTapResult(btn) {{
  var pageCard = btn.closest('.page-card');
  var annotatedImg = pageCard.querySelector('.screenshot-col img');
  openModal(annotatedImg.src, btn.dataset.after || null, btn.dataset.label);
}}
function closeModal(e) {{
  if (e.target === document.getElementById('modal')) {{
    document.getElementById('modal').classList.remove('show');
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') document.getElementById('modal').classList.remove('show');
}});
function toggleIdentity(id, chip) {{
  var panel = document.getElementById(id);
  var show = panel.classList.toggle('show');
  chip.classList.toggle('open', show);
}}
</script>
</body>
</html>
"""


def _slug(name: str) -> str:
    return re.sub(r"[^\w]", "-", name)


def _render_page_card_html(node: NavNode, path: list[str]) -> str:
    """Recursively render a page card and all its explored children."""
    if node.page is None:
        return ""

    page = node.page
    type_label = PAGE_TYPE_LABELS.get(page.page_type, page.page_type)
    slug = _slug(page.name)
    path_with_self = path + [page.title]

    # Breadcrumb: full path for any non-root page
    breadcrumb_html = ""
    if path:
        parts = ""
        for seg in path:
            parts += f'<span class="bc-seg">{seg}</span><span class="bc-sep">›</span>'
        parts += f'<span class="bc-current">{page.title}</span>'
        breadcrumb_html = f'<div class="page-breadcrumb">{parts}</div>'

    via_html = ""
    if node.via_tap:
        via_html = f'<span class="via-tap">← {node.via_tap}</span>'

    # Match flows to navigated taps by checking if tap.label appears in flow_description
    flow_by_tap: dict[int, ReconFlow] = {}
    used_flows: set[int] = set()
    for tap in page.taps:
        if not tap.navigated or not tap.label:
            continue
        for fi, flow in enumerate(page.flows):
            if fi not in used_flows and tap.label in flow.flow_description:
                flow_by_tap[tap.index] = flow
                used_flows.add(fi)
                break
    # Merged tap + flow table
    tap_rows = ""
    seq_scripts = ""
    for tap in page.taps:
        # Build identity chip (clickable → expands JSON panel)
        identity_html = ""
        identity_json_panel = ""
        ident = tap.identity
        if ident and tap.navigated:
            is_new = ident.get("is_new", True)
            phase = ident.get("phase", "")
            matched_name = ident.get("matched_name")
            json_id = f"ident-{_slug(page.name)}-{tap.index}"
            json_html = _render_identity_json(ident)
            identity_json_panel = f'<div class="tap-json-panel" id="{json_id}">{json_html}</div>'

            if is_new:
                form = _form_from_fingerprint(ident.get("fingerprint", ""))
                is_overlay = form in _OVERLAY_FORMS if form else False
                if is_overlay:
                    form_label = _FORM_LABELS.get(form or "", "弹窗页")
                    identity_html = (
                        f'<span class="tap-identity tap-identity-overlay"'
                        f' onclick="toggleIdentity(\'{json_id}\', this)">◈ {form_label}</span>'
                    )
                else:
                    phase_label = {"new_page": "新页面", "visual_shortcut": "新页面", "text_match": "新页面"}.get(phase, phase)
                    identity_html = (
                        f'<span class="tap-identity tap-identity-new"'
                        f' onclick="toggleIdentity(\'{json_id}\', this)">✦ {phase_label}</span>'
                    )
            else:
                phase_label = {"visual_shortcut": "视觉命中", "semantic_match": "语义命中", "text_match": "语义命中"}.get(phase, phase)
                identity_html = (
                    f'<span class="tap-identity tap-identity-dup"'
                    f' onclick="toggleIdentity(\'{json_id}\', this)">⟳ {phase_label}</span>'
                )

        if tap.navigated:
            seq_key = f"{_slug(page.name)}-{tap.index}"
            if len(tap.back_seq) > 1:
                import json as _json
                seq_json = _json.dumps(tap.back_seq)
                seq_scripts += f'_seqs[{_json.dumps(seq_key)}]={seq_json};\n'
                btn = (
                    f'<button class="tap-preview-btn tap-seq-btn"'
                    f" onclick=\"openSeq('{seq_key}')\">查看全过程 ({len(tap.back_seq)})</button>"
                )
            elif tap.after_url:
                tap_lbl = f"tap_{tap.index}: {tap.label}"
                btn = (
                    f'<button class="tap-preview-btn"'
                    f' data-label="{tap_lbl}" data-after="{tap.after_url}"'
                    f' onclick="showTapResult(this)">查看结果</button>'
                )
            else:
                btn = ""

            tap_rows += f"""
            <div class="tap-item">
              <div class="tap-item-main">
                <span class="tap-num">{tap.index}</span>
                <span class="tap-label" title="{tap.label}">{tap.label or "—"}</span>
                <span class="tap-nav">✓ 导航成功</span>
                {btn}
              </div>
              {"<div class='tap-item-detail'>" + identity_html + "</div>" if identity_html else ""}
              {identity_json_panel}
            </div>"""
        else:
            tap_rows += f"""
            <div class="tap-item">
              <div class="tap-item-main">
                <span class="tap-num">{tap.index}</span>
                <span class="tap-label" title="{tap.label}">{tap.label or "—"}</span>
                <span class="tap-none">— 无变化</span>
              </div>
            </div>"""

    nav_count = sum(1 for t in page.taps if t.navigated)
    tap_section = f"""
        <div>
          <div class="section-label">点击探测 ({nav_count}/{len(page.taps)} 导航成功)</div>
          <div class="tap-list">{tap_rows}</div>
        </div>"""

    flows_section = ""

    knowledge_section = ""
    if page.knowledge.strip():
        safe_knowledge = (
            page.knowledge
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        knowledge_section = f"""
            <details open>
              <summary>页面知识摘要</summary>
              <div class="knowledge-body">{safe_knowledge}</div>
            </details>"""

    card_cls = "page-card page-card-error" if page.error else "page-card"
    seq_init = f"<script>if(!window._seqs)window._seqs={{}};\n{seq_scripts}</script>" if seq_scripts else ""
    card_html = f"""
        {seq_init}<div class="{card_cls}" id="{slug}">
          {breadcrumb_html}
          <div class="page-card-header">
            <h2>{page.title}</h2>
            {f'<span class="type-badge">{type_label}</span>' if type_label and page.page_type != "leaf" else ''}
            {via_html}
          </div>
          {'<div class="page-card-desc">' + page.description + '</div>' if page.description else ''}
          {_render_error_html(page.error, page.error_annotated_url)}
          <div class="page-card-body">
            <div class="screenshot-col">
              <img src="{page.annotated_url}" onclick="openModal(this.src)" title="点击放大">
              <div class="legend">
                <span><span class="legend-dot" style="background:var(--green)"></span>导航成功</span>
                <span><span class="legend-dot" style="background:var(--gray)"></span>无变化</span>
              </div>
            </div>
            <div class="info-col">
              {tap_section}
              {flows_section}
              {knowledge_section}
            </div>
          </div>
        </div>"""

    # Recursively render explored children
    children_html = "".join(
        _render_page_card_html(child, path_with_self)
        for child in node.children
        if child.page is not None
    )
    children_section = f'<div class="tree-children">{children_html}</div>' if children_html else ""

    return f'<div class="tree-item">{card_html}{children_section}</div>'


def generate_recon_html(data: AppReconData) -> str:
    roots = _build_nav_tree(data.pages, trace=data.trace)
    tree_html = _render_tree_html(roots)
    sidebar_items = f'<ul class="nav-tree">{tree_html}</ul>'

    pages_html = "".join(_render_page_card_html(root, []) for root in roots)

    # Build warnings HTML
    warnings_html = ""
    if data.dup_warnings:
        items = ""
        for w in data.dup_warnings:
            sims = ""
            if w.get("text_sim") is not None:
                sims += f' text_sim={w["text_sim"]:.3f}'
            if w.get("visual_sim") is not None:
                sims += f' visual_sim={w["visual_sim"]:.3f}'
            safe_title = w["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_parent = w["parent"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_label = w["label"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            items += (
                f'<div class="warn-item">'
                f'<div class="warn-title">⊘ {safe_title}</div>'
                f'<div class="warn-detail">← {safe_parent} · [{safe_label}]{sims}</div>'
                f'</div>'
            )
        warnings_html = (
            f'<details class="sidebar-warnings">'
            f'<summary class="sidebar-warnings-title">⚠ {len(data.dup_warnings)} 个重复页</summary>'
            f'{items}'
            f'</details>'
        )

    return RECON_HTML_TEMPLATE.format(
        app_name=data.app_name,
        sidebar_items=sidebar_items,
        warnings_html=warnings_html,
        pages_html=pages_html,
        **data.stats,
    )
