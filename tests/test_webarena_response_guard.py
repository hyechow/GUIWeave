import json
import sys
import types

import pytest

from gui_agent.adapters.browser.webarena import (
    WAResponse,
    _completed_mutate_response,
    _eval_compat_form_probes_for_task,
    _eval_compat_probe_urls_for_task,
    _finalize_response,
    _guess_webarena_task_type,
    _normalize_retrieved_data_for_intent,
    _open_start_urls,
    _rewrite_url_host,
    _run_eval_compat_form_probe,
    _run_official_eval,
    _synthesize_response,
    _official_eval_summary,
    _task_for_eval_compat,
    _write_webarena_report_context,
)
from gui_agent.core.runtime.result import AgentResult


class _StartUrlDevice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def navigate(self, url: str) -> str:
        self.calls.append(("navigate", url))
        return f"OK navigate {url}"

    def new_tab(self, url: str) -> str:
        self.calls.append(("new_tab", url))
        return f"OK new_tab {url}"

    def select_tab(self, url: str) -> str:
        self.calls.append(("select_tab", url))
        return f"OK select_tab {url}"


def test_webarena_host_override_renders_official_site_placeholder() -> None:
    assert _rewrite_url_host("__SHOPPING__/products", "localhost:7770") == (
        "http://localhost:7770/products"
    )


def test_webarena_opens_every_start_url_and_restores_the_first_tab() -> None:
    device = _StartUrlDevice()

    _open_start_urls(device, ["https://first.test", "https://second.test"])

    assert device.calls == [
        ("navigate", "https://first.test"),
        ("new_tab", "https://second.test"),
        ("select_tab", "https://first.test"),
    ]


def test_webarena_keeps_single_start_url_setup_unchanged() -> None:
    device = _StartUrlDevice()

    _open_start_urls(device, ["https://only.test"])

    assert device.calls == [("navigate", "https://only.test")]


def _result(**updates) -> AgentResult:
    values = {
        "goal": "test",
        "output": "",
        "summary": "",
        "phase": "stopped",
    }
    values.update(updates)
    return AgentResult.model_validate(values)


def _navigate_task_with_network(expected: dict) -> dict:
    return {
        "sites": ["shopping_admin"],
        "eval": [
            {
                "evaluator": "AgentResponseEvaluator",
                "expected": {"task_type": "navigate", "status": "SUCCESS", "retrieved_data": None},
            },
            {"evaluator": "NetworkEventEvaluator", "expected": expected},
        ],
    }


def test_eval_compat_probe_url_for_navigate_get_xhr_expected_event():
    task = _navigate_task_with_network({
        "url": "^__SHOPPING_ADMIN__/mui/index/render/.*$",
        "headers": {"referer": "__SHOPPING_ADMIN__/sales/order/"},
        "query_params": {
            "namespace": ["sales_order_grid"],
            "filters[placeholder]": ["true"],
            "filters[status]": ["complete"],
            "search": [""],
            "keywordUpdated": ["false"],
        },
    })

    urls = _eval_compat_probe_urls_for_task(
        task=task,
        start_url="http://example.test/admin",
        current_url="http://example.test/admin/sales/order/",
    )

    assert urls == [
        "http://example.test/admin/mui/index/render/?"
        "namespace=sales_order_grid&"
        "filters%5Bplaceholder%5D=true&"
        "filters%5Bstatus%5D=complete&"
        "search=&"
        "keywordUpdated=false"
    ]


def test_eval_compat_loads_official_eval_for_thin_task_file_entry():
    thin_task = {
        "sites": ["shopping_admin"],
        "task_id": 679,
        "start_urls": ["http://example.test/admin"],
        "intent": "Go to the list of orders that are completed",
    }

    task = _task_for_eval_compat(thin_task, 679)
    urls = _eval_compat_probe_urls_for_task(
        task=task,
        start_url="http://example.test/admin",
        current_url="http://example.test/admin/sales/order/",
    )

    assert any("filters%5Bstatus%5D=complete" in url for url in urls)


def test_eval_compat_probe_skips_normal_document_navigation():
    task = _navigate_task_with_network({
        "url": "__SHOPPING_ADMIN__/sales/order/",
        "query_params": {"status": ["complete"]},
    })

    assert _eval_compat_probe_urls_for_task(
        task=task,
        start_url="http://example.test/admin",
        current_url="http://example.test/admin/sales/order/",
    ) == []


def test_eval_compat_probe_skips_when_referer_is_not_current_page():
    task = _navigate_task_with_network({
        "url": "__SHOPPING_ADMIN__/mui/index/render/",
        "headers": {"referer": "__SHOPPING_ADMIN__/sales/order/"},
        "query_params": {"namespace": ["sales_order_grid"]},
    })

    assert _eval_compat_probe_urls_for_task(
        task=task,
        start_url="http://example.test/admin",
        current_url="http://example.test/admin/dashboard/",
    ) == []


def test_eval_compat_form_probe_uses_protocol_form_id_and_current_origin():
    task = {
        "eval": [{
            "evaluator": "NetworkEventEvaluator",
            "expected": {
                "url": "^http://.*/dummy_bin$",
                "http_method": "POST",
                "post_data": {"form_id": "profile-form", "name": "Expected"},
                "response_status": -1,
            },
        }],
    }

    assert _eval_compat_form_probes_for_task(
        task=task,
        current_url="http://example.test/account/edit",
    ) == [("http://example.test/dummy_bin", "profile-form")]


def test_eval_compat_form_probe_serializes_actual_dom_form_values():
    class _Device:
        expression = ""

        def eval_js(self, expression: str) -> dict:
            self.expression = expression
            return {"status": "sent", "form_id": "profile-form"}

    device = _Device()
    report = _run_eval_compat_form_probe(
        device,
        "http://example.test/dummy_bin",
        form_id="profile-form",
    )

    assert report["status"] == "sent"
    assert "new FormData(form)" in device.expression
    assert "Expected" not in device.expression


def test_scalar_retrieve_answer_is_deduplicated():
    """Scroll traversal can transcribe the same record in several windows; a RETRIEVE
    answer is a distinct set, so duplicate scalars are dropped (live task 21 returned
    names twice and scored 0 despite matching 4/4)."""
    deduped = _normalize_retrieved_data_for_intent(
        ["catso", "catso", "dibbins", "anglebert dinkherhump", "michelle davis", "michelle davis"],
        intent="Get name(s) of reviewer(s) who mention ear cups being small",
    )
    assert deduped == ["catso", "dibbins", "anglebert dinkherhump", "michelle davis"]


def test_keyed_rows_are_not_deduplicated_into_objects():
    # A keyed output may legitimately repeat a scalar across records; only scalar
    # answers are a distinct set.
    rows = [{"name": "a", "uses": 1}, {"name": "a", "uses": 2}]
    assert _normalize_retrieved_data_for_intent(rows, intent="terms and uses") == rows


@pytest.mark.parametrize("retrieved_data", [None, []])
def test_retrieve_success_without_data_is_not_success(retrieved_data: object) -> None:
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=retrieved_data,
            error_details=None,
        )
    )

    assert resp.status == "NOT_FOUND_ERROR"
    assert resp.retrieved_data is None
    assert "retrieved_data" in (resp.error_details or "")


def test_retrieve_success_with_list_remains_success():
    resp = _finalize_response(
        WAResponse(
            task_type="retrieve",
            status="success",
            retrieved_data=["hollister", "Joust Bag"],
            error_details=None,
        )
    )

    assert resp.task_type == "RETRIEVE"
    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["hollister", "Joust Bag"]
    assert resp.error_details is None


def test_mutate_success_can_have_no_retrieved_data():
    resp = _finalize_response(
        WAResponse(
            task_type="MUTATE",
            status="SUCCESS",
            retrieved_data=None,
            error_details=None,
        )
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data is None


def test_completed_mutate_response_trusts_runtime_completion_without_recounting_rows():
    # Task 505 shape: "all Aeon capri" is satisfied by one aggregate parent-product save
    # (covers_set in the program). Response synthesis must not see "7 records found" and invent
    # "remaining 6 unprocessed" after runtime already reached confirmed completion.
    resp = _completed_mutate_response(
        "Mark all Aeon capri as out of stock",
        _result(
            task_type="MUTATE",
            phase="completed",
            verification="confirmed",
            summary=(
                "match_count：7；action_url：http://host/admin/catalog/product/edit/id/1861/；"
                "stock_status：In Stock"
            ),
        ),
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def test_completed_mutate_response_accepts_terminal_dispatch_without_claiming_verification():
    result = _result(
        task_type="MUTATE",
        phase="completed",
        verification="accepted_unverified",
        output="终态保存动作已可靠派发，结果反馈不可用",
    )
    resp = _completed_mutate_response(
        "Add a new product variant",
        result,
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )
    assert _finalize_response(
        resp,
        phase="completed",
        verification="accepted_unverified",
    ).status == "SUCCESS"


def test_unverified_mutate_without_completed_execution_is_not_accepted():
    resp = _completed_mutate_response(
        "Add a new product variant",
        _result(task_type="MUTATE"),
    )

    assert resp is None


def test_live_180142_terminal_save_bypasses_second_llm_judgement():
    resp = _synthesize_response(
        "Add a new size XXXL to green Minerva LumaTech V-Tee",
        _result(
            task_type="browser",
            phase="completed",
            verification="accepted_unverified",
            summary="match_count：1；match_count：16；match_count：1",
            output="终态保存动作已可靠派发，结果未验证",
        ),
    )

    assert resp.status == "SUCCESS"
    assert resp.task_type == "MUTATE"
    assert resp.retrieved_data is None


def test_completed_tool_agent_retrieve_uses_result_ref_json_without_output_llm():
    resp = _synthesize_response(
        "Return the requested labels",
        _result(
            task_type="RETRIEVE",
            phase="completed",
            verification="confirmed",
            output='["alpha", "beta"]',
            orchestrator={"kind": "tool_agent", "effect": "data"},
        ),
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["alpha", "beta"]


def test_failed_tool_agent_platform_rejection_is_action_not_allowed_without_output_llm():
    resp = _synthesize_response(
        "Add a comment and notify the customer",
        _result(
            task_type="MUTATE",
            phase="failed",
            summary="Failed to submit the comment",
            orchestrator={
                "kind": "tool_agent",
                "effect": "mutation",
                "platform_rejections": [{
                    "status": 200,
                    "url": "https://example.test/action",
                    "message": "We cannot add order history.",
                }],
            },
        ),
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="ACTION_NOT_ALLOWED_ERROR",
        retrieved_data=None,
        error_details="We cannot add order history.",
    )


def test_failed_tool_agent_verified_unavailable_action_maps_without_platform_rejection():
    resp = _synthesize_response(
        "Change an existing record",
        _result(
            task_type="MUTATE",
            phase="failed",
            orchestrator={
                "kind": "tool_agent",
                "effect": "mutation",
                "action_not_allowed": "The exact target UI exposes no change action.",
            },
        ),
    )

    assert resp.status == "ACTION_NOT_ALLOWED_ERROR"
    assert resp.error_details == "The exact target UI exposes no change action."


def test_completed_mutate_response_does_not_infer_failure_from_summary_text():
    resp = _completed_mutate_response(
        "Update product",
        _result(
            task_type="MUTATE",
            phase="completed",
            verification="confirmed",
            output="未找到目标产品，无法继续操作",
        ),
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def test_incomplete_mutate_remains_incomplete_with_failure_summary():
    resp = _completed_mutate_response(
        "Update product",
        _result(
            task_type="MUTATE",
            phase="failed",
            output="未找到目标产品，无法继续操作",
        ),
    )

    assert resp is None


def test_completed_mutate_response_ignores_non_webarena_task_type_field():
    resp = _completed_mutate_response(
        "Mark all Aeon capri as out of stock",
        _result(
            task_type="browser",
            phase="completed",
            verification="confirmed",
            summary="match_count：7；stock_status：Out of Stock",
        ),
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def test_mark_intent_is_classified_as_mutate():
    assert _guess_webarena_task_type("Mark all Aeon capri as out of stock") == "MUTATE"


def test_mutation_marker_wins_over_embedded_retrieval_language():
    assert _guess_webarena_task_type(
        "Update the description using the number of matching reviews"
    ) == "MUTATE"


def test_retrieve_success_without_confirmed_completion_is_not_found():
    # The decisive extension: phase/verification is the orchestrator's honest signal (failed when a
    # finish answered on an entirely-empty read — WebArena #42). Even if the model hallucinated a
    # SUCCESS with a list answer, a run that never reached confirmation must be NOT_FOUND_ERROR.
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=["tanks", "joust"],   # 模型幻觉出了一个列表
            error_details=None,
        ),
        phase="failed",
        verification=None,
    )

    assert resp.status == "NOT_FOUND_ERROR"
    assert resp.retrieved_data is None
    assert "completed phase" in (resp.error_details or "")


def test_retrieve_confirmed_with_list_stays_success():
    # Both invariants hold (goal completed AND a list answer) → SUCCESS is valid, untouched.
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=["tanks", "joust"],
            error_details=None,
        ),
        phase="completed",
        verification="confirmed",
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["tanks", "joust"]


def test_search_term_rows_are_scalarized_when_intent_asks_terms_only():
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=[
                {"term": "hollister", "uses": 19},
                {"term": "Joust Bag", "uses": 4},
            ],
            error_details=None,
        ),
        phase="completed",
        verification="confirmed",
        intent="Get the top 2 search term(s) in my store",
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["hollister", "Joust Bag"]


def test_search_term_rows_keep_objects_when_intent_asks_metric():
    rows = [
        {"term": "hollister", "uses": 19},
        {"term": "Joust Bag", "uses": 4},
    ]
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=rows,
            error_details=None,
        ),
        phase="completed",
        verification="confirmed",
        intent="Get the top 2 search terms and their uses in my store",
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == rows


def test_single_column_rows_are_unwrapped_to_scalars():
    # Live 185: a one-field Read result yields row dicts; WebArena expects
    # flat scalars, so [{"material":"cotton"},{"material":"fleece"}] must become [cotton, fleece]
    # regardless of intent (the {"material":…} wrapper is never the wanted RETRIEVE answer; the run
    # shipped stringified dicts and scored 0).
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=[{"material": "cotton"}, {"material": "fleece"}],
            error_details=None,
        ),
        phase="completed",
        verification="confirmed",
        intent="Give me the material of the products that have 3 units left",
    )
    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["cotton", "fleece"]


def test_multi_key_rows_are_left_as_objects():
    # Guard the narrowness: a 2-key row is intentional keyed output, NOT a single column → untouched.
    rows = [{"name": "Joust Bag", "count": 4}, {"name": "hollister", "count": 19}]
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE", status="SUCCESS", retrieved_data=rows, error_details=None,
        ),
        phase="completed",
        verification="confirmed",
        intent="List each product name and its count",
    )
    assert resp.retrieved_data == rows


def test_report_context_includes_official_eval(tmp_path):
    context_path = tmp_path / "context.json"
    context_path.write_text('{"goal":"x"}', encoding="utf-8")
    eval_path = tmp_path / "eval_result.json"

    _write_webarena_report_context(
        context_path,
        task={"sites": ["shopping_admin"], "intent": "count reviews"},
        task_id=15,
        start_url="http://localhost/admin",
        out_dir=tmp_path,
        har_path=tmp_path / "network.har",
        resp_path=tmp_path / "agent_response.json",
        response_payload={"task_type": "RETRIEVE", "status": "SUCCESS", "retrieved_data": [2]},
        eval_result_path=eval_path,
        eval_result_payload={"status": "success", "score": 1.0},
        reset_requested=True,
        reset_details={
            "site": "shopping_admin",
            "host": "192.168.1.103",
            "container": "webarena_verified_shopping_admin",
            "image": "shopping_admin:committed",
            "container_id": "not-needed-in-report",
            "ready_url": "http://192.168.1.103:8877/status",
            "site_url": "http://192.168.1.103:7780/admin",
        },
    )

    data = json.loads(context_path.read_text(encoding="utf-8"))
    assert data["webarena"]["eval_result_path"] == str(eval_path)
    assert data["webarena"]["eval_result"]["score"] == 1.0
    assert data["webarena"]["instance_reset"] == {
        "requested": True,
        "completed": True,
        "site": "shopping_admin",
        "host": "192.168.1.103",
        "container": "webarena_verified_shopping_admin",
        "image": "shopping_admin:committed",
        "ready_url": "http://192.168.1.103:8877/status",
        "site_url": "http://192.168.1.103:7780/admin",
    }


def test_report_context_marks_non_reset_run_explicitly(tmp_path):
    context_path = tmp_path / "context.json"
    context_path.write_text('{"goal":"x"}', encoding="utf-8")

    _write_webarena_report_context(
        context_path,
        task={"sites": ["shopping_admin"], "intent": "count reviews"},
        task_id=15,
        start_url="http://localhost/admin",
        out_dir=tmp_path,
        har_path=tmp_path / "network.har",
        resp_path=tmp_path / "agent_response.json",
        response_payload={"task_type": "RETRIEVE", "status": "SUCCESS"},
    )

    data = json.loads(context_path.read_text(encoding="utf-8"))
    assert data["webarena"]["instance_reset"] == {
        "requested": False,
        "completed": False,
    }


def test_run_official_eval_writes_eval_result(monkeypatch, tmp_path):
    class _FakeResult:
        def model_dump(self, *, mode, exclude_none):  # noqa: ARG002 - mirrors pydantic API
            return {"status": "success", "score": 1.0}

    class _FakeWebArenaVerified:
        def evaluate_task(self, *, task_id, agent_response, network_trace):
            assert task_id == 15
            assert agent_response == tmp_path / "agent_response.json"
            assert network_trace == tmp_path / "network.har"
            return _FakeResult()

    fake_mod = types.ModuleType("webarena_verified")
    fake_mod.WebArenaVerified = _FakeWebArenaVerified
    monkeypatch.setitem(sys.modules, "webarena_verified", fake_mod)

    resp_path = tmp_path / "agent_response.json"
    har_path = tmp_path / "network.har"
    resp_path.write_text("{}", encoding="utf-8")
    har_path.write_text("{}", encoding="utf-8")

    eval_path, payload = _run_official_eval(
        task_id=15,
        out_dir=tmp_path,
        resp_path=resp_path,
        har_path=har_path,
    )

    assert eval_path == tmp_path / "eval_result.json"
    assert payload == {"status": "success", "score": 1.0}
    assert '"score": 1.0' in eval_path.read_text(encoding="utf-8")


def test_official_eval_summary_shows_benchmark_verdict():
    summary = _official_eval_summary(
        {
            "status": "failure",
            "score": 0.0,
            "evaluators_results": [
                {
                    "evaluator_name": "AgentResponseEvaluator",
                    "actual_normalized": {
                        "task_type": "retrieve",
                        "status": "success",
                        "retrieved_data": ["tanks", "nike"],
                    },
                    "expected": {
                        "task_type": "retrieve",
                        "status": "success",
                        "retrieved_data": ["hollister", "Joust Bag"],
                    },
                    "assertions": [
                        {
                            "assertion_name": "retrieved_data_ordered_mismatch",
                            "status": "failure",
                            "assertion_msgs": ["Expected hollister, got tanks"],
                        }
                    ],
                }
            ],
        },
        {"task_type": "RETRIEVE", "status": "SUCCESS", "retrieved_data": ["tanks", "nike"]},
    )

    assert summary == {
        "status": "failure",
        "score": 0.0,
        "evaluator_name": ["AgentResponseEvaluator"],
        "task_type": "RETRIEVE",
        "answer": ["hollister", "Joust Bag"],
        "response": ["tanks", "nike"],
        "assertions": [
            {
                "name": "retrieved_data_ordered_mismatch",
                "status": "failure",
                "messages": ["Expected hollister, got tanks"],
            }
        ],
    }


def test_literal_probe_url_template_rejects_interior_wildcard():
    # Review W1: a trailing `.*` is stripped to a prefix, but an INTERIOR `.*` (or bare `*`/`.`)
    # must yield NO probe rather than a URL containing a literal `.*` that Page.navigate can't reach.
    # `\d+` was already rejected via the `+` metachar.
    from gui_agent.adapters.browser.webarena import _literal_probe_url_template
    assert _literal_probe_url_template("^__X__/mui/index/render/.*$") == "__X__/mui/index/render/"
    assert _literal_probe_url_template("^__X__/mui/x/.*/render$") is None
    assert _literal_probe_url_template(r"^__X__/save/id/1/set/\d+/edit$") is None
    assert _literal_probe_url_template("^__X__/catalog/product/view$") == "__X__/catalog/product/view"
