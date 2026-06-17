# Browser Native UI Primitives

Browser screenshots capture the web page viewport, not every browser, OS, or native
control popup. If a UI surface is not painted by page DOM/CSS, a vision-only loop may
never see it even after a successful click. These cases need browser primitives or
event handlers instead of repeated `tap` attempts.

## Visibility Boundary

Usually visible to screenshots and DOM readers:

- Page-rendered modals, popovers, menus, drawers, and dropdowns from libraries such as
  React, AntD, Material UI, Magento UI components, etc.
- DOM nodes styled with CSS, including ARIA combobox/listbox implementations.

Often invisible or unreliable in page screenshots:

- Native form control popups:
  - `<select>` option popup
  - `<input type="date">`, `time`, `month`, `week` pickers
  - `<input type="color">`
  - `<input list="...">` datalist suggestions
- Browser or OS dialogs:
  - file chooser
  - print/save/download dialogs
  - HTTP auth prompts
  - permission prompts for location, notifications, camera, microphone
  - certificate/security prompts and external protocol confirmations
- JavaScript native dialogs:
  - `alert`
  - `confirm`
  - `prompt`
- Browser assistive UI:
  - autofill, account, address, and password suggestions
  - password manager overlays
  - spellcheck, translate, and context menus
  - address bar suggestions or browser toolbar menus
- OS input UI:
  - IME candidate windows
  - system text substitution or shortcut popups

## Current Coverage

- `upload`: handles native file chooser paths by injecting the file through Playwright
  instead of clicking and waiting for an OS dialog.
- `select_option`: handles native `<select>` and visible option lists by setting the
  page control directly and dispatching `input`/`change` events. This fixes the
  WebArena task 63 failure mode where Magento's `Status` `<select>` popup never
  appeared in screenshots.

## Priority Backlog

High-value next primitives/handlers:

- Dialog handler for `alert` / `confirm` / `prompt`, with explicit accept/dismiss
  policy and report logging.
- Date/time setter for native date/time/month/week controls, preferably by setting the
  DOM value and dispatching events rather than driving the browser picker UI.
- Permission/download/print handlers only if WebArena or target browser tasks start
  depending on them.

Lower priority unless a real task hits them:

- Autofill/password manager overlays.
- Browser context menus and toolbar menus.
- IME candidate windows.

## Implementation Rule

When a task requires interacting with a surface outside page rendering, do not try to
solve it with screenshots and repeated clicks. Add or reuse a browser primitive.

A new browser primitive usually needs updates in:

- `gui_agent/adapters/browser/actions.py`: action schema and validation.
- `gui_agent/adapters/browser/policies.py`: action-policy prompt and postprocess guard.
- `gui_agent/adapters/browser/executor.py`: dispatch and any tap-to-primitive rescue.
- `gui_agent/adapters/browser/device.py`: Playwright/CDP implementation.
- `gui_agent/reports/` and visualizer code: action label/color/report visibility.
- tests: action-space guard, policy postprocess guard, executor/device behavior where
  practical.

Keep the distinction clear:

- Page-rendered DOM overlay: use normal visual/DOM tap behavior.
- Native/browser/OS overlay: use a primitive or event handler and verify through page
  state, DOM value, network response, or explicit handler result.
