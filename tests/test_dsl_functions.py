"""First-class DSL functions + pure compute: the DSL program is an ordinary code file —
control flow (functions / call / if / foreach), pure compute (deterministic derivation), and
linear GUI milestones (Run). The whole file (main + functions) is produced in ONE decompose;
each function is decomposed ONCE and called N times (no per-row re-decompose).

185 shape: a function resolve_parent_material(name) derives the base via a Compute (NOT a GUI
milestone — that separation is what unstuck the agent), runs one linear GUI milestone, returns
material; a foreach calls it per row. Driven synchronously with a mock executor (no LLM/live).
"""

import pytest

from gui_agent.core.orchestrator import (
    Call,
    Compute,
    Finish,
    ForEach,
    FunctionDef,
    Interpreter,
    Program,
    Run,
    RunResult,
    drive,
)
from gui_agent.core.orchestrator.safe_eval import SafeEvalError, safe_eval

_MATERIAL_OF = {"Minerva": "Cotton", "Eos": "Fleece"}


def _185_program() -> Program:
    return Program(
        goal="material of products with 3 units left",
        functions=[FunctionDef(
            name="resolve_parent_material", params=["name"], returns=["material"], body=[
                # PURE compute (interpreter derives the base; NOT a GUI milestone the agent runs)
                Compute(var="base", expr="re_sub('-[A-Za-z]+-[A-Za-z]+$', '', name)"),
                # ONE linear GUI milestone (search base → open Configurable parent → read material)
                Run(var="d", kind="read", returns=["material"],
                    name="在 Products 搜 {base}，打开 Type=Configurable 父产品，读 Material 主材质"),
            ])],
        statements=[
            Run(kind="filter", name="设 Quantity From=3 To=3"),
            ForEach(var="row", into="mats", returns=["Name"], body=[
                Call(func="resolve_parent_material", args={"name": "{row[Name]}"}, var="m"),
            ]),
            Finish(message="done"),
        ],
    )


def test_185_function_call_per_row_with_compute():
    program = _185_program()
    rows = [{"Name": "Minerva LumaTech V-Tee-XS-Blue"}, {"Name": "Eos V-Neck Hoodie-S-Blue"}]
    seen_milestones: list[str] = []

    def execute(run: Run) -> RunResult:
        if run.kind == "read":
            seen_milestones.append(run.name)  # the {base} was substituted by the interpreter
            brand = "Minerva" if "Minerva" in run.name else "Eos"
            return RunResult(completed=True, reads={"material": _MATERIAL_OF[brand]})
        return RunResult(completed=True)

    interp = Interpreter(program, collect_fn=lambda t, r, limit=None: rows)
    drive(interp, execute)

    # the Compute derived each base deterministically and the interpreter substituted it into the
    # milestone — the agent never saw "-SIZE-COLOR" or had to derive anything itself
    assert seen_milestones == [
        "在 Products 搜 Minerva LumaTech V-Tee，打开 Type=Configurable 父产品，读 Material 主材质",
        "在 Products 搜 Eos V-Neck Hoodie，打开 Type=Configurable 父产品，读 Material 主材质",
    ]
    # the function's `material` return merged back into each row → queryable into-table
    assert interp.env["mats"].rows == [
        {"Name": "Minerva LumaTech V-Tee-XS-Blue", "material": "Cotton"},
        {"Name": "Eos V-Neck Hoodie-S-Blue", "material": "Fleece"},
    ]


def test_compute_accepts_braced_scalar_refs():
    # Live 185 regression: the decomposer wrote the compute expr with the SAME `{name}` template
    # convention it uses in every milestone — `{sku}.rsplit(...)` instead of bare `sku.rsplit(...)`.
    # To safe_eval `{sku}` is a set literal → SafeEvalError → base silently empty → the search
    # milestone ran with an empty keyword. The interpreter must treat `{sku}` and `sku` alike.
    program = Program(
        goal="g",
        functions=[FunctionDef(name="resolve", params=["sku"], returns=["base"], body=[
            Compute(var="base", expr="{sku}.rsplit('-', 2)[0]"),
            Run(kind="read", name="搜 {base}"),
        ])],
        statements=[
            ForEach(var="row", into="out", returns=["SKU"], body=[
                Call(func="resolve", args={"sku": "{row[SKU]}"}, var="m"),
            ]),
            Finish(message="done"),
        ],
    )
    rows = [{"SKU": "WS08-XS-Blue"}, {"SKU": "WH11-S-Blue"}]
    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)  # the {base} must be the non-empty derived parent SKU
        return RunResult(completed=True)

    interp = Interpreter(program, collect_fn=lambda t, r, limit=None: rows)
    drive(interp, execute)
    assert seen == ["搜 WS08", "搜 WH11"]  # NOT "搜 " (empty) — braces stripped, compute succeeded


def test_function_callable_from_main_not_loop_bound():
    # Functions are decoupled from loops — callable directly from main.
    program = Program(
        goal="g",
        functions=[FunctionDef(name="greet", params=["who"], returns=["msg"], body=[
            Compute(var="msg", expr="'hi ' + who"),
        ])],
        statements=[
            Call(func="greet", args={"who": "world"}, var="g"),
            Finish(message="{g[msg]}"),
        ],
    )
    interp = Interpreter(program)
    reply = drive(interp, lambda run: RunResult(completed=True))
    assert reply == "hi world"


def test_unknown_function_fails_honestly():
    program = Program(statements=[Call(func="nope", args={}, var="x"), Finish(message="done")])
    interp = Interpreter(program)
    reply = drive(interp, lambda run: RunResult(completed=True))
    assert "未定义的函数" in reply or "未定义" in reply


def test_call_recursion_is_bounded():
    program = Program(
        goal="g",
        functions=[FunctionDef(name="loop", params=[], returns=[], body=[
            Call(func="loop", args={}, var="r"),
        ])],
        statements=[Call(func="loop", args={}, var="r0"), Finish(message="done")],
    )
    interp = Interpreter(program)
    reply = drive(interp, lambda run: RunResult(completed=True))
    assert "嵌套过深" in reply


# ── safe_eval ────────────────────────────────────────────────────────────────────
def test_safe_eval_string_derivations():
    assert safe_eval("re_sub('-[A-Za-z]+-[A-Za-z]+$', '', name)",
                     {"name": "WS08-XS-Blue"}) == "WS08"
    assert safe_eval("name.rsplit('-', 2)[0]", {"name": "WS08-XS-Blue"}) == "WS08"
    assert safe_eval("sku.split('-')[0]", {"sku": "WS08-XS-Blue"}) == "WS08"
    assert safe_eval("a + '/' + b", {"a": "x", "b": "y"}) == "x/y"


def test_safe_eval_rejects_dangerous():
    for bad in ("__import__('os')", "open('/etc/passwd')", "name.__class__", "().__class__.__bases__"):
        with pytest.raises(SafeEvalError):
            safe_eval(bad, {"name": "x"})


def test_safe_eval_unknown_name_raises():
    with pytest.raises(SafeEvalError):
        safe_eval("nope + 'x'", {})
