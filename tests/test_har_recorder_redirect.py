"""Offline regression for the HarRecorder redirect bug that made every mutation task score 0.

Magento admin Save is a form POST → 302 → GET (edit/list). Chrome re-fires requestWillBeSent for
the redirect under the SAME requestId; the recorder used to overwrite the POST entry with the
redirect-target GET, so the save POST the NetworkEventEvaluator matches on vanished (real capture:
988 GET, 0 POST → actual:[] → 0 despite a genuine save). The fix archives the pre-redirect request
with its 302 response instead of overwriting it."""
from __future__ import annotations

import json
import os
import tempfile

from gui_agent.adapters.browser.har_recorder import HarRecorder


def _dump(rec: HarRecorder) -> dict:
    fd, path = tempfile.mkstemp(suffix=".har")
    os.close(fd)
    try:
        rec.dump(path)
        return json.load(open(path))
    finally:
        os.unlink(path)


def test_start_records_all_existing_tabs_without_request_id_collisions():
    class _Session:
        def __init__(self):
            self.listeners = {}

        def send(self, method, params):
            assert (method, params) == ("Network.enable", {})

        def on(self, event, callback):
            self.listeners[event] = callback

    sessions = [_Session(), _Session()]
    context = type("Context", (), {
        "new_cdp_session": lambda self, page: sessions[page.index],
    })()
    pages = [
        type("Page", (), {"index": index, "context": context})()
        for index in range(2)
    ]
    device = type("Device", (), {"_all_pages": lambda self: pages})()
    rec = HarRecorder(device).start()

    for index, session in enumerate(sessions):
        session.listeners["Network.requestWillBeSent"]({
            "requestId": "same-id",
            "timestamp": index,
            "request": {"method": "POST", "url": f"http://tab-{index}/add"},
        })

    assert len(rec._sessions) == 2
    assert all("Network.loadingFailed" in session.listeners for session in sessions)
    assert [entry["request"]["url"] for entry in _dump(rec)["log"]["entries"]] == [
        "http://tab-0/add", "http://tab-1/add",
    ]


def test_redirect_preserves_pre_redirect_save_post():
    rec = HarRecorder(device=object())
    rid = "REQ-1"
    save_url = "http://host/admin/sales_rule/promo_quote/save"
    # 1) the save POST fires
    rec._on_request({
        "requestId": rid, "wallTime": 1.0, "timestamp": 100.0,
        "request": {
            "method": "POST", "url": save_url,
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "postData": "name=mother%27s+day+sale&website_ids%5B0%5D=1&customer_group_ids%5B0%5D=1",
        },
    })
    # 2) redirect: SAME requestId re-fires with a redirectResponse (302) + the GET target
    rec._on_request({
        "requestId": rid, "wallTime": 1.1, "timestamp": 101.0,
        "redirectResponse": {"status": 302, "statusText": "Found", "headers": {}, "url": save_url},
        "request": {"method": "GET", "url": "http://host/admin/sales_rule/promo_quote/edit/id/5/", "headers": {}},
    })
    # 3) the GET target completes
    rec._on_response({"requestId": rid, "response": {"status": 200, "statusText": "OK", "headers": {}, "mimeType": "text/html"}})
    rec._on_finished({"requestId": rid, "timestamp": 102.0})

    entries = _dump(rec)["log"]["entries"]
    posts = [e for e in entries if e["request"]["method"] == "POST" and "promo_quote/save" in e["request"]["url"]]
    assert len(posts) == 1, [(e["request"]["method"], e["request"]["url"]) for e in entries]
    assert posts[0]["response"]["status"] == 302               # POST carries its 302 redirect response
    assert posts[0]["request"].get("postData", {}).get("text")  # post body preserved for the evaluator
    # the redirect-target GET is still present too
    assert any(e["request"]["method"] == "GET" and "edit/id/5" in e["request"]["url"] for e in entries)


def test_plain_get_still_recorded_once():
    # A non-redirected GET is unaffected.
    rec = HarRecorder(device=object())
    rec._on_request({"requestId": "R2", "wallTime": 1.0, "timestamp": 1.0,
                     "request": {"method": "GET", "url": "http://host/admin/", "headers": {}}})
    rec._on_finished({"requestId": "R2", "timestamp": 2.0})
    entries = _dump(rec)["log"]["entries"]
    assert len(entries) == 1 and entries[0]["request"]["method"] == "GET"


def test_finished_json_response_captures_body_from_originating_session():
    class _Session:
        def send(self, method, params):
            assert (method, params) == (
                "Network.getResponseBody", {"requestId": "JSON"},
            )
            return {
                "body": "eyJpdGVtc19xdHkiOjF9",
                "base64Encoded": True,
            }

    rec = HarRecorder(device=object())
    rec._sessions = [_Session()]
    rec._on_request({
        "requestId": "JSON", "timestamp": 1.0,
        "request": {"method": "GET", "url": "http://host/api/totals"},
    }, "0")
    rec._on_response({
        "requestId": "JSON",
        "response": {"status": 200, "mimeType": "application/json"},
    }, "0")
    rec._on_finished({
        "requestId": "JSON", "timestamp": 2.0,
    }, "0")

    response = _dump(rec)["log"]["entries"][0]["response"]
    assert response["content"] == {
        "size": 15,
        "mimeType": "application/json",
        "text": '{"items_qty":1}',
    }


def test_failed_request_keeps_post_body_and_uses_playwright_status():
    rec = HarRecorder(device=object())
    rec._on_request({
        "requestId": "FAILED",
        "timestamp": 1.0,
        "request": {
            "method": "POST",
            "url": "http://host/transient",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "postData": "form_id=profile&name=Test",
        },
    })
    rec._on_failed({
        "requestId": "FAILED",
        "timestamp": 1.1,
        "errorText": "net::ERR_ABORTED",
    })

    entry = _dump(rec)["log"]["entries"][0]
    assert entry["response"]["status"] == -1
    assert entry["response"]["_failureText"] == "net::ERR_ABORTED"
    assert entry["request"]["postData"]["text"] == "form_id=profile&name=Test"


def test_dump_orders_finished_entries_by_cdp_timestamp_not_arrival_order():
    rec = HarRecorder(device=object())
    rec._on_request({
        "requestId": "NEWER",
        "wallTime": 20.0,
        "timestamp": 20.0,
        "request": {
            "method": "GET",
            "url": (
                "http://host/admin/mui/index/render/?namespace=sales_order_grid"
                "&filters%5Bstatus%5D=complete"
            ),
            "headers": {"Accept": "*/*"},
        },
    })
    rec._on_finished({"requestId": "NEWER", "timestamp": 21.0})
    rec._on_request({
        "requestId": "OLDER",
        "wallTime": 10.0,
        "timestamp": 10.0,
        "request": {
            "method": "GET",
            "url": "http://host/admin",
            "headers": {"Accept": "text/html"},
        },
    })
    rec._on_finished({"requestId": "OLDER", "timestamp": 11.0})

    first_dump = _dump(rec)["log"]["entries"]
    second_dump = _dump(rec)["log"]["entries"]

    assert [e["request"]["url"] for e in first_dump] == [
        "http://host/admin",
        "http://host/admin/mui/index/render/?namespace=sales_order_grid&filters%5Bstatus%5D=complete",
    ]
    assert [e["request"]["url"] for e in second_dump] == [e["request"]["url"] for e in first_dump]
    assert all("_t0" not in e and "_seq" not in e for e in first_dump)


def test_extra_info_content_type_backfills_post_data_mime_type():
    rec = HarRecorder(device=object())
    rec._on_request({
        "requestId": "R3", "wallTime": 1.0, "timestamp": 1.0,
        "request": {
            "method": "POST",
            "url": "http://host/admin/sales/order/addComment/order_id/65/?isAjax=true",
            "headers": {},
            "postData": "history%5Bcomment%5D=hello&history%5Bis_customer_notified%5D=1",
        },
    })
    rec._on_request_extra_info({
        "requestId": "R3",
        "headers": {"Content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
    })

    entry = _dump(rec)["log"]["entries"][0]

    assert entry["request"]["postData"]["mimeType"] == "application/x-www-form-urlencoded; charset=UTF-8"


def test_pending_extra_info_backfills_post_data_mime_type():
    rec = HarRecorder(device=object())
    rec._on_request_extra_info({
        "requestId": "R4",
        "headers": {"Content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
    })
    rec._on_request({
        "requestId": "R4", "wallTime": 1.0, "timestamp": 1.0,
        "request": {
            "method": "POST",
            "url": "http://host/admin/sales/order/addComment/order_id/65/?isAjax=true",
            "headers": {},
            "postData": "history%5Bcomment%5D=hello&history%5Bis_customer_notified%5D=1",
        },
    })

    entry = _dump(rec)["log"]["entries"][0]

    assert entry["request"]["postData"]["mimeType"] == "application/x-www-form-urlencoded; charset=UTF-8"
