"""Browser mechanics for moving one already-bound collection surface."""

from __future__ import annotations

import json
from typing import Any


def move_collection(session: Any, table: dict, family: str) -> bool:
    """Move only the pager/scroll container represented by ``table``."""
    client = getattr(session, "client", None)
    cdp = getattr(client, "_cdp_send", None)
    path = str(table.get("path") or "").strip()
    traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
    if not callable(cdp) or not path:
        return False
    forward = family in {"paginate_next", "scroll_forward", "load_more"}
    expression = f"""(() => {{
      let surface;
      try {{ surface = document.querySelector({json.dumps(path)}); }} catch (_) {{ return false; }}
      if (!surface) return false;
      const disabled = (el) => !!(el && (
        el.disabled || el.matches('[disabled],[aria-disabled="true"],.disabled,[class*="disabled" i]')
      ));
      if ({json.dumps(traversal.get("type"))} === 'paged') {{
        const pager = ['.pager','.pagination','.pages','.page-numbers',
          '.data-grid-paginator','.admin__data-grid-pager',
          'nav[aria-label*="pagination" i]'].join(',');
        const button = {json.dumps(forward)}
          ? '[aria-label*="next page" i],.next-page,.action-next,button[title*="Next" i],button[class*="next" i]'
          : '[aria-label*="previous page" i],.prev-page,.action-previous,button[title*="Previous" i],button[class*="previous" i]';
        for (let root = surface, depth = 0; root && depth < 7; root = root.parentElement, depth++) {{
          const el = root.querySelector(pager)?.querySelector(button);
          if (el && !disabled(el)) {{ el.click(); return true; }}
        }}
        return false;
      }}
      for (let root = surface, depth = 0; root && depth < 6; root = root.parentElement, depth++) {{
        if (root.scrollHeight > root.clientHeight + 2) {{
          root.scrollBy({{top: root.clientHeight * ({json.dumps(forward)} ? 1 : -1), behavior: 'instant'}});
          return true;
        }}
      }}
      return false;
    }})()"""
    try:
        result = cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        moved = bool(((result or {}).get("result") or {}).get("value"))
        if moved:
            settle = getattr(client, "wait_settled", None)
            if callable(settle):
                settle("navigate" if family.startswith("paginate") else "scroll")
        return moved
    except Exception:
        return False


def validate_collection_action(session: Any, table: dict, decision: object, family: str) -> bool:
    """Mechanically keep React fallback actions on the bound traversal affordance."""
    action = getattr(decision, "action", None)
    action_type = str(getattr(action, "action_type", ""))
    allowed = {
        "paginate_next": {"tap"},
        "paginate_prev": {"tap"},
        "load_more": {"tap"},
        "scroll_forward": {"scroll", "drag"},
        "scroll_backward": {"scroll", "drag"},
    }
    if action_type not in allowed.get(family, set()):
        return False
    x, y = getattr(action, "x", None), getattr(action, "y", None)
    path = str(table.get("path") or "").strip()
    client = getattr(session, "client", None)
    cdp = getattr(client, "_cdp_send", None)
    viewport = getattr(client, "viewport_size", None)
    viewport = viewport() if callable(viewport) else viewport
    if not path or not callable(cdp) or not viewport or x is None or y is None:
        return False
    px, py = float(x) / 1000 * viewport[0], float(y) / 1000 * viewport[1]
    expression = f"""(() => {{
      let surface;
      try {{ surface = document.querySelector({json.dumps(path)}); }} catch (_) {{ return false; }}
      const el = document.elementFromPoint({px}, {py});
      if (!surface || !el) return false;
      const inside = surface.contains(el) || !!el.closest('.pager,.pagination,.pages,.data-grid-paginator,.admin__data-grid-pager');
      if (!inside) return false;
      if ({json.dumps(action_type)} !== 'tap') return true;
      return !!el.closest('button,a,[role="button"],.action-next,.action-previous,.next-page,.prev-page');
    }})()"""
    try:
        result = cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return bool(((result or {}).get("result") or {}).get("value"))
    except Exception:
        return False


__all__ = ["move_collection", "validate_collection_action"]
