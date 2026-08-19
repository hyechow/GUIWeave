"""Read-only DOM form-control snapshots for browser perception."""

from __future__ import annotations

import re
from typing import Any

MAX_CONTROLS = 40
# Reserved slots for rendered-but-off-screen controls when the total exceeds MAX_CONTROLS, so a
# large in-viewport set can't fully evict off-viewport fields the planner needs to scroll toward.
OFF_VIEWPORT_RESERVE = 8
MAX_OPTIONS = 30
MAX_TEXT = 80
_COMMIT_LABEL_RE = re.compile(
    r"^(?:save|submit|update|publish)\b|^(?:保存|提交|更新|发布)",
    re.IGNORECASE,
)
_COMMIT_CONTROL_KINDS = frozenset({"button", "submit"})
# Generic navigation vocabulary (language-level, not app-level). Commit/reset/filter/
# query markers always win: "Save and Next" commits, it does not navigate.
# The emitted ``record_*`` / ``page_*`` values are consumed by the deterministic
# record-walk driver (core/tool_agent/record_walk.py) and the runtime walk gate;
# renaming them here breaks both.
_RECORD_NAV_LABEL_RE = re.compile(
    r"^(?:next|previous|prev)$|^(?:下一条|上一条|下一條|上一條)$",
    re.IGNORECASE,
)
_PAGE_NAV_LABEL_RE = re.compile(
    r"^(?:(?:next|previous|prev)\s+page|page\s+(?:next|previous|prev))$"
    r"|^(?:下一页|上一页|下一頁|上一頁)$",
    re.IGNORECASE,
)
_TRAVERSAL_CONTROL_KINDS = frozenset({"button", "a"})
_FIELD_KINDS = frozenset({
    "text_input",
    "textarea",
    "native_select",
    "select",
    "selectmenu",
    "checkbox_input",
    "radio_input",
    "radio_group",
    "rating",
    "rich_textarea",
    "number_input",
    "number",
})
_CHOICE_OP_LABELS = frozenset({
    "deselectall",
    "unselectall",
    "clearall",
    "selectall",
    "取消全选",
    "全部取消",
    "全选",
})
_OFFSCREEN_UNIT_RANK = 6


def _alnum_key(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def rating_selected_scale(value: object, options: list[str] | None) -> str:
    """Map a radio option id onto a 1–N scale using sorted numeric values.

    Star widgets often render highest-first and store opaque option ids. DOM order
    is not the scale; the smallest numeric option is 1 and the largest is N.
    """

    numbers: list[int] = []
    for option in options or []:
        text = str(option).strip()
        if not text.lstrip("-").isdigit():
            return ""
        numbers.append(int(text))
    if len(numbers) < 2:
        return ""
    try:
        current = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    ordered = sorted(set(numbers))
    if current not in ordered:
        return ""
    return str(ordered.index(current) + 1)


def _is_choice_operation(control: dict[str, Any]) -> bool:
    kind = str(control.get("kind") or "")
    if kind not in {"button", "a"}:
        return False
    return _alnum_key(control.get("label") or control.get("value")) in _CHOICE_OP_LABELS


def traversal_action_of(control: dict[str, Any]) -> str | None:
    """Semantic traversal role for a normalized control, else None.

    Returns one of ``record_next`` / ``record_previous`` / ``page_next`` /
    ``page_previous``. Record-level roles walk sibling records of an editor
    surface; page-level roles walk list windows. Commit/reset/filter/query
    controls are never traversal: "Save and Next" commits, it does not
    navigate.
    """

    if str(control.get("kind") or "") not in _TRAVERSAL_CONTROL_KINDS:
        return None
    if (
        control.get("form_action")
        or control.get("query_action")
        or control.get("is_filter")
    ):
        return None
    labels = tuple(filter(None, (
        re.sub(r"\s+", " ", str(control.get(key) or "")).strip()
        for key in ("label", "value")
    )))
    if not labels:
        return None
    navigation_label = next(
        (label for label in labels if _PAGE_NAV_LABEL_RE.match(label)), "",
    )
    scope = "page" if navigation_label else "record"
    navigation_label = navigation_label or next(
        (label for label in labels if _RECORD_NAV_LABEL_RE.match(label)), "",
    )
    if not navigation_label:
        return None
    direction = (
        "previous"
        if re.search(r"(?:previous|prev|上)", navigation_label, re.IGNORECASE)
        else "next"
    )
    return f"{scope}_{direction}"


def form_controls_js() -> str:
    """Return a self-contained JS expression that serializes visible form controls."""
    return r"""
(() => {
  const clean = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();
  const cut = (s, n = 120) => {
    s = clean(s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };
  const rendered = (el) => {
    // 元素在 DOM 中实际渲染(有尺寸、非 display:none/visibility:hidden),不限视口位置。
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const intersectsViewport = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom >= 0 && r.right >= 0
      && r.top <= (innerHeight || document.documentElement.clientHeight)
      && r.left <= (innerWidth || document.documentElement.clientWidth);
  };
  // Vertical position of an off-viewport control relative to the current viewport, so the planner
  // knows WHICH WAY to scroll instead of guessing (guessing "down" sent 702 chasing the Websites
  // field that had scrolled ABOVE the viewport). 'above' = need to scroll up, 'below' = down.
  const viewportPos = (el) => {
    const r = el.getBoundingClientRect();
    const vh = innerHeight || document.documentElement.clientHeight;
    if (r.bottom < 0) return 'above';
    if (r.top > vh) return 'below';
    return 'in';
  };
  const viewState = (el) => {
    const r = el.getBoundingClientRect();
    const vw = innerWidth || document.documentElement.clientWidth;
    const vh = innerHeight || document.documentElement.clientHeight;
    if (!intersectsViewport(el)) {
      return {in_viewport: false, viewport_pos: viewportPos(el)};
    }
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < 0 || cx >= vw || cy < 0 || cy >= vh) {
      return {
        in_viewport: false,
        viewport_pos: cy < 0 ? 'above' : cy >= vh ? 'below' : (cx < vw / 2 ? 'above' : 'below'),
      };
    }
    const hit = document.elementFromPoint(cx, cy);
    const exposed = Boolean(hit && (hit === el || el.contains(hit)));
    if (exposed) return {in_viewport: true, viewport_pos: 'in'};
    // Sticky headers and fixed toolbars can geometrically overlap a row while hiding its center.
    // Such a control is not actionable from the screenshot.  Mark it toward the nearest vertical
    // edge so the visual policy reveals it before clicking instead of trusting stale geometry.
    return {
      in_viewport: false,
      viewport_pos: cy < vh / 2 ? 'above' : 'below',
      occluded: true,
    };
  };
  // 收录所有已「渲染」的控件,不按视口位置丢弃,并给每个控件标 in_viewport。长表单(如 Cart
  // Price Rule)里 Rule Name 在顶、Discount/Save 在底,滚到中部时首尾控件滑出视口 —— 旧的视口
  // 过滤会把它们从清单里删掉,feasibility 于是误判「控件不存在」而放弃(WebArena 702 Rule Name /
  // 703 discount_amount),planner 也不知道该滚过去。改为报全部已渲染控件 + in_viewport 标志,由消费
  // 方决定是否先滚动。原 keepForRead 只把 select/textarea 豁免视口(Material below-fold),现统一到全部。
  const labelFromContainer = (el) => {
    // Decorative/auxiliary text (error messages, input prefixes/suffixes, hints) never names the
    // control — skip it so the first REAL label in the container wins.
    const decorative = (lbl) => {
      if (!lbl) return true;
      if (clean(lbl.getAttribute('role')) === 'alert') return true;
      const cls = clean(lbl.className || '').toLowerCase();
      return /error|prefix|suffix|hint|note|addon/.test(cls);
    };
    const boxes = [
      el.closest('.field'),
      el.closest('[class*=field]'),
      el.parentElement,
      el.parentElement && el.parentElement.parentElement,
    ].filter(Boolean);
    for (const box of boxes) {
      for (const lbl of Array.from(box.querySelectorAll('label,.label,[data-label]'))) {
        if (decorative(lbl)) continue;
        const text = clean(lbl.innerText || lbl.textContent || lbl.getAttribute('data-label'));
        if (text) return text;
      }
    }
    return '';
  };
  const rowSemanticLabelOf = (el) => {
    const inputType = clean(el.getAttribute('type') || el.type || '').toLowerCase();
    if (!['checkbox', 'radio'].includes(inputType)) return '';
    const row = el.closest('tr,[role="row"],[data-role="row"]');
    if (!row) return '';
    const texts = Array.from(row.querySelectorAll('td,[role="gridcell"]'))
      .filter(cell => !cell.contains(el))
      .map(cell => clean(cell.innerText || cell.textContent || cell.getAttribute('data-label')))
      .filter(Boolean);
    const semantic = [];
    const seen = new Set();
    for (const text of texts) {
      const key = text.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      semantic.push(text);
      // The leading identity cells are more useful than trailing Yes/No metadata.
      if (semantic.length >= 2) break;
    }
    return cut(semantic.join(' '), 80);
  };
  const rowValuesOf = (el) => {
    const row = el.closest('tr,[role="row"],[data-role="row"]');
    if (!row) return [];
    const values = [];
    const seen = new Set();
    for (const cell of Array.from(row.querySelectorAll(':scope > td,:scope > [role="gridcell"]'))) {
      const text = cut(cell.innerText || cell.textContent || cell.getAttribute('data-label'), 80);
      const key = text.toLowerCase();
      if (!text || seen.has(key)) continue;
      seen.add(key);
      values.push(text);
      if (values.length >= 8) break;
    }
    return values;
  };
  const labelOf = (el) => {
    if (el.labels && el.labels.length) {
      const text = clean(Array.from(el.labels).map(l => l.innerText || l.textContent).join(' '));
      if (text) return text;
    }
    if (el.id) {
      const css = window.CSS && CSS.escape ? CSS.escape(el.id) : el.id.replace(/"/g, '\\"');
      const lbl = document.querySelector(`label[for="${css}"]`);
      const text = clean(lbl && (lbl.innerText || lbl.textContent));
      if (text) return text;
    }
    const direct = clean(el.getAttribute('aria-label') || el.getAttribute('title'));
    if (direct) return direct;
    // Grid selection controls often have only generated IDs (for example idscheck144), while
    // the record identity lives in sibling cells.  Surface that row text as the control's visual
    // name so generic enhanced grounding can distinguish adjacent checkboxes.
    const rowSemantic = rowSemanticLabelOf(el);
    if (rowSemantic) return rowSemantic;
    const role = clean(el.getAttribute('role')).toLowerCase();
    if (el.tagName === 'BUTTON' || el.tagName === 'A' || ['button', 'link', 'option'].includes(role)) {
      const ownText = clean(el.innerText || el.textContent);
      if (ownText) return ownText;
    }
    const container = labelFromContainer(el);
    if (container) return container;
    const prev = el.previousElementSibling || (el.parentElement && el.parentElement.previousElementSibling);
    const prevText = clean(prev && (prev.innerText || prev.textContent));
    if (prevText && prevText.length <= 80) return prevText;
    return clean(el.getAttribute('placeholder') || el.name || el.id);
  };
  // Radio groups (including visually hidden star/score widgets) share a name and a
  // field label. The per-option label is often just "1"/"2"; walk out to the field
  // legend so collectors can bind the selected value instead of counting painted icons.
  const radioGroupLabelOf = (el) => {
    const generic = (s) => /^(rating|score|stars?|\d+)$/i.test(clean(s));
    const texts = [];
    let node = el;
    for (let depth = 0; depth < 8 && node; depth += 1, node = node.parentElement) {
      const fromBox = labelFromContainer(node);
      if (fromBox) texts.push(fromBox);
      if (node.tagName === 'FIELDSET') {
        const legend = node.querySelector(':scope > legend');
        const legendText = clean(legend && (legend.innerText || legend.textContent));
        if (legendText) texts.push(legendText);
      }
    }
    const unique = [];
    const seen = new Set();
    for (const text of texts) {
      const key = text.toLowerCase();
      if (!text || seen.has(key)) continue;
      seen.add(key);
      unique.push(text);
    }
    return unique.find((text) => !generic(text)) || unique[0] || '';
  };
  const gridHeaderLabelOf = (el) => {
    const table = el.closest('table');
    if (!table) return '';
    const er = el.getBoundingClientRect();
    const cx = er.left + er.width / 2;
    const candidates = Array.from(table.querySelectorAll('th')).map(th => {
      const r = th.getBoundingClientRect();
      const text = clean(th.innerText || th.textContent || '')
        .replace(/[↑↓↕]+/g, '')
        .replace(/\s+/g, ' ')
        .trim();
      return {th, r, text};
    }).filter(c =>
      c.text && c.r.width > 0 && c.r.height > 0
      && c.r.left - 2 <= cx && cx <= c.r.right + 2
      && c.r.bottom <= er.top + 4
      && !c.th.querySelector('input,select,textarea')
    );
    candidates.sort((a, b) => b.r.bottom - a.r.bottom);
    return candidates[0] ? candidates[0].text : '';
  };
  const optionRowOf = (el) => {
    // Repeated option rows without table markup (choice lists): the row is the nearest ancestor
    // whose same-tag siblings each hold a checkbox/radio of their own. Structural shape, not
    // class vocabulary.
    const type = clean(el.getAttribute('type') || el.type || '').toLowerCase();
    if (!['checkbox', 'radio'].includes(type)) return null;
    for (let node = el.parentElement, depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
      const parent = node.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
      if (
        siblings.length >= 2
        && siblings.every((child) => child.querySelector('input[type="checkbox"],input[type="radio"]'))
      ) return node;
    }
    return null;
  };
  const repeatedGroupOf = (el) => {
    // Preserve field association inside repeated form collections. Flat control text cannot tell
    // whether a value belongs to the intended column of an option/line-item row. This is generic
    // DOM structure (table/grid/repeated option rows), not an application vocabulary.
    const row = el.closest('tr,[role="row"],[data-role="row"]') || optionRowOf(el);
    if (!row) return null;
    const owner = row.closest('table,[role="grid"],fieldset,[role="group"]');
    const siblings = row.parentElement ? Array.from(row.parentElement.children) : [];
    const index = Math.max(0, siblings.indexOf(row));
    const ownerKey = clean(
      (owner && (owner.id || owner.getAttribute('data-index') || owner.getAttribute('data-role')))
      || (row.parentElement && row.parentElement.id)
      || 'collection'
    );
    const field = gridHeaderLabelOf(el) || labelOf(el);
    return {
      id: cut(`${ownerKey}:${index}`, 80),
      index,
      field: cut(field, 80),
    };
  };
  const requiredOf = (el) => {
    const own = clean(el.getAttribute('aria-required')).toLowerCase();
    const dataValidate = clean(el.getAttribute('data-validate')).toLowerCase();
    // Class-based "required" markers: the word is language-level UI vocabulary; exclude
    // negations ("not-required") so they can't false-positive.
    const markedRequired = (node) => {
      const cls = clean((node && node.className) || '').toLowerCase();
      return /(?:^|[-_\s])required(?:[-_\s]|$)/.test(cls) && !/(?:^|[-_\s])not[-_]required/.test(cls);
    };
    const holder = el.closest('[aria-required="true"],[data-validate*="required"],[class*="required"]');
    const holderRequired = Boolean(holder) && (
      clean(holder.getAttribute('aria-required')).toLowerCase() === 'true'
      || clean(holder.getAttribute('data-validate')).toLowerCase().includes('required')
      || markedRequired(holder)
    );
    return Boolean(
      el.required
      || own === 'true'
      || dataValidate.includes('required')
      || markedRequired(el)
      || holderRequired
    );
  };
  const kindOf = (el) => {
    const tag = el.tagName;
    const role = clean(el.getAttribute('role')).toLowerCase();
    const dataRole = clean(el.getAttribute('data-role')).toLowerCase();
    const rowRoute = clean(
      el.getAttribute('data-href')
      || el.getAttribute('data-url')
      || el.getAttribute('title')
    );
    if (
      (tag === 'TR' || role === 'row' || dataRole === 'row')
      && (el.hasAttribute('onclick') || /^(?:https?:\/\/|\/)/i.test(rowRoute))
    ) return 'clickable_row';
    if (tag === 'SELECT') return 'native_select';
    if (tag === 'TEXTAREA') return 'textarea';
    if (role === 'combobox') return 'aria_combobox';
    if (role === 'listbox') return 'aria_listbox';
    // selectmenu-style widget (widget-family vocabulary, like "datepicker"): a readonly display
    // <input> whose options render as a clickable list. Recognize it as an option selector, not
    // a text field — otherwise the planner types into the display box and never fires the
    // widget's selection handler (task 63 per-page control).
    if (tag === 'INPUT' && el.closest('[class*="selectmenu" i]')) return 'selectmenu';
    if (tag === 'INPUT') return (el.type || 'text').toLowerCase() + '_input';
    return role || tag.toLowerCase();
  };
  const rectOf = (el) => {
    const r = el.getBoundingClientRect();
    const w = innerWidth || document.documentElement.clientWidth || 1;
    const h = innerHeight || document.documentElement.clientHeight || 1;
    return {
      x: Math.round((r.left + r.width / 2) / w * 1000),
      y: Math.round((r.top + r.height / 2) / h * 1000),
      w: Math.round(r.width),
      h: Math.round(r.height),
    };
  };
  const controls = [];
  const sectionControls = [];
  const statusControls = [];
  const rawControlLimit = 500;
  let totalRendered = 0;
  let rawLimitHit = false;
  const seenElements = new Set();
  const seenKeys = new Set();
  const pushControl = (item, bucket = controls) => {
    const r = item.rect || {};
    const key = [item.kind, item.label, item.name, item.id, r.x, r.y, r.w, r.h].join('|');
    if (seenKeys.has(key)) return;
    seenKeys.add(key);
    bucket.push(item);
  };
  // Page-level feedback is part of the post-action state even when it sits above the current
  // viewport.  Expose common ARIA and notification widgets as read-only controls so an agent
  // cannot mistake a dispatched submit for a successful mutation while an off-screen error is
  // present.  Keep this browser-widget based; no application vocabulary is encoded here.
  const statusSelector = [
    '[role="alert"]',
    '[role="status"]',
    '.message-error',
    '.message-success',
    '.message-warning',
    '.message-notice',
    '.alert-error',
    '.alert-success',
    '.alert-warning',
    '.notification-error',
    '.notification-success',
  ].join(',');
  const statusCandidates = Array.from(document.querySelectorAll(statusSelector));
  for (const el of statusCandidates) {
    if (!rendered(el)) continue;
    // Prefer the innermost matching message to avoid emitting the same text for a wrapper and
    // its child notification element.
    if (Array.from(el.children || []).some(child => child.matches && child.matches(statusSelector))) {
      continue;
    }
    const text = cut(el.innerText || el.textContent || el.getAttribute('aria-label'), 120);
    if (!text) continue;
    const cls = clean(el.className || '').toLowerCase();
    const severity = (
      cls.includes('error') ? 'error'
      : cls.includes('success') ? 'success'
      : cls.includes('warning') ? 'warning'
      : 'status'
    );
    totalRendered += 1;
    pushControl({
      label: cut(`${severity}: ${text}`, 120),
      kind: 'status_message',
      value: text,
      rect: rectOf(el),
      ...viewState(el),
    }, statusControls);
  }
  const expandedState = (el) => {
    if (el.tagName === 'SUMMARY' && el.parentElement && el.parentElement.tagName === 'DETAILS') {
      return el.parentElement.open ? 'true' : 'false';
    }
    const attr = clean(el.getAttribute('aria-expanded'));
    if (attr) return attr.toLowerCase();
    const holder = el.closest('[aria-expanded],details,[class*="collapsible"],[class*="accordion"],[class*="fieldset-wrapper"]');
    if (holder && holder !== el) {
      if (holder.tagName === 'DETAILS') return holder.open ? 'true' : 'false';
      const holderAttr = clean(holder.getAttribute('aria-expanded'));
      if (holderAttr) return holderAttr.toLowerCase();
    }
    const sectionTitle = el.closest('.fieldset-wrapper-title') || el;
    const wrapper = sectionTitle.closest('.fieldset-wrapper,[class*="collapsible"],[class*="accordion"]');
    if (wrapper) {
      const content = Array.from(wrapper.children || []).find(child => {
        if (child === sectionTitle) return false;
        const childCls = clean(child.className || '').toLowerCase();
        return childCls.includes('fieldset-wrapper-content') || childCls.includes('collapsible-content');
      });
      if (content) {
        const cr = content.getBoundingClientRect();
        const cst = getComputedStyle(content);
        const contentCls = clean(content.className || '').toLowerCase();
        if (
          contentCls.includes('_hide')
          || /\b(closed|collapsed|hide|hidden)\b/.test(contentCls)
          || cst.visibility === 'hidden'
          || cst.display === 'none'
          || cr.height <= 1
        ) return 'false';
        if (
          contentCls.includes('_show')
          || /\b(open|active|expanded|show)\b/.test(contentCls)
          || cr.height > 1
        ) return 'true';
      }
    }
    const cls = clean([el.className || '', holder && holder.className || ''].join(' ')).toLowerCase();
    if (cls.includes('_show') || /\b(open|active|expanded|show)\b/.test(cls)) return 'true';
    if (cls.includes('_hide') || /\b(closed|collapsed|hide|hidden)\b/.test(cls)) return 'false';
    return '';
  };
  const richEditorLabelOf = (el) => {
    const id = clean(el.id || '');
    const candidates = [];
    if (id) {
      candidates.push(id.replace(/_ifr$/i, '').replace(/_iframe$/i, ''));
      candidates.push(id);
    }
    for (const candidate of candidates) {
      if (!candidate) continue;
      const css = window.CSS && CSS.escape ? CSS.escape(candidate) : candidate.replace(/"/g, '\\"');
      const lbl = document.querySelector(`label[for="${css}"]`);
      const labelText = clean(lbl && (lbl.innerText || lbl.textContent));
      if (labelText) return labelText;
      const source = document.getElementById(candidate) || document.querySelector(`[name="${css}"]`);
      const sourceLabel = source && labelFromContainer(source);
      if (sourceLabel) return sourceLabel;
    }
    return labelOf(el);
  };
  const richEditorValue = (el) => {
    try {
      if (el.tagName === 'IFRAME' && el.contentDocument && el.contentDocument.body) {
        return el.contentDocument.body.innerText || el.contentDocument.body.textContent || '';
      }
    } catch (e) {}
    return el.innerText || el.textContent || '';
  };
  // Generic section/accordion affordances. These are not editable fields, but they are the
  // deterministic way to acquire fields hidden in collapsed sections. Keep this browser-widget
  // level: ARIA/summary plus common data-role/class patterns, with no site/task vocabulary.
  const sectionSelector = [
    'button[aria-expanded]',
    '[role="button"][aria-expanded]',
    'summary',
    '[data-role="title"]',
    '[class*="accordion"][class*="title"]',
    '[class*="collapsible"][class*="title"]',
    '[class*="fieldset-wrapper-title"]',
  ].join(',');
  for (const el of Array.from(document.querySelectorAll(sectionSelector))) {
    if (seenElements.has(el) || !rendered(el)) continue;
    seenElements.add(el);
    const text = cut(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || el.textContent, 80);
    if (!text) continue;
    totalRendered += 1;
    if (sectionControls.length + controls.length >= rawControlLimit) {
      rawLimitHit = true;
      continue;
    }
    pushControl({
      label: text,
      kind: 'section_toggle',
      name: cut(el.getAttribute('name') || '', 80),
      id: cut(el.id || '', 80),
      value: expandedState(el),
      rect: rectOf(el),
      ...viewState(el),
    }, sectionControls);
  }
  const selector = [
    'input', 'select', 'textarea', 'button', 'a[href]',
    '[role=button]', '[role=link]', '[role=combobox]', '[role=listbox]', '[role=option]',
    'tr[onclick]', '[role=row][onclick]', '[data-role=row][onclick]',
    'tr[data-href]', 'tr[data-url]',
    '[role=row][data-href]', '[role=row][data-url]',
  ].join(',');
  for (const el of Array.from(document.querySelectorAll(selector))) {
    if (seenElements.has(el) || !rendered(el)) continue;
    seenElements.add(el);
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') continue;
    totalRendered += 1;
    if (sectionControls.length + controls.length >= rawControlLimit) {
      rawLimitHit = true;
      continue;
    }
    const kind = kindOf(el);
    const inlineHandler = clean(el.getAttribute('onclick') || '');
    const dataAction = clean(el.getAttribute('data-action') || '');
    const queryAction = (
      /\bresetfilter\s*\(/i.test(inlineHandler)
      || /filter[-_].*(reset|clear)|(reset|clear).*filter/i.test(dataAction)
    )
      ? 'reset'
      : (
        /\bdofilter\s*\(/i.test(inlineHandler)
        || /filter[-_].*(apply|submit)|(apply|submit).*filter/i.test(dataAction)
      )
      ? 'submit'
      : '';
    // Filter-region detection: a container named for filtering (data-role/data-part/class token
    // "filter") that hosts controls but NOT the result grid itself. The grid guard keeps results
    // panels and page-level wrappers from marking every control inside them as a filter.
    const filterRegion = el.closest('[data-role*="filter" i],[data-part*="filter" i],[class*="filter" i]');
    const inFilterRegion = Boolean(filterRegion) && !filterRegion.querySelector('table,[role="grid"]');
    const isFilter = el.id.includes('_filter_') || inFilterRegion;
    const isDatepicker = Boolean(el.matches && el.matches('[class*="datepicker" i],[data-provide="datepicker"]'));
    const label = isFilter ? (gridHeaderLabelOf(el) || labelOf(el)) : labelOf(el);
    const item = {
      label: cut(label, 80),
      kind,
      name: cut(el.getAttribute('name') || '', 80),
      id: cut(el.id || '', 80),
      placeholder: cut(el.getAttribute('placeholder') || '', 80),
      value: '',
      selected_text: '',
      focused: el === document.activeElement,
      rect: rectOf(el),
      ...viewState(el),
    };
    const repeatedGroup = kind === 'clickable_row' ? null : repeatedGroupOf(el);
    if (repeatedGroup) {
      item.group_id = repeatedGroup.id;
      item.group_index = repeatedGroup.index;
      item.group_field = repeatedGroup.field;
    }
    if (kind === 'clickable_row') {
      item.row_values = rowValuesOf(el);
      item.label = item.row_values[0] || item.label;
    }
    if (isFilter) item.is_filter = true;
    if (queryAction) item.query_action = queryAction;
    const inputType = clean(el.getAttribute('type') || el.type || '').toLowerCase();
    const formRole = clean(
      el.getAttribute('data-form-role')
      || el.getAttribute('data-action')
      || ''
    ).toLowerCase();
    if (
      !isFilter
      && !queryAction
      && (
        inputType === 'submit'
        || formRole === 'save'
        || formRole === 'submit'
      )
    ) item.form_action = 'commit';
    if (isDatepicker) item.is_datepicker = true;
    if (requiredOf(el)) item.required = true;
    if (el.tagName === 'SELECT') {
      const opts = Array.from(el.options || []);
      const selOpts = Array.from(el.selectedOptions || []);
      const selTexts = selOpts.map(o => o.textContent || o.label || o.value).filter(Boolean);
      item.value = cut(el.value, 80);
      item.selected_text = cut(selTexts.join(', '), 80);
      item.selected_text_primary = cut(selTexts[0] || '', 80);
      item.options = opts.map(o => cut(o.textContent || o.label || o.value, 80)).filter(Boolean).slice(0, 60);
    } else if (kind === 'selectmenu') {
      // selectmenu-style widget: value = display input's text; options = the innermost clickable
      // items in the widget's option list (kept in the DOM while collapsed, so visibility does
      // not restrict querySelectorAll). The widget root is the OUTERMOST contiguous
      // selectmenu-marked ancestor — inner wrappers (a value box holding only the input) match
      // the same token but hold no options.
      item.value = cut(el.value, 80);
      let sm = null;
      for (let n = el.parentElement, d = 0; n && d < 5; d += 1, n = n.parentElement) {
        if (n.matches && n.matches('[class*="selectmenu" i]')) { sm = n; } else if (sm) break;
      }
      const optAll = sm ? Array.from(sm.querySelectorAll('[role="option"],[role="menuitem"],button,a[href],li')) : [];
      // A widget's own trigger (toggle button, haspopup element) is chrome, not an option.
      const optEls = optAll.filter((o) => o !== el && !o.contains(el)
        && !(o.matches && o.matches('[aria-haspopup],[class*="toggle" i],[class*="trigger" i],[class*="button" i]'))
        && !optAll.some((other) => other !== o && o.contains(other)));
      item.options = [...new Set(optEls.map(o => cut(o.textContent || '', 80)).filter(Boolean))].slice(0, 60);
    } else if (['checkbox', 'radio'].includes((el.type || '').toLowerCase())) {
      item.value = el.checked ? 'on' : 'off';
    } else if ((el.type || '').toLowerCase() === 'password') {
      item.value = el.value ? '(password set)' : '';
    } else {
      item.value = cut(el.value || el.textContent || '', 80);
    }
    pushControl(item);
  }
  const radiosByName = new Map();
  for (const el of Array.from(document.querySelectorAll('input[type="radio"]'))) {
    const name = clean(el.getAttribute('name') || el.name || '');
    if (!name) continue;
    if (!radiosByName.has(name)) radiosByName.set(name, []);
    radiosByName.get(name).push(el);
  }
  const collapsedRadioNames = new Set();
  for (const [name, radios] of radiosByName.entries()) {
    const values = radios.map((radio) => clean(radio.value));
    const numericScale = values.length >= 2 && values.every((value) => /^\d+$/.test(value));
    if (radios.length < 2 && !numericScale) continue;
    const label = radioGroupLabelOf(radios[0]);
    if (!clean(label)) continue;
    collapsedRadioNames.add(name);
    const checked = radios.find((radio) => radio.checked);
    const visibleLabel = radios.map((radio) => {
      const labeled = radio.labels && radio.labels[0];
      if (labeled && rendered(labeled)) return labeled;
      if (!radio.id) return null;
      const css = window.CSS && CSS.escape ? CSS.escape(radio.id) : radio.id.replace(/"/g, '\\"');
      const forLabel = document.querySelector(`label[for="${css}"]`);
      return forLabel && rendered(forLabel) ? forLabel : null;
    }).find(Boolean);
    const box = radios[0].closest('fieldset, .field, [class*="field"]') || radios[0];
    const rectEl = visibleLabel || (rendered(box) ? box : radios[0]);
    const optionLabels = radios.map((radio) => {
      const optionLabel = radio.labels && radio.labels[0]
        ? clean(radio.labels[0].innerText || radio.labels[0].textContent)
        : '';
      return clean(optionLabel);
    });
    const uniqueNumericLabels = optionLabels.filter((text, index, all) => (
      /^\d+$/.test(text) && all.indexOf(text) === index
    ));
    const checkedIndex = radios.findIndex((radio) => radio.checked);
    let selectedScale = '';
    if (checked && numericScale) {
      const ranked = values.slice().sort((left, right) => Number(left) - Number(right));
      selectedScale = String(ranked.indexOf(clean(checked.value)) + 1);
    } else if (checkedIndex >= 0 && uniqueNumericLabels.length === radios.length) {
      selectedScale = optionLabels[checkedIndex];
    } else if (checkedIndex >= 0) {
      selectedScale = String(checkedIndex + 1);
    }
    const options = (numericScale ? values : optionLabels)
      .map((text) => cut(text, 40))
      .filter(Boolean);
    totalRendered += 1;
    if (sectionControls.length + controls.length >= rawControlLimit) {
      rawLimitHit = true;
      continue;
    }
    pushControl({
      label: cut(label, 80),
      kind: numericScale ? 'rating' : 'radio_group',
      name: cut(name, 80),
      id: cut((checked && checked.id) || radios[0].id || '', 80),
      value: checked ? clean(checked.value) : '',
      selected_text: selectedScale,
      selected_text_primary: selectedScale,
      options,
      rect: rectOf(rectEl),
      ...viewState(rectEl),
    });
  }
  if (collapsedRadioNames.size) {
    for (let index = controls.length - 1; index >= 0; index -= 1) {
      const item = controls[index];
      if (item.kind === 'radio_input' && collapsedRadioNames.has(item.name)) {
        controls.splice(index, 1);
      }
    }
  }
  const richSelector = [
    '[contenteditable="true"]',
    'iframe[id$="_ifr"]',
    'iframe[id$="_iframe"]',
    'iframe.tox-edit-area__iframe',
    '.tox-edit-area iframe',
    'iframe[title*="Rich Text" i]',
  ].join(',');
  for (const el of Array.from(document.querySelectorAll(richSelector))) {
    if (seenElements.has(el) || !rendered(el)) continue;
    seenElements.add(el);
    const label = richEditorLabelOf(el);
    if (!clean(label)) continue;
    totalRendered += 1;
    if (sectionControls.length + controls.length >= rawControlLimit) {
      rawLimitHit = true;
      continue;
    }
    pushControl({
      label: cut(label, 80),
      kind: 'rich_textarea',
      name: cut(el.getAttribute('name') || '', 80),
      id: cut(el.id || '', 80),
      value: cut(richEditorValue(el), 80),
      focused: el === document.activeElement,
      rect: rectOf(el),
      ...viewState(el),
    });
  }
  return JSON.stringify({
    controls: statusControls.concat(sectionControls, controls),
    total_rendered: totalRendered,
    raw_limit_hit: rawLimitHit,
  });
})()
"""


def normalize_form_control_snapshot(
    raw: Any,
    *,
    max_controls: int | None = MAX_CONTROLS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize controls and report whether the returned inventory is complete.

    Repeated collection rows are selection units: once a row is chosen, every control from that
    row is retained.  This prevents a cap boundary from returning only the value column while
    dropping the sibling field that makes the row valid or invalid.
    """
    controls = raw.get("controls") if isinstance(raw, dict) else raw
    if not isinstance(controls, list):
        return [], {
            "total_rendered": 0,
            "returned": 0,
            "truncated": False,
            "coverage": "unknown",
            "raw_limit_hit": False,
        }
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(controls):
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind"), 40)
        label = _text(item.get("label"), MAX_TEXT)
        name = _text(item.get("name"), MAX_TEXT)
        control_id = _text(item.get("id"), MAX_TEXT)
        placeholder = _text(item.get("placeholder"), MAX_TEXT)
        value = _text(item.get("value"), MAX_TEXT)
        selected_text = _text(item.get("selected_text"), MAX_TEXT)
        selected_text_primary = _text(item.get("selected_text_primary"), MAX_TEXT)
        options = _string_list(item.get("options"))
        row_values = _string_list(item.get("row_values"))
        if not any([
            kind, label, name, control_id, placeholder, value, selected_text,
            selected_text_primary, options,
        ]):
            continue
        norm: dict[str, Any] = {"kind": kind or "control"}
        select_like = kind in {
            "native_select", "select", "selectmenu", "combobox", "listbox",
            "aria_listbox",
        }
        if label:
            norm["label"] = label
        if name:
            norm["name"] = name
        if control_id:
            norm["id"] = control_id
        if placeholder:
            norm["placeholder"] = placeholder
        if value:
            norm["value"] = value
        if kind == "rating":
            scale = rating_selected_scale(value, options)
            if scale:
                selected_text = scale
                selected_text_primary = scale
        if selected_text or (
            "selected_text" in item and select_like
        ):
            norm["selected_text"] = selected_text
        if selected_text_primary or (
            "selected_text_primary" in item and select_like
        ):
            norm["selected_text_primary"] = selected_text_primary
        if options:
            norm["options"] = options
        if row_values:
            norm["row_values"] = row_values
        if item.get("focused") is True:
            norm["focused"] = True
        if item.get("is_filter") is True:
            norm["is_filter"] = True
        if item.get("query_action") in {"submit", "reset"}:
            norm["query_action"] = item["query_action"]
        if (
            item.get("form_action") == "commit"
            or (
                kind in _COMMIT_CONTROL_KINDS
                and _COMMIT_LABEL_RE.search(label or value)
            )
        ):
            norm["form_action"] = "commit"
        traversal = traversal_action_of(norm)
        if traversal:
            norm["traversal_action"] = traversal
        if item.get("is_datepicker") is True:
            norm["is_datepicker"] = True
        if item.get("required") is True:
            norm["required"] = True
        group_id = _text(item.get("group_id"), MAX_TEXT)
        group_field = _text(item.get("group_field"), MAX_TEXT)
        if group_id:
            norm["group_id"] = group_id
            if isinstance(item.get("group_index"), (int, float)):
                norm["group_index"] = int(item["group_index"])
            if group_field:
                norm["group_field"] = group_field
        rect = item.get("rect")
        if isinstance(rect, dict):
            norm["rect"] = {
                key: int(rect[key])
                for key in ("x", "y", "w", "h")
                if isinstance(rect.get(key), (int, float))
            }
        # Only flag the exceptional off-viewport controls (in-viewport is the default). Lets the
        # planner scroll to a rendered-but-off-screen control instead of the feasibility judge
        # concluding it is absent.
        if item.get("in_viewport") is False:
            norm["in_viewport"] = False
            if item.get("occluded") is True:
                norm["occluded"] = True
            # Carry the scroll DIRECTION so the planner scrolls the right way to reach it.
            vp = item.get("viewport_pos")
            if vp in ("above", "below"):
                norm["viewport_pos"] = vp
        if kind == "status_message":
            priority = -1
        elif kind == "section_toggle":
            priority = 0
        elif kind == "rich_textarea":
            priority = 1
        elif item.get("in_viewport") is True:
            priority = 2
        elif item.get("in_viewport") is False:
            priority = 3
        else:
            priority = 2
        ranked.append((priority, index, norm))
    raw_total = raw.get("total_rendered") if isinstance(raw, dict) else None
    total_rendered = int(raw_total) if isinstance(raw_total, (int, float)) else len(ranked)
    raw_limit_hit = bool(isinstance(raw, dict) and raw.get("raw_limit_hit"))
    if max_controls is None:
        ranked.sort(key=lambda row: row[1])
        normalized = [norm for _, _, norm in ranked]
        metadata = {
            "total_rendered": total_rendered,
            "returned": len(normalized),
            "truncated": raw_limit_hit,
            "coverage": "partial" if raw_limit_hit else "complete",
            "raw_limit_hit": raw_limit_hit,
        }
        return normalized, metadata

    units: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    for row in ranked:
        norm = row[2]
        group_id = str(norm.get("group_id") or "")
        key = f"group:{group_id}" if group_id else f"control:{row[1]}"
        units.setdefault(key, []).append(row)

    def unit_rank(unit: list[tuple[int, int, dict[str, Any]]]) -> tuple[int, int]:
        first_index = min(item[1] for item in unit)
        kinds = {str(item[2].get("kind") or "") for item in unit}
        focused = any(item[2].get("focused") is True for item in unit)
        grouped = any(item[2].get("group_id") for item in unit)
        in_view = any(item[2].get("in_viewport") is not False for item in unit)
        field_like = bool(kinds & _FIELD_KINDS) or any(
            _is_choice_operation(item[2]) for item in unit
        )
        if "status_message" in kinds:
            return (-1, first_index)
        if "section_toggle" in kinds:
            return (0, first_index)
        if "rich_textarea" in kinds:
            return (1, first_index)
        if focused:
            return (2, first_index)
        if field_like and in_view:
            # Editable fields and select/clear-all ops outrank collection row links so a
            # dense table cannot evict the checkboxes the current surface is asking about.
            return (3, first_index)
        if grouped and in_view:
            # Bottom-most rendered rows are commonly the newly-added/incomplete rows.  Prefer
            # them over older rows while retaining the whole row atomically.
            return (4, -first_index)
        if in_view:
            return (5, first_index)
        return (_OFFSCREEN_UNIT_RANK, first_index)

    ordered_units = sorted(units.values(), key=unit_rank)
    selected: list[tuple[int, int, dict[str, Any]]] = []
    deferred_offscreen: list[list[tuple[int, int, dict[str, Any]]]] = []
    has_offscreen = any(
        unit_rank(unit)[0] == _OFFSCREEN_UNIT_RANK for unit in ordered_units
    )
    on_view_limit = max_controls - (OFF_VIEWPORT_RESERVE if has_offscreen else 0)
    for unit in ordered_units:
        if unit_rank(unit)[0] == _OFFSCREEN_UNIT_RANK:
            deferred_offscreen.append(unit)
            continue
        if len(selected) + len(unit) <= on_view_limit:
            selected.extend(unit)
    offscreen_budget = min(OFF_VIEWPORT_RESERVE, max_controls - len(selected))
    for unit in deferred_offscreen:
        if len(unit) <= offscreen_budget:
            selected.extend(unit)
            offscreen_budget -= len(unit)
    # Fill any remaining room from omitted units without splitting a repeated group.
    selected_ids = {id(item) for item in selected}
    for unit in ordered_units:
        if any(id(item) in selected_ids for item in unit):
            continue
        if len(selected) + len(unit) <= max_controls:
            selected.extend(unit)
            selected_ids.update(id(item) for item in unit)

    selected.sort(key=lambda row: row[1])
    normalized = [norm for _, _, norm in selected]
    truncated = raw_limit_hit or len(normalized) < total_rendered
    metadata = {
        "total_rendered": total_rendered,
        "returned": len(normalized),
        "truncated": truncated,
        "coverage": "partial" if truncated else "complete",
        "raw_limit_hit": raw_limit_hit,
    }
    return normalized, metadata


def normalize_form_controls(raw: Any) -> list[dict[str, Any]]:
    """Backward-compatible controls-only view of :func:`normalize_form_control_snapshot`."""
    controls, _metadata = normalize_form_control_snapshot(raw)
    return controls


def normalize_form_control_state(
    raw: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the complete normalized control-state index before prompt truncation."""
    return normalize_form_control_snapshot(raw, max_controls=None)


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, MAX_TEXT)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= MAX_OPTIONS:
            break
    return out
