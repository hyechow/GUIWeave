"""Read-only structured perception for the foreground Android WebView."""

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
_MAX_TEXT_CHARS = 262_144
_MAX_CANDIDATES = 4
_DOCUMENT_JS = f"""(() => {{
  const body = document.body;
  if (!body) return null;
  const only = body.children.length === 1 ? body.firstElementChild : null;
  if (body.children.length && (!only || only.tagName !== "PRE")) return null;
  const content = (only || body).textContent;
  if (typeof content !== "string" || content.length > {_MAX_TEXT_CHARS}) return null;
  return {{title: document.title || "", content}};
}})()"""


def _get_json(url: str, *, timeout: float = _TIMEOUT_S) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _candidate_sockets(unix_table: str, package_id: str, pids: set[str]) -> list[str]:
    names = re.findall(r"@(\S*devtools_remote\S*)", unix_table)
    return list(dict.fromkeys(
        name for name in names
        if package_id in name or any(name.endswith(f"_{pid}") for pid in pids)
    ))[:_MAX_CANDIDATES]


def _evaluate(
    ws_url: str, *, timeout: float = _TIMEOUT_S,
) -> dict[str, Any] | None:
    import websocket

    endpoint = urlsplit(ws_url)
    transport = socket.create_connection(
        (endpoint.hostname or "127.0.0.1", endpoint.port or 80),
        timeout=timeout,
    )
    try:
        ws = websocket.create_connection(
            ws_url, timeout=timeout, socket=transport, suppress_origin=True,
        )
    except Exception:
        transport.close()
        raise
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": _DOCUMENT_JS, "returnByValue": True},
        }))
        for _ in range(16):
            message = json.loads(ws.recv())
            if message.get("id") != 1:
                continue
            value = ((message.get("result") or {}).get("result") or {}).get("value")
            return value if isinstance(value, dict) else None
        return None
    finally:
        ws.close()


def _document_snapshot(target: dict[str, Any], value: dict[str, Any]) -> dict[str, Any] | None:
    content = value.get("content")
    if not isinstance(content, str):
        return None
    name = str(target.get("title") or value.get("title") or "").strip()
    row: dict[str, Any] = {"Content": content}
    if name:
        row["Name"] = name
    return {
        "title": name,
        "url": str(target.get("url") or ""),
        "tables": [{
            "caption": name or "Document",
            "rows": [row],
            "partial": False,
            "traversal": {"type": "static"},
        }],
    }


def _visible_pages(targets: Any) -> list[dict[str, Any]]:
    pages = [
        target for target in targets if isinstance(target, dict)
        and target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ] if isinstance(targets, list) else []
    visible = []
    for target in pages:
        try:
            description = json.loads(str(target.get("description") or "{}"))
        except json.JSONDecodeError:
            description = {}
        if description.get("visible") is True and description.get("empty") is not True:
            visible.append(target)
    return (visible or (pages if len(pages) == 1 else []))[:_MAX_CANDIDATES]


def read_foreground_document(dev: Any, package_id: str) -> dict[str, Any] | None:
    """Return a complete plain-document snapshot, or ``None`` when unavailable."""

    if not re.fullmatch(r"[A-Za-z0-9_.]+", package_id):
        return None
    pids = set(str(dev.shell(f"pidof {package_id}") or "").split())
    unix_table = str(dev.shell("cat /proc/net/unix") or "")
    deadline = time.monotonic() + _BUDGET_S
    for remote in _candidate_sockets(unix_table, package_id, pids):
        if time.monotonic() >= deadline:
            break
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        local = f"tcp:{port}"
        forwarded = False
        try:
            dev.forward(local, f"localabstract:{remote}", norebind=True)
            forwarded = True
            base = f"http://127.0.0.1:{port}"
            version = _get_json(
                f"{base}/json/version",
                timeout=min(_TIMEOUT_S, max(0.05, deadline - time.monotonic())),
            )
            if not isinstance(version, dict) or version.get("Android-Package") != package_id:
                continue
            targets = _get_json(
                f"{base}/json",
                timeout=min(_TIMEOUT_S, max(0.05, deadline - time.monotonic())),
            )
            for target in _visible_pages(targets):
                if time.monotonic() >= deadline:
                    break
                endpoint = urlsplit(str(target["webSocketDebuggerUrl"]))
                ws_path = endpoint.path + (f"?{endpoint.query}" if endpoint.query else "")
                value = _evaluate(
                    f"ws://127.0.0.1:{port}{ws_path}",
                    timeout=min(_TIMEOUT_S, max(0.05, deadline - time.monotonic())),
                )
                if value is not None and (snapshot := _document_snapshot(target, value)):
                    return snapshot
        except Exception:  # noqa: BLE001 - this sensor is strictly optional
            continue
        finally:
            if forwarded:
                dev.forward_remove(local, raise_non_found=False)
    return None
