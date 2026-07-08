import sys
import types

from gui_agent.adapters.browser.webarena import (
    WAResponse,
    _completed_mutate_response,
    _finalize_response,
    _guess_webarena_task_type,
    _preflight_failure_response,
    _run_official_eval,
    _official_eval_summary,
    _warn_if_pre_loop_page_changed,
    _write_webarena_report_context,
)


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
    # "remaining 6 unprocessed" after runtime already reached goal_completed.
    resp = _completed_mutate_response(
        "Mark all Aeon capri as out of stock",
        {
            "task_type": "MUTATE",
            "goal_completed": True,
            "stop_reason": (
                "match_count：7；action_url：http://host/admin/catalog/product/edit/id/1861/；"
                "stock_status：In Stock"
            ),
            "result_summary": "",
        },
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def test_completed_mutate_response_does_not_mask_explicit_failure_text():
    resp = _completed_mutate_response(
        "Update product",
        {
            "task_type": "MUTATE",
            "goal_completed": True,
            "result_summary": "未找到目标产品，无法继续操作",
        },
    )

    assert resp is None


def test_completed_mutate_response_ignores_non_webarena_task_type_field():
    resp = _completed_mutate_response(
        "Mark all Aeon capri as out of stock",
        {
            "task_type": "browser",
            "goal_completed": True,
            "stop_reason": "match_count：7；stock_status：Out of Stock",
        },
    )

    assert resp == WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def test_mark_intent_is_classified_as_mutate():
    assert _guess_webarena_task_type("Mark all Aeon capri as out of stock") == "MUTATE"


def test_retrieve_success_goal_not_completed_is_not_found():
    # The decisive extension: goal_completed is the orchestrator's honest signal (False when a
    # finish answered on an entirely-empty read — WebArena #42). Even if the model hallucinated a
    # SUCCESS with a list answer, a run that never reached goal_completed must be NOT_FOUND_ERROR.
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=["tanks", "joust"],   # 模型幻觉出了一个列表
            error_details=None,
        ),
        goal_completed=False,
    )

    assert resp.status == "NOT_FOUND_ERROR"
    assert resp.retrieved_data is None
    assert "goal_completed" in (resp.error_details or "")


def test_retrieve_goal_completed_with_list_stays_success():
    # Both invariants hold (goal completed AND a list answer) → SUCCESS is valid, untouched.
    resp = _finalize_response(
        WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS",
            retrieved_data=["tanks", "joust"],
            error_details=None,
        ),
        goal_completed=True,
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
        goal_completed=True,
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
        goal_completed=True,
        intent="Get the top 2 search terms and their uses in my store",
    )

    assert resp.status == "SUCCESS"
    assert resp.retrieved_data == rows


def test_single_column_rows_are_unwrapped_to_scalars():
    # Live 185: a 1-column data_query (SELECT DISTINCT material) yields row dicts; webarena expects
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
        goal_completed=True,
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
        goal_completed=True,
        intent="List each product name and its count",
    )
    assert resp.retrieved_data == rows


def test_preflight_failure_response_is_deterministic_error():
    resp = _preflight_failure_response(
        "Tell me the top search terms",
        {
            "stop_reason": "orchestrator preflight failed: ROUTER_ENTITY_DROPPED",
            "result_summary": "orchestrator preflight failed: ROUTER_ENTITY_DROPPED",
        },
    )

    assert resp.task_type == "RETRIEVE"
    assert resp.status == "DATA_VALIDATION_ERROR"
    assert resp.retrieved_data is None
    assert "ROUTER_ENTITY_DROPPED" in (resp.error_details or "")


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
