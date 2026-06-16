"""CLI entry point for app reconnaissance and self-learning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__ is None or __package__ == "":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
from PIL import Image

load_dotenv()

from gui_agent.adapters.iphone.recon import PageParser, viz_result
from gui_agent.adapters.iphone.recon.bfs import probe_elements

ROOT = Path(__file__).resolve().parents[3]
LOG_ROOT = ROOT / "logs" / "recon"
# Knowledge is platform-scoped (knowledge/<platform>/<app>/); recon is iPhone-only.
KNOWLEDGE_ROOT = ROOT / "knowledge" / "iphone"
OFFLINE_EXPECTED_SIZE = (636, 1402)




# ── Core recon ────────────────────────────────────────────

def _parse_identity(phone) -> tuple:
    """Screenshot → parse page identity. Returns (png_bytes, knowledge, page_name)."""
    from gui_agent.adapters.iphone.recon.dfs import _page_name_from_fingerprint
    png_bytes = phone.screenshot()

    knowledge = PageParser().analyze_screen(png_bytes)
    page_name, _, _ = _page_name_from_fingerprint(png_bytes)
    return png_bytes, knowledge, page_name


def _probe_page(phone, knowledge, png_bytes, out_dir: Path, sample: int = 0,
                nav_stack: list | None = None):
    """Save initial screenshot + probe all elements. Returns (result, out_dir)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    img_path = out_dir / "initial.png"
    img_path.write_bytes(png_bytes)
    viz_result(knowledge, png_bytes, "initial", out_dir)

    result = probe_elements(phone.client, knowledge, out_dir, img_path, phone.screenshot,
                            sample=sample, nav_stack=nav_stack)
    result.save(out_dir / "recon_result.json")

    ok = sum(1 for t in result.taps if t.tap_ok and t.navigated)
    print(f"  探测完成: {len(result.taps)} 个元素 (成功导航 {ok})")

    return result, out_dir


# ── Commands ─────────────────────────────────────────────

def run_app(app: str, depth: int = 0, sample: int = 0,
            mode: str | None = None, target: str | None = None,
            hud=None) -> None:
    """Online recon with DFS depth control.

    depth=0: explore current page only.
    depth=N: DFS explore discovered pages (up to N levels deep).
    mode: None=initial, "add"=add page to existing app, "update"=re-probe target page.
    target: required for both "add" (parent page dir) and "update" (page dir to overwrite).
    """
    from gui_agent.adapters.iphone.perception import LivePhoneSession
    from gui_agent.adapters.iphone.recon.dfs import explore_dfs

    if mode in ("add", "update") and not target:
        label = "父页面目录名" if mode == "add" else "页面目录名"
        print(f"错误: --mode {mode} 需要指定 --target <{label}>")
        raise SystemExit(1)

    mode_label = {"add": "新增", "update": "更新"}.get(mode, "侦察")
    print(f"应用{mode_label}: {app} (depth={depth}, sample={sample})")
    app_log_dir = LOG_ROOT / app
    app_log_dir.mkdir(parents=True, exist_ok=True)

    from gui_agent.core.run_io import tee_stdio
    with tee_stdio(app_log_dir):
        if hud:
            hud.update(f"侦察 {app}: 预加载模型…")

        # Preload all local models before exploration starts
        print("预加载模型...")
        from gui_agent.adapters.iphone.recon.cascade_matcher import get_matcher
        from gui_agent.adapters.iphone.recon.icon_detector import warm_up as _yolo_warm_up
        get_matcher().warm_up()
        _yolo_warm_up()

        if hud:
            hud.update(f"侦察 {app}: DFS 探索 (depth={depth}, sample={sample})…")

        with LivePhoneSession() as phone:
            # Phase 1: DFS exploration (probe only, no knowledge gen)
            tree = explore_dfs(phone, app_log_dir, max_depth=depth, sample=sample,
                               mode=mode, target_dir=target,
                               status_cb=hud.update if hud else None)

        # Phase 2: Post-order knowledge generation (leaves first) — DISABLED
        # total = sum(1 + _count_tree_nodes(n.children) for n in tree)
        # if total > 0:
        #     print(f"\n--- 开始知识生成 (自底向上, {total} 个页面) ---")
        #     generate_knowledge_postorder(tree, app)
        #     print("知识生成完成")


def _count_tree_nodes(nodes) -> int:
    from gui_agent.adapters.iphone.recon.dfs import _count_nodes
    return _count_nodes(nodes)


def run_parse(paths: list[Path]) -> None:
    """Parse page structure. With paths: offline. Without: online."""
    if paths:
        _parse_offline(paths)
    else:
        _parse_online()


def _parse_online() -> None:
    from gui_agent.adapters.iphone.perception import LivePhoneSession

    print("在线解析: 截取手机屏幕...")
    with LivePhoneSession() as phone:
        png_bytes, knowledge, page_name = _parse_identity(phone)
        _probe_page(phone, knowledge, png_bytes, LOG_ROOT / "online" / page_name)


def _parse_offline(paths: list[Path]) -> None:
    images: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".png":
            images.append(p)
        elif p.is_dir():
            images.extend(sorted(p.glob("*.png")))
    if not images:
        print("未找到 PNG 图片")
        return

    print(f"离线解析: 共 {len(images)} 张图片")
    parser = PageParser()
    out_dir = LOG_ROOT / "offline"
    for img_path in images:
        with Image.open(img_path) as img:
            if img.size != OFFLINE_EXPECTED_SIZE:
                print(f"截图尺寸不匹配: {img_path} 实际 {img.size}，期望 {OFFLINE_EXPECTED_SIZE}")
                raise SystemExit(1)
        png_bytes = img_path.read_bytes()
        initial_path = out_dir / f"{img_path.stem}_initial.png"
        initial_path.parent.mkdir(parents=True, exist_ok=True)
        initial_path.write_bytes(png_bytes)
        print(f"\n图片: {img_path.name}")
        knowledge = parser.analyze_screen(png_bytes)
        viz_result(knowledge, png_bytes, img_path.stem, out_dir)


def run_export(app: str, page: str | None = None, enhanced: bool = False, hud=None) -> None:
    """Export page knowledge for all (or one specific) pages of an app.

    Reads initial_result.json + recon_result.json per page, runs one LLM call,
    writes page_meta.json + knowledge.md locally and syncs to knowledge/{app}/.
    Also generates knowledge for leaf pages discovered but not probed.
    """
    from gui_agent.core.self_learning.knowledge import (
        build_export, build_leaf_export, collect_leaf_pages, save_export,
    )

    app_log_dir = LOG_ROOT / app
    if page:
        page_dirs = [app_log_dir / page]
    else:
        page_dirs = sorted(
            p for p in app_log_dir.iterdir()
            if p.is_dir() and (p / "recon_result.json").exists()
        )

    print(f"\n--- 导出页面知识: {app} ({len(page_dirs)} 个已探测页面) ---")
    for idx, page_dir in enumerate(page_dirs, 1):
        print(f"\n  [{page_dir.name}]")
        if hud:
            hud.update(f"导出 {app}  {idx}/{len(page_dirs)}  {page_dir.name}")
        try:
            exported = build_export(page_dir, mode="enhanced" if enhanced else "strict")
            save_export(exported, page_dir, KNOWLEDGE_ROOT / app)
            print(f"  ✓ {exported.meta.page_title} ({exported.meta.page_type})")
        except Exception as e:
            print(f"  ✗ {e}")

    # Leaf pages: discovered but not probed (depth_limit or overlay_skip)
    if not page:
        knowledge_dir = KNOWLEDGE_ROOT / app
        knowledge_dir.mkdir(parents=True, exist_ok=True)

        # Stale cleanup: remove old leaf knowledge files using previous leaf_meta titles
        prev_meta_path = app_log_dir / "leaf_meta.json"
        prev_leaf_titles: set[str] = set()
        if prev_meta_path.exists():
            prev_meta = json.loads(prev_meta_path.read_text("utf-8"))
            for entry in prev_meta.values():
                t = entry.get("title", "")
                if t:
                    prev_leaf_titles.add(t.replace("/", "_").replace(" ", "_"))
        for stale in knowledge_dir.glob("*.md"):
            if stale.stem in prev_leaf_titles:
                stale.unlink()

        app = knowledge_dir.name
        leaves = collect_leaf_pages(app_log_dir)
        # Only block overwriting knowledge files from probed pages (not old leaf files)
        probed_titles = {f.stem for f in knowledge_dir.glob("*.md")}
        # Build mapping: leaf_key → {title, type, parent} for report builder
        # For depth_limit pages: key = page_name
        # For overlay pages: key = "overlay::{parent_name}::{label}" (page_name is empty)
        leaf_meta: dict[str, dict] = {}
        if leaves:
            print(f"\n--- 叶子页 ({len(leaves)} 个未探测页面) ---")
            for leaf_idx, (parent_name, tap) in enumerate(leaves, 1):
                try:
                    if hud:
                        raw = (tap.get("identity") or {}).get("page_name", "") or parent_name
                        hud.update(f"叶子页 {leaf_idx}/{len(leaves)}  {raw}")
                    result = build_leaf_export(parent_name, tap)
                    ident = tap.get("identity") or {}
                    raw_name = ident.get("page_name", "")
                    is_overlay = ident.get("phase") == "overlay_skip"
                    meta_key = (
                        f"overlay::{parent_name}::{tap.get('label', '')}"
                        if is_overlay else raw_name
                    )
                    leaf_meta[meta_key] = {
                        "title": result.meta.page_title,
                        "type": result.meta.page_type,
                        "parent": parent_name,
                        "is_overlay": is_overlay,
                    }
                    safe_title = result.meta.page_title.replace("/", "_").replace(" ", "_")
                    if safe_title in probed_titles:
                        print(f"  ⊘ {result.meta.page_title} — 已有已探测版本，跳过")
                        continue
                    skill_text = result.knowledge.to_skill(app, result.meta.parent_page)
                    (knowledge_dir / f"{safe_title}.md").write_text(skill_text, encoding="utf-8")
                    probed_titles.add(safe_title)
                    print(f"  ✓ {result.meta.page_title} ({result.meta.page_type}) ← {parent_name}")
                except Exception as e:
                    print(f"  ✗ {e}")
        # Save leaf title mapping for report builder
        if leaf_meta:
            (app_log_dir / "leaf_meta.json").write_text(
                json.dumps(leaf_meta, ensure_ascii=False, indent=2), encoding="utf-8",
            )

    print(f"\n  输出目录: {KNOWLEDGE_ROOT / app}")


# ── CLI ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="App Recon: 页面结构分析与自学习")
    ap.add_argument("--app", type=str, metavar="NAME", help="在线侦察指定应用，自动完成 recon→learn→knowledge")
    ap.add_argument("--depth", type=int, default=0, metavar="N", help="DFS 探索深度，0=仅当前页面 (默认)")
    ap.add_argument("--sample", type=int, default=0, metavar="N", help="每个页面随机采样 N 个元素探测，0=全部 (默认)")
    ap.add_argument("--mode", choices=["add", "update"], metavar="MODE", help="add=新增页面到已有应用, update=重新探测指定页面")
    ap.add_argument("--target", type=str, metavar="DIR", help="add=父页面目录名, update=要更新的页面目录名")
    ap.add_argument("--export", type=str, metavar="APP", help="导出指定应用的页面知识（page_meta.json + knowledge.md）")
    ap.add_argument("--page", type=str, metavar="DIR", help="--export 时只导出指定页面目录")
    ap.add_argument("--enhanced", action="store_true", help="--export 时启用增强模式：补充视觉检测到的未探测元素")
    ap.add_argument("--debug-parse", nargs="*", type=Path, metavar="PATH", help="[调试] 解析图片，无参数则在线截图")
    ap.add_argument("--hud", action="store_true", help="在 iPhone 镜像窗口下方显示实时探测状态面板")
    args = ap.parse_args()

    hud = None
    if args.hud:
        from gui_agent.adapters.iphone.hud import make_iphone_hud
        hud = make_iphone_hud()

    try:
        if args.app:
            run_app(args.app, depth=args.depth, sample=args.sample,
                    mode=args.mode, target=args.target, hud=hud)
        elif args.export:
            run_export(args.export, page=args.page, enhanced=args.enhanced, hud=hud)
        elif args.debug_parse is not None:
            run_parse(args.debug_parse)
        else:
            ap.print_help()
    finally:
        if hud:
            hud.close()


if __name__ == "__main__":
    main()
