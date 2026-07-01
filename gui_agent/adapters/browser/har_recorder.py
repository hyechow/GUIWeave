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

``Network.requestWillBeSent.request.headers`` only carries headers the
page/renderer explicitly set — browser-injected ones (``Accept``, ``Sec-Fetch-*``,
``Cookie``) are added by the network service afterward and only show up on the
separate ``Network.requestWillBeSentExtraInfo`` event. Without merging that event
in, every captured request looks like it has no ``Accept`` header, which makes
WebArena's ``NetworkEvent.is_navigation_event`` (gated on ``Accept: text/html`` or
the Sec-Fetch-Dest/Mode/User trio) always False even for real top-level
navigations — so we subscribe to both events and merge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl


def _headers_list(headers: dict | None) -> list[dict]:
    return [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()]


def _merge_headers(existing: list[dict], extra: dict) -> list[dict]:
    """Merge ``extra`` (name->value) into ``existing`` (HAR header-entry list),
    overwriting by case-insensitive name and appending names not already present."""
    by_lower = {h["name"].lower(): h for h in existing}
    for name, value in extra.items():
        key = name.lower()
        if key in by_lower:
            by_lower[key]["value"] = str(value)
        else:
            new_h = {"name": str(name), "value": str(value)}
            by_lower[key] = new_h
            existing.append(new_h)
    return existing


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
        # A redirect (POST → 302 → GET) reuses ONE requestId and fires requestWillBeSent again
        # with a redirectResponse. The pre-redirect request (e.g. the Magento save POST the
        # NetworkEventEvaluator matches on) must be archived here BEFORE it is overwritten by the
        # redirect target, or it is lost and every mutation task scores 0 despite a real save.
        self._redirected: list[dict] = []
        # requestId -> extra-info headers seen before requestWillBeSent arrived
        # (CDP does not guarantee event order between the two).
        self._pending_extra_headers: dict[str, dict] = {}

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
            self._sess.on("Network.requestWillBeSentExtraInfo", self._on_request_extra_info)
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
            # A redirect (e.g. save POST → 302 → GET) reuses the same requestId and re-fires
            # requestWillBeSent with a redirectResponse. Archive the PRE-redirect request with that
            # 302 response instead of overwriting it — otherwise the POST the evaluator matches on is
            # lost to the redirect target (988 GETs, 0 POST; every mutation task scored 0).
            redirect = params.get("redirectResponse")
            prev = self._reqs.get(rid)
            if redirect and prev is not None:
                prev["response"].update({
                    "status": redirect.get("status", 0),
                    "statusText": redirect.get("statusText", ""),
                    "headers": _headers_list(redirect.get("headers")),
                    "redirectURL": redirect.get("url") or req.get("url", ""),
                })
                self._redirected.append(prev)
            if rid not in self._reqs:
                self._order.append(rid)
            self._reqs[rid] = entry
            extra = self._pending_extra_headers.pop(rid, None)
            if extra:
                _merge_headers(entry["request"]["headers"], extra)
        except Exception:
            pass

    def _on_request_extra_info(self, params: dict) -> None:
        try:
            rid = params.get("requestId")
            headers = params.get("headers") or {}
            if rid is None or not headers:
                return
            entry = self._reqs.get(rid)
            if entry is not None:
                _merge_headers(entry["request"]["headers"], headers)
            else:
                # requestWillBeSent hasn't arrived yet — stash for when it does.
                self._pending_extra_headers[rid] = headers
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

        collected = list(self._redirected) + [
            self._reqs[rid] for rid in self._order if self._reqs.get(rid)
        ]
        collected.sort(key=lambda e: float(e["_t0"]) if isinstance(e.get("_t0"), (int, float)) else 0.0)
        entries = []
        for e in collected:
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
