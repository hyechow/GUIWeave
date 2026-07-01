"""Read-only DOM form-control snapshots for browser perception."""

from __future__ import annotations

from typing import Any

MAX_CONTROLS = 25
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
  const seen = new Set();
  const selector = 'input,select,textarea,[role=combobox],[role=listbox]';
  for (const el of Array.from(document.querySelectorAll(selector))) {
    if (seen.has(el) || !rendered(el)) continue;
    seen.add(el);
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') continue;
    const kind = kindOf(el);
    const isFilter = el.id.includes('_filter_')
      || !!el.closest('[data-role="filter-form"]')
      || !!el.closest('.admin__data-grid-filters');
    const isDatepicker = el.classList && el.classList.contains('_has-datepicker');
    const item = {
      label: cut(labelOf(el), 80),
      kind,
      name: cut(el.getAttribute('name') || '', 80),
      id: cut(el.id || '', 80),
      placeholder: cut(el.getAttribute('placeholder') || '', 80),
      value: '',
      selected_text: '',
      focused: el === document.activeElement,
      rect: rectOf(el),
      in_viewport: inViewport(el),
    };
    if (isFilter) item.is_filter = true;
    if (isDatepicker) item.is_datepicker = true;
    if (el.tagName === 'SELECT') {
      const opts = Array.from(el.options || []);
      // 取「全部已选项」(multiple 时可能多选)而非仅第一个选中项,join 成可读文本;
      // 多选属性(如 Material)否则只读到第一个选中值,漏掉其余。
      const selOpts = Array.from(el.selectedOptions || []);
      item.value = cut(el.value, 80);
      item.selected_text = cut(selOpts.map(o => o.textContent || o.label || o.value).filter(Boolean).join(', '), 80);
      item.options = opts.map(o => cut(o.textContent || o.label || o.value, 80)).filter(Boolean).slice(0, 60);
    } else if (kind === 'selectmenu') {
      // selectmenu:value=input 显示值;options 从 .selectmenu-items 选项 button 文本抓
      // (折叠时选项在 DOM 但不可见,querySelectorAll 不受 visible 限制,仍能取到)。
      item.value = cut(el.value, 80);
      const sm = el.closest('.selectmenu');
      const optEls = sm ? Array.from(sm.querySelectorAll('.selectmenu-items .selectmenu-item-action, .selectmenu-items button')) : [];
      item.options = optEls.map(o => cut(o.textContent || '', 80)).filter(Boolean).slice(0, 60);
    } else if ((el.type || '').toLowerCase() === 'password') {
      item.value = el.value ? '(password set)' : '';
    } else {
      item.value = cut(el.value || el.textContent || '', 80);
    }
    controls.push(item);
    if (controls.length >= 40) break;
  }
  return JSON.stringify({controls});
})()
"""


def normalize_form_controls(raw: Any) -> list[dict[str, Any]]:
    """Normalize raw JS form-control snapshots into compact dictionaries."""
    controls = raw.get("controls") if isinstance(raw, dict) else raw
    if not isinstance(controls, list):
        return []
    out: list[dict[str, Any]] = []
    for item in controls[:MAX_CONTROLS]:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("kind"), 40)
        label = _text(item.get("label"), MAX_TEXT)
        name = _text(item.get("name"), MAX_TEXT)
        control_id = _text(item.get("id"), MAX_TEXT)
        placeholder = _text(item.get("placeholder"), MAX_TEXT)
        value = _text(item.get("value"), MAX_TEXT)
        selected_text = _text(item.get("selected_text"), MAX_TEXT)
        options = _string_list(item.get("options"))
        if not any([kind, label, name, control_id, placeholder, value, selected_text, options]):
            continue
        norm: dict[str, Any] = {"kind": kind or "control"}
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
        if selected_text:
            norm["selected_text"] = selected_text
        if options:
            norm["options"] = options
        if item.get("focused") is True:
            norm["focused"] = True
        if item.get("is_filter") is True:
            norm["is_filter"] = True
        if item.get("is_datepicker") is True:
            norm["is_datepicker"] = True
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
        out.append(norm)
    return out


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
