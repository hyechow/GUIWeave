"""Compare two screenshots via CascadeMatcher: visual sim + semantic fingerprint."""

import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.recon.cascade_matcher import get_matcher, PageFingerprint


# ── Semantic fingerprint ──────────────────────────────────────────────────

FINGERPRINT_PROMPT = """\
分析这个 iPhone App 截图，输出页面结构描述。

严格按以下格式输出三行：
用途：<这类页面让用户做什么，10字以内的动宾短语>
内容区：<6字以内描述主内容区布局，只能从以下选择：双列网格/三列网格/单列列表/图标网格/气泡对话/表单输入/混合布局/纯文本/图片瀑布>
页面形态：<从以下选项中选择一个，只写字母代号>
  A — 全屏页面（无底部Tab栏）
  B — Tab页（底部有固定Tab栏，2-5个Tab）
  C — 弹窗（覆盖层，底层页面仍可见）
  D — 底部面板（从底部滑出的半屏面板）
  E — 侧边抽屉（从侧边滑出）
  F — 菜单（下拉或弹出选项列表）
  G — 键盘页（软键盘覆盖底部）

规则：
- 如果有弹窗/面板/抽屉等覆盖层，三行全部描述覆盖层本身
- 不提状态栏、顶部返回按钮、底部固定导航栏
- 全部中文"""

FORM_LABELS = {
    "A": "全屏页面", "B": "Tab页", "C": "弹窗", "D": "底部面板",
    "E": "侧边抽屉", "F": "菜单", "G": "键盘页",
}


class PageFingerprint(BaseModel):
    purpose: str = Field(description="用途字段的值")
    content: str = Field(description="内容区字段的值")
    form: str = Field(description="页面形态字母代号，A-G")


def _classify(png: bytes) -> PageFingerprint:
    cfg = resolve_llm_config("back_nav")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key,
                     base_url=cfg.base_url, temperature=0)
    b64 = base64.b64encode(resize_to_logical_png(png)).decode()
    messages = [
        SystemMessage(content=FINGERPRINT_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": "请分析这个页面："},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    return invoke_structured(llm, messages, PageFingerprint)


def _semantic_compare(fp1: PageFingerprint, fp2: PageFingerprint, m) -> dict:
    """Compare two fingerprints: form exact match + content exact match + purpose embed."""
    form_match = fp1.form == fp2.form
    content_match = fp1.content == fp2.content

    m._load_embed()
    e1 = m._compute_text(fp1.purpose)
    e2 = m._compute_text(fp2.purpose)
    purpose_sim = float(np.dot(e1, e2))

    same = form_match and content_match and purpose_sim > 0.90

    return {
        "form_match": form_match,
        "content_match": content_match,
        "purpose_sim": purpose_sim,
        "same": same,
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <image1> <image2>")
        sys.exit(1)

    p1, p2 = Path(sys.argv[1]), Path(sys.argv[2])
    png1, png2 = p1.read_bytes(), p2.read_bytes()

    m = get_matcher()

    # Visual similarity
    print("Computing visual embeddings ...")
    e1 = m.embed_full(png1)
    e2 = m.embed_full(png2)
    vis = m.visual_sim(e1, e2)
    txt = m.text_sim(e1, e2)
    print(f"\n  Visual similarity:  {vis:.4f}")
    print(f"  Text similarity:    {txt:.4f}")

    # Semantic fingerprint
    print("Computing semantic fingerprints ...")
    fp1 = _classify(png1)
    fp2 = _classify(png2)

    f1_label = FORM_LABELS.get(fp1.form, "?")
    f2_label = FORM_LABELS.get(fp2.form, "?")

    print(f"\n--- Semantic fingerprint ---")
    print(f"  {p1.name}:  [{fp1.form}]{f1_label}  内容={fp1.content}  用途={fp1.purpose}")
    print(f"  {p2.name}:  [{fp2.form}]{f2_label}  内容={fp2.content}  用途={fp2.purpose}")

    sem = _semantic_compare(fp1, fp2, m)

    print(f"\n--- Comparison ---")
    print(f"  页面形态: {'✓ SAME' if sem['form_match'] else '✗ DIFF'}  ({fp1.form} vs {fp2.form})")
    print(f"  内容区:   {'✓ SAME' if sem['content_match'] else '✗ DIFF'}  ({fp1.content} vs {fp2.content})")
    print(f"  用途 sim: {sem['purpose_sim']:.4f}  {'✓ ≥0.90' if sem['purpose_sim'] >= 0.90 else '✗ <0.90'}")

    print(f"\n  Semantic:  {'SAME' if sem['same'] else 'DIFF'}")
    print(f"  Visual:    {'SAME' if vis >= 0.85 else 'DIFF'}  (threshold 0.85)")
    print(f"  Combined:  {'SAME' if sem['same'] or vis >= 0.85 else 'DIFF'}")


if __name__ == "__main__":
    main()
