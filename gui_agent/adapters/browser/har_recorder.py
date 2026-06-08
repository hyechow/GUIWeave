"""Raw-CDP network recorder → HAR, for WebArena's NetworkEventEvaluator.

WHY NOT Playwright ``record_har_path``: that is a context-creation option, but the
browser adapter ATTACHES to an existing Chrome context over ``connect_over_cdp``
(and that high-level binding is the one that hangs here). So we capture at the CDP
transport instead: a dedicated ``CDPSession`` on the page target, ``Network.enable``,
and listeners that accumulate ``requestWillBeSent`` / ``responseReceived`` /
``loadingFinished`` into memory. Events are pumped by the agent loop's own CDP
traffic (every-turn screenshots), so they land while the task runs.

``dump()`` serializes the in-memory events to a HAR — it touches NO live session, so
it is safe to call AFTER the agent loop has closed the device (Playwright stopped).
That is the whole point of buffering in memory: the entry regains control only once
``run_agent_loop`` has returned and the session is gone.

Captured per request: method, url, headers, query string, and post body (when CDP
inlines it) + response status/headers/mimeType — exactly the fields the evaluator
matches on. Response BODIES are not fetched (would need a live ``getResponseBody``);
add that later if a task evaluates on response content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl


def _headers_list(headers: dict | None) -> list[dict]:
    return [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()]


def _query_list(url: str) -> list[dict]:
    try:
        return [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    except Exception:
        return []


def _iso(wall_time: float | None) -> str:
    try:
        if wall_time:
            return datetime.fromtimestamp(wall_time, tz=timezone.utc).isoformat()
    except Exception:
        pass
    return datetime.now(tz=timezone.utc).isoformat()


class HarRecorder:
    """Buffers CDP Network events off a live ``PlaywrightDevice`` and emits a HAR."""

    def __init__(self, device: object):
        self._device = device
        self._sess = None
        # requestId -> partial entry dict (request filled first, response merged in).
        self._reqs: dict[str, dict] = {}
        self._order: list[str] = []

    # ----- lifecycle -------------------------------------------------------
    def start(self) -> "HarRecorder":
        """Open a dedicated CDP session on the page target, enable Network and
        subscribe. Best-effort: on any failure the recorder simply captures nothing
        (the entry still writes a valid empty HAR so eval can load it)."""
        try:
            ctx = getattr(self._device, "_context", None)
            page = getattr(self._device, "page", None)
            if ctx is None or page is None:
                return self
            self._sess = ctx.new_cdp_session(page)
            self._sess.send("Network.enable", {})
            self._sess.on("Network.requestWillBeSent", self._on_request)
            self._sess.on("Network.responseReceived", self._on_response)
            self._sess.on("Network.loadingFinished", self._on_finished)
        except Exception:
            self._sess = None
        return self

    # ----- CDP event handlers ---------------------------------------------
    def _on_request(self, params: dict) -> None:
        try:
            rid = params.get("requestId")
            if rid is None:
                return
            req = params.get("request") or {}
            post_text = req.get("postData")
            headers = req.get("headers") or {}
            content_type = headers.get("Content-Type") or headers.get("content-type") or ""
            entry = {
                "startedDateTime": _iso(params.get("wallTime")),
                "_t0": params.get("timestamp"),
                "time": 0,
                "request": {
                    "method": req.get("method", "GET"),
                    "url": req.get("url", ""),
                    "httpVersion": "HTTP/1.1",
                    "cookies": [],
                    "headers": _headers_list(headers),
                    "queryString": _query_list(req.get("url", "")),
                    "headersSize": -1,
                    "bodySize": len(post_text) if isinstance(post_text, str) else -1,
                },
                "response": {
                    "status": 0,
                    "statusText": "",
                    "httpVersion": "HTTP/1.1",
                    "cookies": [],
                    "headers": [],
                    "content": {"size": 0, "mimeType": "", "text": ""},
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": -1,
                },
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
            }
            if isinstance(post_text, str) and post_text:
                entry["request"]["postData"] = {"mimeType": content_type, "text": post_text}
            # A redirect reuses the same requestId; keep the latest request snapshot
            # but don't lose ordering.
            if rid not in self._reqs:
                self._order.append(rid)
            self._reqs[rid] = entry
        except Exception:
            pass

    def _on_response(self, params: dict) -> None:
        try:
            rid = params.get("requestId")
            entry = self._reqs.get(rid) if rid is not None else None
            if entry is None:
                return
            resp = params.get("response") or {}
            entry["response"].update(
                {
                    "status": resp.get("status", 0),
                    "statusText": resp.get("statusText", ""),
                    "headers": _headers_list(resp.get("headers")),
                    "content": {"size": 0, "mimeType": resp.get("mimeType", ""), "text": ""},
                }
            )
        except Exception:
            pass

    def _on_finished(self, params: dict) -> None:
        try:
            rid = params.get("requestId")
            entry = self._reqs.get(rid) if rid is not None else None
            if entry is None:
                return
            t0 = entry.pop("_t0", None)
            t1 = params.get("timestamp")
            if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
                entry["time"] = max(0.0, (t1 - t0) * 1000.0)
        except Exception:
            pass

    # ----- output ----------------------------------------------------------
    def dump(self, path: str) -> str:
        """Serialize buffered events to a HAR 1.2 file (no live session needed)."""
        import json

        entries = []
        for rid in self._order:
            e = self._reqs.get(rid)
            if not e:
                continue
            e.pop("_t0", None)
            entries.append(e)
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "gui_agent.browser.HarRecorder", "version": "1.0"},
                "entries": entries,
            }
        }
        try:
            with open(path, "w") as fh:
                json.dump(har, fh)
        except Exception as exc:  # noqa: BLE001
            return f"failed: write {path} ({exc})"
        return f"OK har {len(entries)} entries -> {path}"
