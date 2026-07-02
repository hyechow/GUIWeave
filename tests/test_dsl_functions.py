"""First-class DSL functions + pure compute: the DSL program is an ordinary code file —
control flow (functions / call / if / foreach), pure compute (deterministic derivation), and
linear GUI milestones (Run). The whole file (main + functions) is produced in ONE decompose;
each function is decomposed ONCE and called N times (no per-row re-decompose).

Legacy 185 regression shape: a function resolve_parent_material(name) derives the base via a
Compute (NOT a GUI milestone), runs one linear GUI milestone, returns material; a foreach calls it
per row. The current 185 plan adds self-first + Action_url below; this legacy fixture still pins the
generic function/compute/call mechanics.
"""

import pytest

from gui_agent.core.orchestrator import (
    Call,
    Compute,
    Cond,
    Finish,
    ForEach,
    FunctionDef,
    If,
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


def _resolve_product_material_fn() -> FunctionDef:
    # The 185 approach: a function with self-first resolution expressed as an explicit `if` on the
    # self read — read the attribute on the product itself; the parent is only a FALLBACK when self is
    # empty (one shape covers attrs on the variant — Size/Color → self, done — AND on the parent —
    # Material). The self read uses the row's Action_url so it does not depend on the Products list
    # still showing the qty=3 result set after a previous parent fallback search changed filters.
    return FunctionDef(
        name="resolve_product_material", params=["sku", "product_url"],
        returns=["material", "source_kind", "source_sku"], body=[
            Run(kind="navigation", var="self_d", returns=["material"],
                name="打开 {product_url}，进入 SKU={sku} 自己的产品详情页",
                success_condition="已进入 SKU={sku} 的产品详情页",
                read_spec="material：自身 Material 主材质，空则留空"),
            If(cond=Cond(var="self_d", field="material", cmp="exists"),
               then=[
                   Compute(var="source_kind", expr="'self'"),
                   Compute(var="source_sku", expr="sku"),
                   Run(kind="navigation", name="使用浏览器返回上一页，回到 Products 列表或搜索结果页",
                       success_condition="页面显示 Products 列表、Search by keyword 输入框和结果表格"),
               ],
               otherwise=[
                   Compute(var="base_sku", expr="sku.rsplit('-',2)[0]"),
                   Run(kind="navigation", name="使用浏览器返回上一页，回到 Products 列表或搜索结果页",
                       success_condition="页面显示 Products 列表、顶部 Search by keyword 输入框和结果表格"),
                   Run(kind="filter", name="搜父 SKU={base_sku}、Type=Configurable",
                       success_condition="出现 SKU={base_sku}、Type=Configurable 行"),
                   Run(kind="read", var="parent_d", returns=["material"],
                       name="开父 SKU={base_sku} 编辑页读 Material"),
                   Run(kind="navigation", name="使用浏览器返回上一页，回到 Products 搜索结果列表",
                       success_condition="页面显示 Products 列表、Search by keyword 输入框和结果表格"),
                   Compute(var="source_kind", expr="'parent'"), Compute(var="source_sku", expr="base_sku"),
               ]),
        ])


def _qty3_program() -> Program:
    return Program(goal="material of products with 3 units left",
        functions=[_resolve_product_material_fn()],
        statements=[
            ForEach(var="row", into="out", returns=["SKU", "Action_url"], body=[
                Call(
                    func="resolve_product_material",
                    args={
                        "sku": "{row[SKU]}",
                        "product_url": "{row[Action_url]}",
                    },
                    var="m",
                )]),
            Finish(message="done")])


def _drive_qty3(self_material_by_sku: dict[str, str]) -> tuple[list[dict], list[str]]:
    rows = [
        {"SKU": "WH11-S-Blue", "Action_url": "http://shop/admin/catalog/product/edit/id/11"},
        {"SKU": "WS08-XS-Blue", "Action_url": "http://shop/admin/catalog/product/edit/id/8"},
    ]
    interp = Interpreter(_qty3_program(), collect_fn=lambda t, r, limit=None: rows)
    opened: list[str] = []
    url_to_sku = {row["Action_url"]: row["SKU"] for row in rows}

    def execute(run: Run) -> RunResult:
        if "material" in run.returns:
            opened.append(run.name)
            if "开父" in run.name:
                return RunResult(completed=True, reads={"material": "ParentMat"})
            url = run.name.split("打开 ", 1)[1].split("，", 1)[0]
            sku = url_to_sku[url]
            return RunResult(completed=True, reads={"material": self_material_by_sku.get(sku, "")})
        opened.append(run.name)
        return RunResult(completed=True)

    drive(interp, execute)
    return interp.env["out"].rows, opened


def test_attribute_read_on_self_when_present_does_not_touch_parent():
    # Size/Color-style: the variant carries the value → read self, return source_kind=self, and the
    # parent fallback branch is NEVER entered (no "开父" read).
    rows, opened = _drive_qty3({"WH11-S-Blue": "Fleece", "WS08-XS-Blue": "Cotton"})
    assert rows == [
        {
            "SKU": "WH11-S-Blue",
            "Action_url": "http://shop/admin/catalog/product/edit/id/11",
            "material": "Fleece",
            "source_kind": "self",
            "source_sku": "WH11-S-Blue",
        },
        {
            "SKU": "WS08-XS-Blue",
            "Action_url": "http://shop/admin/catalog/product/edit/id/8",
            "material": "Cotton",
            "source_kind": "self",
            "source_sku": "WS08-XS-Blue",
        },
    ]
    assert all("搜父候选" not in name for name in opened)
    assert opened.count("使用浏览器返回上一页，回到 Products 列表或搜索结果页") == 2


def test_parent_fallback_only_when_self_empty():
    # Material-style: WH11 variant carries it (self), WS08 variant empty → resolve parent (source_sku
    # = derived parent base WS08). Parent is the fallback, taken per-row by evidence, not by default.
    rows, opened = _drive_qty3({"WH11-S-Blue": "Fleece", "WS08-XS-Blue": ""})
    assert rows == [
        {
            "SKU": "WH11-S-Blue",
            "Action_url": "http://shop/admin/catalog/product/edit/id/11",
            "material": "Fleece",
            "source_kind": "self",
            "source_sku": "WH11-S-Blue",
        },
        {
            "SKU": "WS08-XS-Blue",
            "Action_url": "http://shop/admin/catalog/product/edit/id/8",
            "material": "ParentMat",
            "source_kind": "parent",
            "source_sku": "WS08",
        },
    ]
    assert "使用浏览器返回上一页，回到 Products 列表或搜索结果页" in opened
    assert "搜父 SKU=WS08、Type=Configurable" in opened
    assert "使用浏览器返回上一页，回到 Products 搜索结果列表" in opened


def test_validator_flags_dead_conditional_in_function():
    # "编排逻辑写死" root cause: the self-first `if` reads self_d, but the self-read milestone binds a
    # DIFFERENT var → self_d is never produced → condition is always empty → every row falls to else →
    # degenerates to hardcoded-always-parent. validate_program now walks function bodies, so the
    # existing IF_COND_VAR_NOT_IN_SCOPE rule catches the var mismatch and triggers a repair retry.
    from gui_agent.core.orchestrator import Cond, validate_program

    def _fn(self_read_var: str) -> FunctionDef:
        # source_kind mirrors the production shape: computed in-branch and surfaced via the
        # function's `returns` (its consumption channel — else COMPUTE_VAR_UNUSED rightly fires).
        return FunctionDef(name="resolve", params=["sku"], returns=["material", "source_kind"], body=[
            Run(kind="filter", name="搜 {sku}", success_condition="出现 {sku}"),
            Run(kind="navigation", var=self_read_var, returns=["material"], read_spec="读 material",
                name="开 {sku} 编辑页", success_condition="进入"),
            If(cond=Cond(var="self_d", field="material", cmp="exists"),
               then=[Compute(var="source_kind", expr="'self'")],
               otherwise=[Run(kind="navigation", var="pd", returns=["material"], read_spec="读",
                              name="开父", success_condition="进父"),
                          Compute(var="source_kind", expr="'parent'")]),
        ])

    def _prog(fn: FunctionDef) -> Program:
        return Program(goal="取材质", functions=[fn], statements=[
            ForEach(var="row", into="out", returns=["SKU"],
                    body=[Call(func="resolve", args={"sku": "{row[SKU]}"}, var="m")]),
            Run(kind="data_query", var="q", returns=["material"], name="去重",
                sql="SELECT DISTINCT material FROM out"),
            Finish(message="{q[material]}")])

    bad = [i.code for i in validate_program(_prog(_fn("d")))]       # self-read var 'd' ≠ if's 'self_d'
    assert "IF_COND_VAR_NOT_IN_SCOPE" in bad
    assert validate_program(_prog(_fn("self_d"))) == []            # bound → no issue


def test_validator_accepts_function_returns_in_foreach_table_query():
    # Typed capability chain: foreach collects the row key, call resolves a detail field, and the
    # function's declared returns become columns in the foreach into table for data_query.
    from gui_agent.core.orchestrator import validate_program

    program = Program(
        goal="返回逐行详情字段",
        functions=[FunctionDef(name="resolve", params=["sku"], returns=["material"], body=[
            Run(kind="navigation", var="d", returns=["material"], read_spec="读 material",
                name="打开 SKU={sku} 的详情页并读取 material"),
        ])],
        statements=[
            ForEach(var="row", into="out", returns=["SKU"], body=[
                Call(func="resolve", args={"sku": "{row[SKU]}"}, var="m"),
            ]),
            Run(kind="data_query", var="q", returns=["material"], name="去重 material",
                sql="SELECT DISTINCT material FROM out"),
            Finish(message="{q[material]}"),
        ],
    )

    assert validate_program(program) == []


def test_validator_checks_call_args_against_current_row_contract():
    # Live 185 shape guard: a second foreach that only declares material cannot call a function with
    # {row[SKU]} / {row[Action_url]}; those fields are not in that loop row contract.
    from gui_agent.core.orchestrator import validate_program

    program = Program(
        goal="返回逐行详情字段",
        functions=[FunctionDef(name="resolve", params=["sku"], returns=["material"], body=[
            Run(kind="navigation", var="d", returns=["material"], read_spec="读 material",
                name="打开 SKU={sku} 的详情页并读取 material"),
        ])],
        statements=[
            ForEach(var="row", into="out", returns=["material"], body=[
                Call(func="resolve", args={"sku": "{row[SKU]}"}, var="m"),
            ]),
        ],
    )

    issues = validate_program(program)
    assert "TEMPLATE_FIELD_NOT_IN_RETURNS" in {i.code for i in issues}


def test_validator_allows_row_url_capability_when_passed_and_used():
    from gui_agent.core.orchestrator import validate_program

    program = Program(
        goal="逐行打开详情读字段",
        functions=[FunctionDef(name="resolve", params=["sku", "detail_url"], returns=["material"], body=[
            Run(kind="navigation", var="d", returns=["material"], read_spec="读 material",
                name="打开 {detail_url}，进入 SKU={sku} 的详情页并读取 material"),
        ])],
        statements=[
            ForEach(var="row", into="out", returns=["SKU", "Action_url"], body=[
                Call(
                    func="resolve",
                    args={"sku": "{row[SKU]}", "detail_url": "{row[Action_url]}"},
                    var="m",
                ),
            ]),
            Run(kind="data_query", var="q", returns=["material"], name="去重 material",
                sql="SELECT DISTINCT material FROM out"),
            Finish(message="{q[material]}"),
        ],
    )

    assert validate_program(program) == []


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


def test_safe_eval_numeric_derivation():
    # WebArena 778: percentage price change — both live decomposes naturally wrote
    # round(float(current_price) * 0.865, 2). float/round/abs and * - / must evaluate,
    # else the whole numeric-compute class silently degrades to "".
    assert safe_eval("round(float('150.00') * 0.865, 2)", {}) == 129.75
    assert safe_eval("round(float(p) * (1 - 0.135), 2)", {"p": "75.00"}) == 64.88
    assert safe_eval("abs(10 - 12.5)", {}) == 2.5
    assert safe_eval("round(-13.5 / 100, 3)", {}) == -0.135


def test_compute_reads_env_run_result_fields():
    # 778 runtime root cause: the compute expr references a prior read's field the natural way —
    # product_detail['current_price'] — but the eval scope only held scalars, so it raised
    # 未知变量 → silently "" → the fill milestone lost its concrete value and the planner
    # hallucinated one (typed 200.00 instead of current×0.865). Env reads must be in scope.
    program = Program(goal="降价13.5%", statements=[
        Run(kind="navigation", var="product_detail", returns=["current_price"],
            read_spec="读 Price", name="打开变体详情页", success_condition="进入编辑页"),
        Compute(var="new_price", expr="str(round(float(product_detail['current_price']) * 0.865, 2))"),
        Run(kind="action", name="将价格更新为 {new_price} 并保存", success_condition="保存成功"),
        Finish(message="已更新为 {new_price}"),
    ])
    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        if run.var == "product_detail":
            return RunResult(completed=True, reads={"current_price": "150.00"})
        return RunResult(completed=True)

    reply = drive(Interpreter(program), execute)
    # The fill action reached the executor with the EXACT computed value — not "新值", not empty.
    assert seen[-1] == "将价格更新为 129.75 并保存"
    assert "129.75" in reply


def test_fill_fails_fast_on_empty_compute_scalar():
    # A failed compute binds "" — the consuming action must FAIL FAST (honest reply), not run with
    # a gap-name the planner then fills with a hallucinated value (778: typed 200.00).
    program = Program(goal="降价", statements=[
        Compute(var="new_price", expr="round(float(nonexistent['x']), 2)"),  # 求值失败 → ""
        Run(kind="action", name="将价格更新为 {new_price} 并保存", success_condition="保存成功"),
        Finish(message="done"),
    ])
    executed: list[str] = []

    def execute(run: Run) -> RunResult:
        executed.append(run.name)
        return RunResult(completed=True)

    reply = drive(Interpreter(program), execute)
    assert executed == []  # the gap-named milestone never reached the executor
    assert "无法执行" in reply and "new_price" in reply


def test_safe_eval_coerces_numeric_strings_for_arithmetic():
    # Page reads are always strings ("75.00", "$1,299.00"). *, -, / can only be numeric intent, so
    # they coerce — the compute works whether or not the decomposer wrapped float() (WebArena 778
    # wrote `round(current_price * 0.865, 2)` on a str → "can't multiply sequence").
    assert safe_eval("round(current_price * 0.865, 2)", {"current_price": "75.00"}) == 64.88
    assert safe_eval("round(price * 0.865, 2)", {"price": "$1,299.00"}) == 1123.63
    assert safe_eval("qty - 1", {"qty": "10"}) == 9.0
    assert safe_eval("total / 2", {"total": "100"}) == 50.0
    # `+` and `%` keep string semantics — SKU derivation must NOT become numeric.
    assert safe_eval("a + b", {"a": "WS08", "b": "-XS"}) == "WS08-XS"


def test_compute_scope_exposes_read_fields_bare_and_dotted():
    # The decomposer references a prior read's field BOTH ways nondeterministically: bare
    # `current_price` and `variant_row['current_price']`. Both must resolve (WebArena 778: the bare
    # form silently produced "" and the fill milestone lost its value).
    for expr in ("round(current_price * 0.865, 2)",
                 "round(variant_row['current_price'] * 0.865, 2)"):
        program = Program(goal="降价", statements=[
            Run(kind="action", var="variant_row", returns=["current_price"],
                name="点 size28 变体行", success_condition="选中"),
            Compute(var="new_price", expr=expr),
            Run(kind="action", name="将 Price 更新为 {new_price} 并保存", success_condition="保存成功"),
            Finish(message="done"),
        ])
        seen: list[str] = []

        def execute(run: Run) -> RunResult:
            seen.append(run.name)
            if run.var == "variant_row":
                return RunResult(completed=True, reads={"current_price": "75.00"})
            return RunResult(completed=True)

        drive(Interpreter(program), execute)
        assert seen[-1] == "将 Price 更新为 64.88 并保存", f"expr {expr!r} → {seen[-1]!r}"


def test_compute_template_field_braces_in_foreach_body():
    # Offline 778 v3 wrote the body compute as `{p[price]} * 0.865` (the SAME template convention as
    # milestone names). To safe_eval that's a set literal → SafeEvalError → "". _compute normalizes
    # `{var[field]}` → `var['field']`; with the loop var bound per row it must fill concrete values.
    program = Program(goal="降价", statements=[
        ForEach(var="p", target="size28 变体行", returns=["sku", "price"], body=[
            Compute(var="new_price", expr="{p[price]} * 0.865"),
            Run(kind="action", name="打开 {p[sku]} 并更新价格为 {new_price} 保存",
                success_condition="已保存"),
        ]),
        Finish(message="done"),
    ])
    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        return RunResult(completed=True)

    rows = [{"sku": "WP09-28-Blue", "price": "75.00"}, {"sku": "WP09-28-Gray", "price": "$75.00"}]
    drive(Interpreter(program, collect_fn=lambda target, cols, limit=None: rows), execute)
    assert seen == [
        "打开 WP09-28-Blue 并更新价格为 64.875 保存",
        "打开 WP09-28-Gray 并更新价格为 64.875 保存",
    ]
