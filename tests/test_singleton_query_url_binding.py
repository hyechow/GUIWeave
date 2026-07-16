from gui_agent.core.orchestrator import Cond, If, Program, Query, Run, validate_program
from gui_agent.core.orchestrator.passes import bind_singleton_query_urls


def _singleton_url_program(*, url_fields: list[str] | None = None) -> Program:
    returns = ["match_count", *(url_fields or ["detail_url"])]
    return Program(
        statements=[
            Query(
                var="q_parent",
                name="Find the unique owner entry",
                returns=returns,
                sql=(
                    "SELECT COUNT(*) AS match_count, "
                    "CASE WHEN COUNT(*) = 1 THEN MAX(Action) ELSE '' END AS detail_url "
                    "FROM candidates"
                ),
            ),
            If(
                cond=Cond(
                    var="q_parent",
                    field="match_count",
                    cmp="==",
                    value="1",
                ),
                then=[
                    Run(
                        name="Open the selected owner",
                        kind="navigation",
                        success_condition="The selected owner editor is visible",
                    )
                ],
            ),
        ]
    )


def test_binds_unique_query_url_to_singleton_guarded_navigation() -> None:
    source = _singleton_url_program()

    bound = bind_singleton_query_urls(source)

    branch = bound.statements[1]
    assert isinstance(branch, If)
    assert branch.then[0].name == "{q_parent[detail_url]}；Open the selected owner"
    assert source.statements[1].then[0].name == "Open the selected owner"
    assert "DATA_QUERY_URL_RESULT_UNUSED" not in {
        issue.code for issue in validate_program(bound)
    }


def test_singleton_query_url_binding_is_idempotent() -> None:
    once = bind_singleton_query_urls(_singleton_url_program())
    twice = bind_singleton_query_urls(once)

    assert once.model_dump() == twice.model_dump()


def test_does_not_guess_when_url_binding_is_ambiguous() -> None:
    source = _singleton_url_program(url_fields=["detail_url", "edit_url"])

    bound = bind_singleton_query_urls(source)

    branch = bound.statements[1]
    assert isinstance(branch, If)
    assert branch.then[0].name == "Open the selected owner"
    assert "DATA_QUERY_URL_RESULT_UNUSED" in {
        issue.code for issue in validate_program(bound)
    }


def test_does_not_bind_without_exact_singleton_guard() -> None:
    source = _singleton_url_program()
    branch = source.statements[1]
    assert isinstance(branch, If)
    source = source.model_copy(
        update={
            "statements": [
                source.statements[0],
                branch.model_copy(
                    update={
                        "cond": branch.cond.model_copy(
                            update={"cmp": "!=", "value": "0"}
                        )
                    }
                ),
            ]
        }
    )

    bound = bind_singleton_query_urls(source)

    bound_branch = bound.statements[1]
    assert isinstance(bound_branch, If)
    assert bound_branch.then[0].name == "Open the selected owner"


def test_production_draft_pipeline_applies_singleton_url_binding() -> None:
    from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, to_program

    draft = _PlanDraft(
        goal="Update the unique selected owner",
        steps=[
            _StepDraft(
                op="run",
                run_kind="data_query",
                var="q_parent",
                name="Find the unique owner entry",
                returns=["match_count", "detail_url"],
                sql=(
                    "SELECT COUNT(*) AS match_count, "
                    "CASE WHEN COUNT(*) = 1 THEN MAX(Action) ELSE '' END AS detail_url "
                    "FROM candidates"
                ),
            ),
            _StepDraft(
                op="if",
                cond_var="q_parent",
                cond_field="match_count",
                cond_cmp="==",
                cond_value="1",
                then=[
                    _StepDraft(
                        op="run",
                        run_kind="navigation",
                        name="Open the selected owner",
                    )
                ],
            ),
        ],
    )

    program = to_program(draft, draft.goal)

    branch = program.statements[1]
    assert isinstance(branch, If)
    assert branch.then[0].name.startswith("{q_parent[detail_url]}；")
