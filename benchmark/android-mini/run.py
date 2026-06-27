"""Android mini benchmark runner.

This module runs real Android runner-mode tasks, so it intentionally lives under
benchmark/ rather than evals/. The current benchmark covers the first layer of
CheckGithubInfoTask: locate/open the AndroidWorld GitHub repository and read
repository info from natural user intent. Verification reads the live Android UI
text with UIAutomator and, for answer tasks, checks the runner's final output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from html import unescape
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_FILE = Path(__file__).parent / "cases.json"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _run(
    cmd: list[str],
    *,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _find_adb(env: dict[str, str]) -> str:
    candidates = [
        env.get("ANDROID_ADB_BIN", ""),
        env.get("ADB", ""),
        shutil.which("adb") or "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if _run([candidate, "version"], timeout=10, env=env).returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    raise RuntimeError("adb 不可用：请安装 adb，或设置 ANDROID_ADB_BIN/ADB")


def _adb(
    env: dict[str, str],
    adb: str,
    args: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    serial = env.get("ANDROID_SERIAL", "").strip()
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    return _run(cmd, timeout=timeout, env=env)


def _ensure_device(env: dict[str, str], adb: str) -> None:
    serial = env.get("ANDROID_SERIAL", "").strip()
    if ":" in serial:
        _run([adb, "connect", serial], timeout=15, env=env)
    result = _adb(env, adb, ["get-state"], timeout=15)
    if result.returncode != 0 or result.stdout.strip() != "device":
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"adb 设备不可用：{detail or 'unknown error'}")


def _initialize_android_state(env: dict[str, str], adb: str, case: dict) -> None:
    script = str(case.get("init_script") or "").strip()
    if not script:
        return
    script_path = (PROJECT_ROOT / script).resolve()
    try:
        script_path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"init_script must stay inside repository: {script}") from exc
    if not script_path.exists():
        raise FileNotFoundError(f"init_script not found: {script}")

    init_env = {
        **env,
        "ADB": adb,
        "ANDROID_ADB_BIN": adb,
    }
    if case.get("init_url"):
        init_env["ANDROID_MINI_INIT_URL"] = str(case["init_url"])
    if case.get("init_settle_s") is not None:
        init_env["ANDROID_MINI_INIT_SETTLE_S"] = str(case["init_settle_s"])
    if case.get("init_keep_foreground") is not None:
        init_env["ANDROID_MINI_INIT_KEEP_FOREGROUND"] = "1" if case.get("init_keep_foreground") else "0"
    for key, value in (case.get("init_env") or {}).items():
        init_env[str(key)] = str(value)

    result = _run(
        ["bash", str(script_path)],
        timeout=int(case.get("init_timeout_s", 90)),
        env=init_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"init_script failed ({script}): {detail}")


def _run_verify_script(env: dict[str, str], adb: str, case: dict) -> str | None:
    script = str(case.get("verify_script") or "").strip()
    if not script:
        return None
    script_path = (PROJECT_ROOT / script).resolve()
    try:
        script_path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"verify_script must stay inside repository: {script}") from exc
    if not script_path.exists():
        raise FileNotFoundError(f"verify_script not found: {script}")

    verify_env = {
        **env,
        "ADB": adb,
        "ANDROID_ADB_BIN": adb,
    }
    for key, value in (case.get("verify_env") or {}).items():
        verify_env[str(key)] = str(value)

    result = _run(
        ["bash", str(script_path)],
        timeout=int(case.get("verify_timeout_s", 60)),
        env=verify_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"verify_script failed ({script}): {detail}"
    return None


def _dump_ui_text(env: dict[str, str], adb: str) -> str:
    dump = _adb(env, adb, ["shell", "uiautomator", "dump", "/sdcard/window.xml"], timeout=20)
    if dump.returncode != 0:
        raise RuntimeError(f"uiautomator dump failed: {(dump.stderr or dump.stdout).strip()}")
    xml = _adb(env, adb, ["shell", "cat", "/sdcard/window.xml"], timeout=20)
    if xml.returncode != 0:
        raise RuntimeError(f"read window.xml failed: {(xml.stderr or xml.stdout).strip()}")
    values: list[str] = []
    for attr in ("text", "content-desc"):
        values.extend(unescape(v) for v in re.findall(rf'{attr}="([^"]*)"', xml.stdout))
    return "\n".join(v for v in values if v.strip())


def _check_text(ui_text: str, case: dict) -> list[str]:
    haystack = _norm(ui_text)
    issues: list[str] = []
    missing = [
        needle
        for needle in case.get("expected_text_contains_all", [])
        if _norm(needle) not in haystack
    ]
    if missing:
        issues.append(f"UI text missing required markers: {missing}")

    any_markers = case.get("expected_text_contains_any", [])
    if any_markers and not any(_norm(marker) in haystack for marker in any_markers):
        issues.append(f"UI text missing all page markers: {any_markers}")

    forbidden = [
        marker
        for marker in case.get("forbidden_text_contains", [])
        if _norm(marker) in haystack
    ]
    if forbidden:
        issues.append(f"UI text contains forbidden markers: {forbidden}")
    return issues


def _extract_final_output(stdout: str) -> str:
    matches = list(
        re.finditer(
            r"=+\n最终输出\n=+\n(?P<output>.*?)(?:\n=+\n|\Z)",
            stdout,
            flags=re.S,
        )
    )
    if not matches:
        return stdout
    return matches[-1].group("output").strip()


def _check_output(output: str, case: dict) -> list[str]:
    if not (
        case.get("expected_output_contains_all")
        or case.get("expected_output_contains_any")
        or case.get("expected_output_regex_all")
        or case.get("expected_output_regex_any")
        or case.get("forbidden_output_contains")
    ):
        return []

    haystack = _norm(output)
    issues: list[str] = []
    missing = [
        needle
        for needle in case.get("expected_output_contains_all", [])
        if _norm(needle) not in haystack
    ]
    if missing:
        issues.append(f"final output missing required markers: {missing}")

    any_markers = case.get("expected_output_contains_any", [])
    if any_markers and not any(_norm(marker) in haystack for marker in any_markers):
        issues.append(f"final output missing all output markers: {any_markers}")

    missing_regex = [
        pattern
        for pattern in case.get("expected_output_regex_all", [])
        if re.search(pattern, output) is None
    ]
    if missing_regex:
        issues.append(f"final output missing required regex patterns: {missing_regex}")

    any_regex = case.get("expected_output_regex_any", [])
    if any_regex and not any(re.search(pattern, output) for pattern in any_regex):
        issues.append(f"final output missing all regex patterns: {any_regex}")

    forbidden = [
        marker
        for marker in case.get("forbidden_output_contains", [])
        if _norm(marker) in haystack
    ]
    if forbidden:
        issues.append(f"final output contains forbidden markers: {forbidden}")
    return issues


def _dump_and_check_ui_text(env: dict[str, str], adb: str, case: dict) -> tuple[str, list[str]]:
    attempts = int(case.get("ui_dump_attempts", 4))
    interval_s = float(case.get("ui_dump_interval_s", 2.0))
    last_text = ""
    last_issues: list[str] = []
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(interval_s)
        ui_text = _dump_ui_text(env, adb)
        issues = _check_text(ui_text, case)
        if not issues:
            return ui_text, []
        last_text = ui_text
        last_issues = issues
    return last_text, last_issues


def _run_case(
    case: dict,
    *,
    env: dict[str, str],
    adb: str,
    show_runner_output: bool,
) -> tuple[bool, str]:
    cmd = [
        str(PROJECT_ROOT / "bin" / "runner"),
        "android",
        case["goal"],
        "--max-turns",
        str(case.get("max_turns", 8)),
    ]
    cmd.extend(str(arg) for arg in case.get("runner_args", []))
    bench_env = {
        **env,
        "HEADLESS": env.get("HEADLESS", "1"),
        "AGENT_HEADLESS": env.get("AGENT_HEADLESS", "1"),
    }
    try:
        _initialize_android_state(bench_env, adb, case)
    except Exception as exc:  # noqa: BLE001
        return False, f"init failed: {exc}"
    result = _run(cmd, timeout=int(case.get("timeout_s", 360)), env=bench_env)
    if show_runner_output:
        print(result.stdout)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-40:])
        return False, f"runner exited with {result.returncode}\n{tail}"

    output = _extract_final_output(result.stdout)
    output_issues = _check_output(output, case)
    verify_issue = _run_verify_script(bench_env, adb, case)

    ui_text, issues = _dump_and_check_ui_text(bench_env, adb, case)
    issues = [*output_issues, *([verify_issue] if verify_issue else []), *issues]
    if issues:
        snippet = "\n".join(ui_text.splitlines()[:80])
        output_snippet = "\n".join(output.splitlines()[:20])
        return False, "; ".join(issues) + f"\nFinal output:\n{output_snippet}\nUI text head:\n{snippet}"
    return True, str(case.get("pass_message") or "case assertions passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", help="run one case label")
    parser.add_argument("--show-runner-output", action="store_true")
    args = parser.parse_args()

    env = os.environ.copy()
    try:
        adb = _find_adb(env)
        _ensure_device(env, adb)
    except Exception as exc:  # noqa: BLE001
        print(f"环境检查未通过：{exc}")
        return 1

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if args.label:
        cases = [c for c in cases if c.get("label") == args.label]
        if not cases:
            print(f"未找到 case label: {args.label}")
            return 1

    print("── Android Mini Benchmark ──")
    passed = 0
    failed = 0
    for case in cases:
        ok, detail = _run_case(
            case,
            env=env,
            adb=adb,
            show_runner_output=args.show_runner_output,
        )
        passed += ok
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {case['label']}  {detail}")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
