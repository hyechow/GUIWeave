"""Read-only DOM form-control snapshots for browser perception."""

from __future__ import annotations

from typing import Any

MAX_CONTROLS = 40
# Reserved slots for rendered-but-off-screen controls when the total exceeds MAX_CONTROLS, so a
# large in-viewport set can't fully evict off-viewport fields the planner needs to scroll toward.
OFF_VIEWPORT_RESERVE = 8
MAX_OPTIONS = 30
MAX_TEXT = 80


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
  const inViewport = (el) => {
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
  // 收录所有已「渲染」的控件,不按视口位置丢弃,并给每个控件标 in_viewport。长表单(如 Cart
  // Price Rule)里 Rule Name 在顶、Discount/Save 在底,滚到中部时首尾控件滑出视口 —— 旧的视口
  // 过滤会把它们从清单里删掉,feasibility 于是误判「控件不存在」而放弃(WebArena 702 Rule Name /
  // 703 discount_amount),planner 也不知道该滚过去。改为报全部已渲染控件 + in_viewport 标志,由消费
  // 方决定是否先滚动。原 keepForRead 只把 select/textarea 豁免视口(Material below-fold),现统一到全部。
  const labelFromContainer = (el) => {
    const boxes = [
      el.closest('.admin__field'),
      el.closest('.field'),
      el.closest('[class*=field]'),
      el.parentElement,
      el.parentElement && el.parentElement.parentElement,
    ].filter(Boolean);
    for (const box of boxes) {
      const lbl = box.querySelector('label,.admin__field-label,.label,[data-label]');
      const text = clean(lbl && (lbl.innerText || lbl.textContent || lbl.getAttribute('data-label')));
      if (text) return text;
    }
    return '';
  };
  const labelOf = (el) => {
    const direct = clean(el.getAttribute('aria-label') || el.getAttribute('title'));
    if (direct) return direct;
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
    const container = labelFromContainer(el);
    if (container) return container;
    const prev = el.previousElementSibling || (el.parentElement && el.parentElement.previousElementSibling);
    const prevText = clean(prev && (prev.innerText || prev.textContent));
    if (prevText && prevText.length <= 80) return prevText;
    return clean(el.getAttribute('placeholder') || el.name || el.id);
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
  const repeatedGroupOf = (el) => {
    // Preserve field association inside repeated form collections. Flat control text cannot tell
    // whether a value belongs to the intended column of an option/line-item row. This is generic
    // DOM structure (table/grid/field-option), not an application vocabulary.
    const row = el.closest('tr,[role="row"],[data-role="row"],.admin__field-option');
    if (!row) return null;
    const owner = row.closest('table,[role="grid"],fieldset,.admin__fieldset');
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
    const cls = clean(el.className || '').toLowerCase();
    const holder = el.closest('._required,[aria-required="true"],[data-validate*="required"],.required-entry');
    return Boolean(
      el.required
      || own === 'true'
      || dataValidate.includes('required')
      || cls.includes('required-entry')
      || holder
    );
  };
  const kindOf = (el) => {
    const tag = el.tagName;
    const role = clean(el.getAttribute('role')).toLowerCase();
    if (tag === 'SELECT') return 'native_select';
    if (tag === 'TEXTAREA') return 'textarea';
    if (role === 'combobox') return 'aria_combobox';
    if (role === 'listbox') return 'aria_listbox';
    // Magento selectmenu: <input> 是自定义下拉的显示框,选项在 .selectmenu-items 里、click 绑在
    // 选项 <button>(.selectmenu-item-action) 上。识别为 selectmenu(选选项,非 type 输入),否则被当
    // text_input → planner 用 type 填值,改了显示框却不触发 setSize(见 task 63 per page 复盘)。
    if (tag === 'INPUT' && el.closest('.selectmenu')) return 'selectmenu';
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
    const wrapper = sectionTitle.closest('.fieldset-wrapper, .admin__collapsible-block-wrapper');
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
      effect_kind: 'presentation',
      name: cut(el.getAttribute('name') || '', 80),
      id: cut(el.id || '', 80),
      value: expandedState(el),
      rect: rectOf(el),
      in_viewport: inViewport(el),
      viewport_pos: viewportPos(el),
    }, sectionControls);
  }
  const selector = [
    'input', 'select', 'textarea', 'button', 'a[href]',
    '[role=button]', '[role=link]', '[role=combobox]', '[role=listbox]',
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
    const isFilter = el.id.includes('_filter_')
      || !!el.closest('[data-role="filter-form"]')
      || !!el.closest('.admin__data-grid-filters');
    const isPager = !!el.closest(
      '[class*="pagination"],[class*="pager"],[class*="pages-items"]'
    );
    const form = el.closest('form');
    const isAuthentication = !!(
      form && form.querySelector('input[type="password"]')
    );
    const isDatepicker = el.classList && el.classList.contains('_has-datepicker');
    const role = clean(el.getAttribute('role')).toLowerCase();
    const isNavigation = el.tagName === 'A' || role === 'link';
    const isPresentation = !!(
      el.hasAttribute('aria-expanded')
      || el.closest('[role="menu"],[class*="columns"]')
    );
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
      in_viewport: inViewport(el),
      viewport_pos: viewportPos(el),
    };
    const repeatedGroup = repeatedGroupOf(el);
    if (repeatedGroup) {
      item.group_id = repeatedGroup.id;
      item.group_index = repeatedGroup.index;
      item.group_field = repeatedGroup.field;
    }
    if (isFilter) item.is_filter = true;
    if (isAuthentication) {
      item.effect_kind = 'authentication';
    } else if (isFilter) {
      item.effect_kind = 'query_control';
    } else if (isPager) {
      item.effect_kind = 'pagination';
    } else if (isNavigation) {
      item.effect_kind = 'navigation';
    } else if (isPresentation) {
      item.effect_kind = 'presentation';
    } else if (el.tagName === 'BUTTON' || role === 'button') {
      item.effect_kind = 'business_commit';
    } else {
      item.effect_kind = 'field_write';
    }
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
      // selectmenu:value=input 显示值;options 从 .selectmenu-items 选项 button 文本抓
      // (折叠时选项在 DOM 但不可见,querySelectorAll 不受 visible 限制,仍能取到)。
      item.value = cut(el.value, 80);
      const sm = el.closest('.selectmenu');
      const optEls = sm ? Array.from(sm.querySelectorAll('.selectmenu-items .selectmenu-item-action, .selectmenu-items button')) : [];
      item.options = optEls.map(o => cut(o.textContent || '', 80)).filter(Boolean).slice(0, 60);
    } else if (['checkbox', 'radio'].includes((el.type || '').toLowerCase())) {
      item.value = el.checked ? 'on' : 'off';
    } else if ((el.type || '').toLowerCase() === 'password') {
      item.value = el.value ? '(password set)' : '';
    } else {
      item.value = cut(el.value || el.textContent || '', 80);
    }
    pushControl(item);
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
      in_viewport: inViewport(el),
      viewport_pos: viewportPos(el),
    });
  }
  return JSON.stringify({
    controls: sectionControls.concat(controls),
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
        if not any([
            kind, label, name, control_id, placeholder, value, selected_text,
            selected_text_primary, options,
        ]):
            continue
        norm: dict[str, Any] = {"kind": kind or "control"}
        select_like = kind in {"native_select", "select", "selectmenu", "combobox", "listbox"}
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
        if item.get("focused") is True:
            norm["focused"] = True
        if item.get("is_filter") is True:
            norm["is_filter"] = True
        if item.get("effect_kind"):
            norm["effect_kind"] = _text(item.get("effect_kind"), 40)
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
            # Carry the scroll DIRECTION so the planner scrolls the right way to reach it.
            vp = item.get("viewport_pos")
            if vp in ("above", "below"):
                norm["viewport_pos"] = vp
        if kind == "section_toggle":
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
        if "section_toggle" in kinds:
            return (0, first_index)
        if "rich_textarea" in kinds:
            return (1, first_index)
        if focused:
            return (2, first_index)
        if grouped and in_view:
            # Bottom-most rendered rows are commonly the newly-added/incomplete rows.  Prefer
            # them over older rows while retaining the whole row atomically.
            return (3, -first_index)
        if in_view:
            return (4, first_index)
        return (5, first_index)

    ordered_units = sorted(units.values(), key=unit_rank)
    selected: list[tuple[int, int, dict[str, Any]]] = []
    deferred_offscreen: list[list[tuple[int, int, dict[str, Any]]]] = []
    has_offscreen = any(unit_rank(unit)[0] == 5 for unit in ordered_units)
    on_view_limit = max_controls - (OFF_VIEWPORT_RESERVE if has_offscreen else 0)
    for unit in ordered_units:
        if unit_rank(unit)[0] == 5:
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
