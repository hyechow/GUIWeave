import sys
import types

from gui_agent.adapters.browser.webarena import (
    WAResponse,
    _finalize_response,
    _run_official_eval,
    _official_eval_summary,
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
