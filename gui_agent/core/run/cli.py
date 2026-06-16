"""Command-line entrypoint for the agent loop runner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.llm.output import generate_reply
from gui_agent.core.run.io import EscStopSignal, create_run_dir, tee_stdio
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge
from gui_agent.core.run.state import write_final_run_state


def main(
    *,
    run_loop=None,
    policy_builder=None,
    supervisor_builder=None,
) -> None:
    if run_loop is None or policy_builder is None or supervisor_builder is None:
        from gui_agent.core.run.loop import build_policy, build_supervisor, run_agent_loop

        run_loop = run_agent_loop
        policy_builder = build_policy
        supervisor_builder = build_supervisor

    # The platform bundle supplies the default/choice names so argparse stays in
    # sync with the registry without importing any adapter at module top.
    bundle = build_platform()
    parser = argparse.ArgumentParser(description="测试手机策略运行模式")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="打开微信",
        help="目标指令，如「打开微信」「发一条朋友圈」",
    )
    parser.add_argument(
        "--policy",
        default=bundle.default_action_policy,
        choices=list(bundle.action_policy_choices),
        help="动作策略模块",
    )
    parser.add_argument(
        "--supervisor",
        # Preserve iphone's historical default ("simple") when the platform offers
        # it; otherwise fall back to the platform's own default (e.g. browser only
        # offers "milestone"). Keeps iphone identical, fixes browser out-of-the-box.
        default="simple" if "simple" in bundle.supervisor_choices else bundle.default_supervisor,
        choices=list(bundle.supervisor_choices),
        help="监督者策略模块",
    )
    parser.add_argument(
        "--context",
        type=Path,
        help="agent-loop 可选的 context 加载路径",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="agent-loop 最大自动执行轮数，防止无限循环",
    )
    parser.add_argument(
        "--no-dynamic-max-turns",
        action="store_true",
        help="DSL 编排器模式下不按 Program 复杂度自动上调 max_turns",
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="agent-loop 动作执行后自动进入下一轮；默认手动确认",
    )
    parser.add_argument(
        "--stop-on-esc",
        action="store_true",
        help="auto-continue 模式下监听 ESC，并在当前 turn 收尾后安全停止",
    )
    parser.add_argument(
        "--hud",
        action="store_true",
        help="在 iPhone 镜像窗口下方显示实时动作状态面板",
    )
    parser.add_argument(
        "--no-router",
        action="store_true",
        help="跳过 router 意图改写，直接把输入当作目标（默认经 router，与 chat 模式一致）",
    )
    parser.add_argument(
        "--orchestrator",
        action="store_true",
        help="DSL 编排器模式：先把目标 decompose 成 run/if/finish 程序，由解释器排序驱动各 milestone"
             "（默认走 DAG 路径）",
    )
    args = parser.parse_args()

    action_policy = policy_builder(args.policy)
    supervisor = supervisor_builder(args.supervisor)

    # Route the raw input through the LLM router (same path as chat mode) so the
    # goal is intent-classified and normalized consistently. Skipped when resuming
    # a saved context (--context, goal comes from the file) or with --no-router.
    raw_input = args.prompt
    router_result = None
    goal = args.prompt
    # Router (route_message) is platform-aware: each platform injects its own prompt
    # (iphone: 操控手机/APP; browser: 网页任务). Passing bundle.platform makes browser
    # tasks ("搜索nvidia股价") classify correctly instead of being rejected.
    if not args.context and not args.no_router:
        try:
            from gui_agent.core.chat.session import route_message
            router_result = route_message(raw_input, session=[], prefs_context="", platform=bundle.platform)
        except Exception as exc:
            print(f"Router  : 调用失败，回退原始输入（{exc}）")
        if router_result is not None:
            if not router_result.goal:
                if router_result.needs_clarification:
                    print(f"Router  : 需要补充信息 — {router_result.clarification}")
                else:
                    print("Router  : 非任务（闲聊/问答），已跳过")
                return
            goal = router_result.goal
            print(f"Router  : {raw_input!r} → {goal!r}")

    input_context_path = args.context
    log_dir = create_run_dir("agent-loop", bundle.platform)
    context_path = log_dir / "context.json"
    hud = bundle.make_status_reporter(args.hud)
    with tee_stdio(log_dir):
        print(f"Platform: {bundle.platform}")
        print(f"Log Dir : {log_dir}")
        print(f"Context : {input_context_path if input_context_path else None}")

        # Auto-discover app knowledge from the resolved goal. Done INSIDE the tee so the
        # match / load lines land in stdout.log (they used to print before the tee and vanish);
        # the summary is also persisted to context.json (context.knowledge) for offline analysis.
        knowledge_summary: dict | None = None
        knowledge = auto_discover_knowledge(goal, bundle.platform)
        if knowledge and hasattr(supervisor, "set_app_knowledge"):
            supervisor.set_app_knowledge(
                knowledge.navigation,
                app_name=knowledge.app_name,
                elements=knowledge.elements,
                sections=knowledge.sections,
                check=knowledge.check,
            )
            knowledge_summary = knowledge.summary()
            print(
                f"Knowledge: auto-loaded app={knowledge_summary['app_name']} "
                f"(nav={knowledge_summary['nav_chars']} chars, "
                f"elements={knowledge_summary['elements_chars']} chars, "
                f"sections={knowledge_summary['section_count']})"
            )

        # DSL orchestrator mode (opt-in): decompose the goal into a run/if/finish program; the
        # interpreter sequences milestones instead of the supervisor's DAG walker. program=None
        # (default) → the DAG path is unchanged.
        program = None
        run_max_turns = args.max_turns
        if args.orchestrator:
            from gui_agent.core.orchestrator import (
                decompose, estimate_program_turns,
                normalize_confirm_read_gates, normalize_precondition_gates,
            )
            from gui_agent.core.supervisor.milestone.helpers import resolve_file_refs
            # Resolve @<path> refs once (config field values the goal only points at) and feed
            # them to the decomposer — mirrors the DAG path, which the orchestrator's decompose
            # otherwise skipped (the LLM only saw the literal @token, never the field values).
            file_section = resolve_file_refs(goal)
            # L2 structural backstops (deterministic, keyed on structural signals, not gate wording):
            #  · confirm-read action gates → lenient dispatch gate (checker doesn't re-judge the
            #    result the read owns) — signal = action→read adjacency;
            #  · precondition gates (确保已登录/已进入某模式) → generic ensure-state gate so an
            #    already-satisfied precondition is done on frame 1 (no form/data stuck; app-specific
            #    markers live in the checker's _check.md) — signal = the run.precondition flag. See engine.
            program = normalize_precondition_gates(normalize_confirm_read_gates(
                decompose(goal, knowledge=knowledge.navigation if knowledge else "",
                          file_section=file_section)
            ))
            # The config must ALSO reach the execution-time planner deterministically — the
            # supervisor's constraints flow to every milestone's planner, and reseed never clears
            # them (LLM distillation of config into constraints proved unstable; see DAG path).
            if file_section and hasattr(supervisor, "_global_constraints"):
                _CAP = 3000
                supervisor._global_constraints.append(
                    file_section if len(file_section) <= _CAP
                    else file_section[:_CAP] + "\n…（配置过长已截断，其余以分解结果为准）"
                )
            print(f"Orchestrator: 分解为 {len(program.statements)} 条语句")
            if not args.no_dynamic_max_turns:
                run_max_turns = estimate_program_turns(program, floor=args.max_turns)
                if run_max_turns != args.max_turns:
                    print(
                        f"Orchestrator: max_turns {args.max_turns} -> {run_max_turns} "
                        "based on program complexity"
                    )

        try:
            stop_on_esc = args.stop_on_esc and args.auto_continue
            if args.stop_on_esc and not args.auto_continue:
                print("Interrupt: --stop-on-esc 仅在 --auto-continue 模式下启用")
            with EscStopSignal(enabled=stop_on_esc) as esc_stop:
                if args.stop_on_esc:
                    if esc_stop.enabled:
                        print("Interrupt: 按 ESC 将在当前 turn 收尾后停止")
                    elif stop_on_esc:
                        print("Interrupt: stdin 不是 TTY，ESC 停止未启用")
                result: dict | None = run_loop(
                    goal,
                    action_policy,
                    supervisor,
                    input_context_path,
                    log_dir,
                    context_path,
                    max_turns=run_max_turns,
                    auto_continue=args.auto_continue,
                    hud=hud,
                    raw_input=raw_input,
                    router=router_result.model_dump() if router_result else None,
                    knowledge=knowledge_summary,
                    program=program,
                    stop_requested=esc_stop.requested if esc_stop.enabled else None,
                )
            if result:
                if program is not None:
                    # Orchestrator mode: the answer is the interpreter's reply (finish /
                    # auto-summary from the program's persisted reads), not a re-derivation
                    # from content_notes — that's the whole point of the structured program.
                    output = result.get("result_summary") or "（编排器未产生答复）"
                else:
                    # Reply to the user's ORIGINAL input (parity with chat, which
                    # passes user_msg) rather than the router-rewritten goal.
                    output = generate_reply(
                        raw_input,
                        result,
                        content_notes=result.get("content_notes"),
                        collection_context=result.get("collection_context"),
                    )
                print("\n" + "=" * 50)
                print("最终输出")
                print("=" * 50)
                print(output.rstrip())
                print("=" * 50)
                # Persist the final reply and structured run state so the HTML report can render it.
                try:
                    write_final_run_state(context_path, result, output)
                except Exception as exc:
                    print(f"（输出未写入 context: {exc}）")

            # Auto-generate HTML report
            if (log_dir / "context.json").exists():
                try:
                    from scripts.report_builder import RunnerReportBuilder, save_report
                    report_data = RunnerReportBuilder().build(log_dir)
                    report_path = save_report(report_data, log_dir / "report.html")
                    print(f"\nReport  : {report_path}")
                except Exception as exc:
                    print(f"\nReport  : 生成失败 ({exc})")
        finally:
            if hud:
                hud.close()
