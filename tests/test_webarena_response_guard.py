import sys
import types

from gui_agent.adapters.browser.webarena import (
    WAResponse,
    _completed_mutate_response,
    _eval_compat_probe_urls_for_task,
    _finalize_response,
    _guess_webarena_task_type,
    _compile_failure_response,
    _run_official_eval,
    _synthesize_response,
    _official_eval_summary,
    _print_program,
    _task_for_eval_compat,
    _warn_if_pre_loop_page_changed,
    _write_webarena_report_context,
)
from gui_agent.core.orchestrator import CodingProgram
from gui_agent.core.run.result import AgentResult


def test_print_program_renders_reviewed_python(capsys):
    _print_program(CodingProgram(
        goal="read material",
        source="def run(ctx):\n    return 'Cotton'",
    ))

    output = capsys.readouterr().out
    assert "coding orchestrator program" in output
    assert "def run(ctx)" in output


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


def test_retrieve_success_without_data_is_not_success():
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=None,
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


def test_completed_coding_retrieve_uses_json_return_without_output_llm():
    returned = [{"month": "April", "count": 9}]
    resp = _synthesize_response(
        "Return monthly completed order counts",
        _result(
            task_type="RETRIEVE",
            phase="completed",
            verification="confirmed",
            output='[{"month": "April", "count": 9}]',
            orchestrator={"kind": "coding", "run_log": []},
        ),
    )
    assert resp == WAResponse(
        task_type="RETRIEVE",
        status="SUCCESS",
        retrieved_data=returned,
        error_details=None,
    )


def test_completed_coding_retrieve_accepts_unverified_read_evidence():
    resp = _synthesize_response(
        "Return matching customer nicknames",
        _result(
            task_type="RETRIEVE",
            phase="completed",
            verification="accepted_unverified",
            output='["Emma", "seam miller"]',
            orchestrator={"kind": "coding", "run_log": []},
        ),
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == ["Emma", "seam miller"]


def test_completed_coding_retrieve_wraps_scalar_for_webarena_protocol():
    resp = _synthesize_response(
        "Return the total as a number only",
        _result(
            task_type="RETRIEVE",
            phase="completed",
            verification="confirmed",
            output="182.4",
            orchestrator={"kind": "coding", "run_log": []},
        ),
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == [182.4]


def test_completed_coding_ui_state_uses_program_effect_not_intent_guess():
    resp = _synthesize_response(
        "Show the orders report from May 1, 2021 to March 31, 2022.",
        _result(
            task_type="browser",
            phase="completed",
            verification="confirmed",
            output='{"token":"c1:state","postcondition":{"rendered":true}}',
            orchestrator={
                "kind": "coding",
                "effect": "ui_state",
                "run_log": [{"coding_op": "reach"}],
            },
        ),
    )

    assert resp == WAResponse(
        task_type="NAVIGATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


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


def test_compile_failure_response_is_deterministic_error():
    resp = _compile_failure_response(
        "Tell me the top search terms",
        _result(
            summary="orchestrator compile failed: REF_NOT_IN_SCOPE",
            output="orchestrator compile failed: REF_NOT_IN_SCOPE",
        ),
    )

    assert resp.task_type == "RETRIEVE"
    assert resp.status == "DATA_VALIDATION_ERROR"
    assert resp.retrieved_data is None
    assert "REF_NOT_IN_SCOPE" in (resp.error_details or "")


def test_pre_loop_page_drift_warns(capsys):
    class _Device:
        def page_info(self):
            return "http://localhost/admin/review/product/index/", "Reviews"

    _warn_if_pre_loop_page_changed(
        _Device(),
        initial_url="http://localhost/admin/admin/dashboard/",
        initial_title="Dashboard",
    )

    out = capsys.readouterr().out
    assert "pre-loop page changed after initial observe" in out
    assert "/admin/admin/dashboard/" in out
    assert "/admin/review/product/index/" in out


def test_pre_loop_page_drift_ignores_same_url(capsys):
    class _Device:
        def page_info(self):
            return "http://localhost/admin/admin/dashboard", "Dashboard"

    _warn_if_pre_loop_page_changed(
        _Device(),
        initial_url="http://localhost/admin/admin/dashboard/",
        initial_title="Dashboard",
    )

    assert capsys.readouterr().out == ""


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
    )

    data = context_path.read_text(encoding="utf-8")
    assert '"eval_result_path"' in data
    assert '"score": 1.0' in data


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
