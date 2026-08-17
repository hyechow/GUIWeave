"""Read a complete plain-text document from the foreground Android WebView."""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.request
from typing import Any
from urllib.parse import urlsplit

_TIMEOUT_S = 1.0
_BUDGET_S = 2.0
_MAX_CANDIDATES = 4
_DOCUMENT_JS = """(() => {
  const body = document.body;
  if (!body) return null;
  const only = body.children.length === 1 ? body.firstElementChild : null;
  if (body.children.length && (!only || only.tagName !== "PRE")) return null;
  const content = (only || body).textContent;
  if (typeof content !== "string" || content.length > 262144) return null;
  return {title: document.title || "", content};
})()"""


def _get_json(url: str, *, timeout: float = _TIMEOUT_S) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read())


def _candidate_sockets(unix_table: str, package_id: str, pids: set[str]) -> list[str]:
    return list(dict.fromkeys(
        name for name in re.findall(r"@(\S*devtools_remote\S*)", unix_table)
        if package_id in name or any(name.endswith(f"_{pid}") for pid in pids)
    ))[:_MAX_CANDIDATES]


def _evaluate(ws_url: str, *, timeout: float = _TIMEOUT_S) -> dict[str, Any] | None:
    import websocket

    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": _DOCUMENT_JS, "returnByValue": True,
        }}))
        for _ in range(16):
            message = json.loads(ws.recv())
            if message.get("id") == 1:
                value = ((message.get("result") or {}).get("result") or {}).get("value")
                return value if isinstance(value, dict) else None
    finally:
        ws.close()
    return None


def _document_snapshot(target: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    content = value.get("content")
    if not isinstance(content, str):
        return None
    title = str(target.get("title") or value.get("title") or "").strip()
    table = {"caption": title or "Document", "rows": [{"Content": content}],
             "partial": False, "traversal": {"type": "static"}}
    return {"title": title, "url": str(target.get("url") or ""), "tables": [table]}


def _visible_pages(targets: Any) -> list[dict[str, Any]]:
    pages = [item for item in targets if isinstance(item, dict)
             and item.get("type") == "page" and item.get("webSocketDebuggerUrl")
             ] if isinstance(targets, list) else []
    visible = []
    for item in pages:
        try: description = json.loads(str(item.get("description") or "{}"))
        except json.JSONDecodeError: description = {}
        if description.get("visible") is True and description.get("empty") is not True:
            visible.append(item)
    return (visible or (pages if len(pages) == 1 else []))[:_MAX_CANDIDATES]


def read_foreground_document(dev: Any, package_id: str) -> dict[str, Any] | None:
    """Return a complete plain-document snapshot when guarded CDP is available."""

    if not re.fullmatch(r"[A-Za-z0-9_.]+", package_id):
        return None
    pids = set(str(dev.shell(f"pidof {package_id}") or "").split())
    remotes = _candidate_sockets(str(dev.shell("cat /proc/net/unix") or ""), package_id, pids)
    deadline = time.monotonic() + _BUDGET_S
    for remote in remotes:
        if time.monotonic() >= deadline:
            break
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        local = f"tcp:{port}"
        try:
            dev.forward(local, f"localabstract:{remote}", norebind=True)
            base = f"http://127.0.0.1:{port}"
            remaining = lambda: min(_TIMEOUT_S, max(0.05, deadline - time.monotonic()))
            version = _get_json(f"{base}/json/version", timeout=remaining())
            if not isinstance(version, dict) or version.get("Android-Package") != package_id:
                continue
            for target in _visible_pages(_get_json(f"{base}/json", timeout=remaining())):
                endpoint = urlsplit(str(target["webSocketDebuggerUrl"]))
                path = endpoint.path + (f"?{endpoint.query}" if endpoint.query else "")
                value = _evaluate(f"ws://127.0.0.1:{port}{path}", timeout=remaining())
                if value is not None and (snapshot := _document_snapshot(target, value)):
                    return snapshot
        except Exception:  # optional read-only sensor
            continue
        finally:
            dev.forward_remove(local, raise_non_found=False)
    return None
