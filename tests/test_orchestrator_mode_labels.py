from gui_agent.adapters.browser.webarena import _print_program
from gui_agent.core.orchestrator import (
    Compute,
    Finish,
    ForEach,
    Program,
    Query,
    Read,
    Run,
    execution_mode_for_kind,
)


def test_run_kind_execution_mode_contract():
    assert execution_mode_for_kind("navigation") == "interactive"
    assert execution_mode_for_kind("filter") == "interactive"
    assert execution_mode_for_kind("action") == "interactive"
    assert execution_mode_for_kind("read") == "non_interactive"
    assert execution_mode_for_kind("data_query") == "non_interactive"
    assert execution_mode_for_kind("compute") == "non_interactive"


def test_webarena_program_print_labels_execution_modes(capsys):
    program = Program(
        statements=[
            Run(kind="navigation", name="进入 Products List 页面"),
            Run(kind="navigation", name="打开 {row[action_url]} 详情页"),
            Run(kind="action", name="将价格更新为 {new_price} 并保存"),
            ForEach(
                var="row",
                into="variant_rows",
                row_fields=["sku", "name", "action_url"],
                member_desc="size 28 的 Sahara leggings 变体",
                body=[Run(kind="action", name="处理 {row[sku]}")],
            ),
            Read(var="r", name="读取保存提示", returns=["status"], read_spec="读取提示文字"),
            Compute(var="new_price", expr="round(100 * 0.865, 2)"),
            Query(var="q", name="汇总结果", returns=["result"], sql="SELECT 1 AS result"),
            Finish(message="{q[result]}"),
        ],
    )

    _print_program(program)
    out = capsys.readouterr().out

    assert "[interactive:navigation] 进入 Products List 页面" in out
    assert "[browser:navigation_url] 打开 {row[action_url]} 详情页" in out
    assert "[interactive:action] 将价格更新为 {new_price} 并保存" in out
    assert "[control:foreach] row -> variant_rows row_fields=['sku', 'name', 'action_url'] member_desc='size 28 的 Sahara leggings 变体'" in out
    assert "[non-interactive:read] 读取保存提示" in out
    assert "[non-interactive:compute] new_price = round(100 * 0.865, 2)" in out
    assert "[non-interactive:data_query] 汇总结果" in out
    assert "SQL: SELECT 1 AS result" in out
    assert "Query: op='run'" not in out
