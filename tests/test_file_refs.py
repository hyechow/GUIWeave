"""resolve_file_refs: @<path> tokens in the goal → file contents injected at decompose time.

Config-heavy tasks point at a file (「按 @sim.json 的配置新建」) instead of dictating a dozen
field values in the prompt. Resolution is existence-based with trailing-trim, so prose glued
to the path and plain @-mentions never break anything.
"""

from __future__ import annotations

import gui_agent.core.supervisor.milestone.model_io as model_io
from gui_agent.core.supervisor.milestone.model_io import resolve_file_refs


def _mk(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_basic_ref_injects_content(tmp_path):
    _mk(tmp_path, "sim.json", '{"count": 2}')
    out = resolve_file_refs("按 @sim.json 的配置新建虚拟机器人", base=tmp_path)
    assert "## 引用文件内容" in out
    assert "### @sim.json" in out
    assert '{"count": 2}' in out


def test_glued_cjk_prose_is_trimmed(tmp_path):
    _mk(tmp_path, "sim.json", "DATA")
    # 中文紧贴路径(无空格):「@sim.json的配置」→ 逐字符回退到存在的文件
    out = resolve_file_refs("按@sim.json的配置新建", base=tmp_path)
    assert "### @sim.json" in out and "DATA" in out


def test_trailing_ascii_punct_stripped(tmp_path):
    _mk(tmp_path, "sim.json", "DATA")
    out = resolve_file_refs("read @sim.json, then proceed", base=tmp_path)
    assert "### @sim.json" in out


def test_quoted_and_bracketed_tokens(tmp_path):
    _mk(tmp_path, "交管配置_1楼.json", "CJK-NAME-DATA")
    out = resolve_file_refs("按「@交管配置_1楼.json」配置执行", base=tmp_path)
    assert "CJK-NAME-DATA" in out


def test_unresolvable_mention_is_ignored(tmp_path):
    assert resolve_file_refs("联系 @qq.com 或 @不存在的文件.json", base=tmp_path) == ""


def test_multiple_refs_and_dedupe(tmp_path):
    _mk(tmp_path, "a.json", "AAA")
    _mk(tmp_path, "sub/b.yaml", "BBB")
    out = resolve_file_refs("用 @a.json 和 @sub/b.yaml，再看一遍 @a.json", base=tmp_path)
    assert out.count("### @a.json") == 1     # deduped
    assert "AAA" in out and "BBB" in out


def test_subdir_and_absolute_path(tmp_path):
    p = _mk(tmp_path, "cfg/two_robot.json", "ABS")
    out = resolve_file_refs(f"按 @{p} 执行", base=tmp_path / "elsewhere")
    assert "ABS" in out


def test_truncation_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(model_io, "_FILE_REF_MAX_CHARS", 10)
    _mk(tmp_path, "big.txt", "x" * 100)
    out = resolve_file_refs("读 @big.txt", base=tmp_path)
    assert "已截断" in out and "x" * 100 not in out


def test_aggregate_total_cap_across_multiple_refs(tmp_path, monkeypatch):
    # file_reference_block is `required` (never dropped by the budgeter), so the TOTAL across
    # all @refs must be deterministically bounded — otherwise several big files defeat the hard
    # context cap. Per-file cap stays high; the aggregate cap truncates/omits the overflow.
    monkeypatch.setattr(model_io, "_FILE_REF_MAX_CHARS", 1000)
    monkeypatch.setattr(model_io, "_FILE_REF_TOTAL_MAX_CHARS", 1200)
    _mk(tmp_path, "a.txt", "a" * 1000)
    _mk(tmp_path, "b.txt", "b" * 1000)
    _mk(tmp_path, "c.txt", "c" * 1000)
    out = resolve_file_refs("读 @a.txt 和 @b.txt 和 @c.txt", base=tmp_path)
    # total injected text content is bounded by the aggregate cap
    assert out.count("a") + out.count("b") + out.count("c") <= 1200 + 200  # +slack for marker text
    assert "总量超上限" in out                       # honest overflow marker present
    assert "a.txt" in out                            # the first file is fully injected
    assert "c" * 1000 not in out                     # the third is omitted (budget exhausted)


def test_no_refs_returns_empty():
    assert resolve_file_refs("没有任何文件引用的普通任务") == ""
