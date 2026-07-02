"""Python-surface compiler tests: restricted-Python plans compile to the SAME Program IR the JSON
path produces, so every downstream gate (validator/preflight/interpreter) applies unchanged. The
778 shape is the canonical case; bad constructs must fail with author-facing feedback."""
from __future__ import annotations

import pytest

from gui_agent.core.orchestrator import (
    Compute, Finish, ForEach, If, Interpreter, Run, RunResult, drive, validate_program,
)
from gui_agent.core.orchestrator.pysurface import CompileIssue, compile_python_plan

PLAN_778 = '''
navigate("进入 Products List 页面", sc="页面显示产品列表和筛选控件")
filter("清除残留筛选，在搜索框输入 'Sahara' 并提交搜索", sc="列表显示 Sahara 相关记录（非0条）")
for row in collect("搜索结果中 SKU 含 '-28-' 的变体行", returns=["sku", "price", "action_url"]):
    d = navigate(f"打开变体 {row['action_url']} 的编辑页", sc="已进入该变体编辑页",
                 returns=["current_price"], read_spec="current_price: Price 输入框中的数值")
    new_price = round(float(d["current_price"]) * 0.865, 2)
    action(f"将价格更新为 {new_price} 并保存", sc="页面显示保存成功提示")
finish("已完成所有 size 28 Sahara leggings 变体的调价")
'''


def test_compiles_778_shape_to_ir():
    prog = compile_python_plan(PLAN_778, goal="Reduce the price of size 28 Sahara leggings by 13.5%")
    kinds = [type(s).__name__ for s in prog.statements]
    assert kinds == ["Run", "Run", "ForEach", "Finish"]
    fe = prog.statements[2]
    assert fe.var == "row" and fe.returns == ["sku", "price", "action_url"]
    assert [type(b).__name__ for b in fe.body] == ["Run", "Compute", "Run"]
    assert fe.body[0].name == "打开变体 {row[action_url]} 的编辑页"      # f-string → IR template
    assert fe.body[1].expr == "round(float(d['current_price']) * 0.865, 2)"
    assert "{new_price}" in fe.body[2].name
    assert not validate_program(prog)


def test_compiled_plan_executes_end_to_end():
    prog = compile_python_plan(PLAN_778, goal="降价")
    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        if run.returns == ["current_price"]:
            return RunResult(completed=True, reads={"current_price": "$75.00"})
        return RunResult(completed=True)

    rows = [{"sku": "WP02-28-Blue", "price": "75.00", "action_url": "u1"}]
    drive(Interpreter(prog, collect_fn=lambda t, c, limit=None: rows), execute)
    # lenient float(): "$75.00" → 75.0 → 64.88 filled concretely
    assert any("64.88" in s for s in seen), seen


def test_if_branch_and_subgoal_forms():
    prog = compile_python_plan('''
v = read("读结果数", sc="可见", returns=["count"], read_spec="count: N records found 的 N")
if v["count"] == "0":
    finish("未找到")
else:
    for row in collect("匹配行", returns=["sku"]):
        subgoal(f"对 {row['sku']} 完成读取当前价格、更新价格并保存的子任务")
    finish("完成")
''')
    branch = prog.statements[1]
    assert isinstance(branch, If) and branch.cond.cmp == "==" and branch.cond.value == "0"
    fe = branch.otherwise[0]
    assert isinstance(fe, ForEach) and fe.body_goal and not fe.body
    assert "{row[sku]}" in fe.body_goal


@pytest.mark.parametrize("bad,hint", [
    ("import os", "import"),
    ("while True:\n    pass", "不支持的语句"),
    ("x = [r for r in rows]", "运行时不支持"),
    ("def f():\n    pass", "def"),
    ("if [r for r in a]:\n    finish('x')", "运行时不支持"),
    ("navigate(some_var)", "字符串字面量"),
])
def test_bad_constructs_rejected_with_feedback(bad, hint):
    with pytest.raises(CompileIssue, match=""):
        try:
            compile_python_plan(bad)
        except CompileIssue as e:
            assert hint in str(e)
            raise


def test_safe_eval_membership_and_ternary():
    from gui_agent.core.orchestrator.safe_eval import safe_eval
    scope = {"sku": "WP02-28-Blue", "price": "75.00"}
    assert safe_eval("'-28-' in sku", scope) is True
    assert safe_eval("'XL' in sku or '-28-' in sku", scope) is True
    assert safe_eval("'yes' if '-28-' in sku else 'no'", scope) == "yes"
    assert safe_eval("float(price) > 70", scope) is True
    assert safe_eval("float('$1,299.00')", {}) == 1299.0


def test_v2_idioms_assigned_collect_pass_guard_numeric_cond():
    # The five natural idioms the first python-arm run rejected (93 compile errors → empty programs):
    # assigned collect, pass, `if C: continue` guard, free-form numeric condition, collect read_spec.
    prog = compile_python_plan('''
navigate("进入评论列表", sc="网格已加载")
rows = collect("所有 pending 评论行", returns=["id", "rating"], read_spec="rating: 星级数字")
for row in rows:
    if int(row["rating"]) >= 4:
        continue
    action(f"删除评论 {row['id']}", sc="该行已删除")
finish("已删除所有低于 4 星的 pending 评论")
''')
    kinds = [type(s).__name__ for s in prog.statements]
    assert kinds == ["Run", "ForEach", "Finish"]
    fe = prog.statements[1]
    assert "列读取说明" in fe.target                       # read_spec folded into target
    body_kinds = [type(b).__name__ for b in fe.body]
    assert body_kinds == ["Compute", "If"]                  # synthetic cond scalar + guard If
    guard = fe.body[1]
    assert guard.then == [] and len(guard.otherwise) == 1   # continue → work in otherwise
    assert guard.cond.field == guard.cond.var               # self-field scalar cond
    assert not validate_program(prog)


def test_v2_guard_executes_correctly():
    prog = compile_python_plan('''
for row in collect("变体行", returns=["sku"]):
    if "-28-" not in row["sku"]:
        continue
    action(f"更新 {row['sku']}", sc="已保存")
finish("done")
''')
    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        return RunResult(completed=True)

    rows = [{"sku": "WP02-28-Blue"}, {"sku": "WP02-29-Red"}, {"sku": "WP02-28-Gray"}]
    drive(Interpreter(prog, collect_fn=lambda t, c, limit=None: rows), execute)
    assert seen == ["更新 WP02-28-Blue", "更新 WP02-28-Gray"]   # -29- row skipped by the guard
