"""Turn-budget estimation for DSL orchestrator programs.

The agent-loop max_turns guard counts GUI decision/action turns. This module gives
orchestrator mode a conservative max_turns BEFORE execution starts so a genuinely
heavy program (multi-field form, file upload, create-then-verify chain) isn't starved
at the default floor, while a normal program just stays at the floor.

CALIBRATION (against 10 real RoboTeam browser runs, 2026-06-13..16; logs only, no test
fixtures): actual turns are driven almost entirely by ACTION milestones — reads consume
no turn (single-frame, hand-off-merged in run_agent_loop) and navigations are cheap. The
OLD model summed a cost per STATEMENT and stacked keyword bonuses, so a verbose decompose
(LLM split into 8 statements) estimated ~3-4x its real turn count (e.g. a 14-turn run →
45/58). Same-structure runs also vary wildly (a 2-action connectivity+create task ran 8
turns once and starved at 20 another time) — pure execution noise the program text can't
predict. So we do NOT try to predict actual turns; we set a sensible CEILING:

  * read / filter / finish → 0 (no marginal turn);
  * navigation → cheap, capped (multi-hop nav usually collapses under nav knowledge);
  * action milestones → the real driver, summed along the LONGEST branch with a small
    per-action heavy bonus (form / create / upload / multi-waypoint sequence).

Constants are tuned so normal tasks fall back to the floor (20) and only create-order
chains / uploads / many-action programs rise to ~22-30, capped at 32 (observed max actual
was 28). Over-budgeting is the benign direction (headroom on an easy run is unused);
under-budgeting starves a real task. We bias to headroom but keep the cap tight so a
flailing run still fails in bounded time.
"""

from __future__ import annotations

from .program import Finish, ForEach, If, Program, Run, Stmt

BASE_TURNS = 5            # task-level overhead: initial observe + settle + a decision turn or two
DEFAULT_FLOOR_TURNS = 20  # the caller's requested minimum (bin/runner passes --max-turns 20)
DEFAULT_CAP_TURNS = 32    # runaway ceiling; just above the observed max real run (28 turns)
# A foreach iterates a runtime-unknown number of rows, each running its body (e.g. open detail +
# read). Budget for an assumed iteration count, and lift the cap when a program has a foreach — its
# whole point is to process a collection, so the 32-turn ceiling tuned for flat programs would starve
# it (a 10-row drill = ~40+ turns).
FOREACH_ASSUMED_ITERS = 12
FOREACH_CAP_TURNS = 80

NAV_TURNS = 1             # a navigation milestone is ~1-3 taps but knowledge-guided hops collapse
NAV_CONTRIB_CAP = 4       # total navigation contribution, however many nav steps
ACTION_BASE = 4           # a light action milestone (one button / a tiny form)
ACTION_HEAVY_CAP = 6      # max heavy bonus on a single action → per-action ceiling = 10

# Heavy-action signals (substring match on name+success_condition+read_spec, lowercased).
# Each present group adds its weight ONCE; the per-action total is capped at ACTION_HEAVY_CAP.
_FORM_TERMS = (
    "表单", "弹窗", "填写", "填入", "输入", "配置", "设置", "选择", "指定", "搜索",
    "form", "dialog", "modal", "input", "configure", "setting", "select", "assign", "search",
)
_CREATE_OR_COMMIT_TERMS = (
    "创建", "新建", "建单", "下单", "提交", "保存", "发送", "删除", "启用", "禁用",
    "create", "new", "submit", "save", "send", "delete", "enable", "disable",
)
_UPLOAD_TERMS = (
    "上传", "导入", "upload", "import",
)
_SEQUENCE_TERMS = (
    "动作序列", "序列", "多步", "依次", "->", "→",
    "sequence", "waypoint", "waypoints",
)

_FORM_BONUS = 2
_CREATE_BONUS = 2
_UPLOAD_BONUS = 4
_SEQUENCE_BONUS = 1


def estimate_program_turns(
    program: Program,
    *,
    floor: int = DEFAULT_FLOOR_TURNS,
    cap: int | None = DEFAULT_CAP_TURNS,
) -> int:
    """Estimate a global max_turns ceiling from a Program's action milestones.

    Branches are estimated by the longest branch (only one path runs). `floor` preserves
    the caller's requested minimum; `cap` limits automatic expansion but never lowers
    `floor` (so an explicit high --max-turns is always honoured).
    """
    estimate = BASE_TURNS + _estimate_stmts(program.statements)
    estimate = max(floor, estimate)
    if cap is not None:
        # A foreach program iterates a collection — lift the flat-program cap so its body×rows isn't
        # starved (still bounded, just higher).
        effective_cap = FOREACH_CAP_TURNS if _has_foreach(program.statements) else cap
        estimate = min(estimate, max(effective_cap, floor))
    return estimate


def _has_foreach(stmts: list[Stmt]) -> bool:
    for s in stmts:
        if isinstance(s, ForEach):
            return True
        if isinstance(s, If) and (_has_foreach(s.then) or _has_foreach(s.otherwise)):
            return True
    return False


def _estimate_stmts(stmts: list[Stmt]) -> int:
    """Turn cost of a statement list: action milestones summed, navs capped, reads free."""
    action_cost = 0
    nav_cost = 0
    branch_cost = 0
    for stmt in stmts:
        if isinstance(stmt, Run):
            if stmt.kind == "action":
                action_cost += _action_cost(_run_text(stmt))
            elif stmt.kind in {"navigation", "filter"} and not stmt.precondition:
                nav_cost += NAV_TURNS  # precondition nav is usually already satisfied → free
        elif isinstance(stmt, If):
            # 2-turn branch overhead + the heavier of the two arms (only one executes)
            branch_cost += 2 + max(_estimate_stmts(stmt.then), _estimate_stmts(stmt.otherwise))
        elif isinstance(stmt, ForEach):
            # body cost × an assumed iteration count (runtime-unknown rows); reads in the body are
            # free per the model, so this is mostly the per-row action (open detail) × rows.
            branch_cost += FOREACH_ASSUMED_ITERS * _estimate_stmts(stmt.body)
        elif isinstance(stmt, Finish):
            pass
    return action_cost + min(nav_cost, NAV_CONTRIB_CAP) + branch_cost


def _action_cost(text: str) -> int:
    bonus = 0
    if _has_any(text, _FORM_TERMS):
        bonus += _FORM_BONUS
    if _has_any(text, _CREATE_OR_COMMIT_TERMS):
        bonus += _CREATE_BONUS
    if _has_any(text, _UPLOAD_TERMS):
        bonus += _UPLOAD_BONUS
    if _has_any(text, _SEQUENCE_TERMS):
        bonus += _SEQUENCE_BONUS
    return ACTION_BASE + min(bonus, ACTION_HEAVY_CAP)


def _run_text(run: Run) -> str:
    return " ".join(
        part for part in (run.name, run.success_condition, run.read_spec) if part
    ).lower()


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)
