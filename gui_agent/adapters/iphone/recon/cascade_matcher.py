"""Cascade page matcher: shared GUIClip + LLM + bge-small-zh models."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

# ── Structured semantic fingerprint ───────────────────────────────────────

SEMANTIC_PROMPT = """\
分析这个 iPhone App 截图，输出页面结构描述。

严格按以下格式输出三行：
用途：<这类页面让用户做什么，10字以内的动宾短语>
内容区：<6字以内描述主内容区布局，只能从以下选择：双列网格/三列网格/单列列表/图标网格/气泡对话/表单输入/混合布局/纯文本/图片瀑布>
页面形态：<从以下选项中选择一个，只写字母代号>
  A — 全屏页面（屏幕底部无Tab栏）
  B — Tab页（屏幕最底部有固定Tab栏：2-5个图标+文字标签均匀排列，横贯整个屏幕宽度）
  C — 弹窗（浮层覆盖在底层页面上，底层内容仍可见）
  D — 底部面板（从底部滑出的半屏面板）
  E — 侧边抽屉（从侧边滑出）
  F — 菜单（下拉或弹出选项列表）
  G — 键盘页（软键盘覆盖底部）

规则：
- 判断A还是B：先看屏幕最底部——有横贯屏幕宽度的"图标+文字"Tab栏就选B，否则选A
- 有弹窗/面板/抽屉等覆盖层时，三行全部描述覆盖层本身，不描述底层页面
- "用途"和"内容区"只描述主体内容区，不提Tab栏、状态栏、顶部导航栏
- 全部中文"""


class PageFingerprint(BaseModel):
    purpose: str = Field(description="用途字段的值")
    content: str = Field(description="内容区字段的值")
    form: str = Field(description="页面形态字母代号，A-G")


@dataclass
class PageEmbedding:
    """Visual + optional text embedding for a screenshot."""

    visual: np.ndarray          # GUIClip embedding, shape (512,), L2-normalised
    text: np.ndarray | None = None  # bge-small-zh embedding, shape (512,), lazy


class CascadeMatcher:
    """Holds GUIClip and bge-small-zh models; generates and compares page embeddings.

    GUIClip and bge are loaded lazily on first use.
    Intended to be created once and shared across PageIdentity and PageComparator.
    """

    def __init__(
        self,
        guiclip_model: str = "Jl-wei/guiclip-vit-base-patch32",
        embed_model: str = "BAAI/bge-small-zh",
        llm_config_key: str = "fingerprint",
    ):
        self._guiclip_name = guiclip_model
        self._embed_name = embed_model
        self._llm_config_key = llm_config_key
        self._clip_model: Any = None
        self._clip_proc: Any = None
        self._embed: Any = None
        self._purpose_cache: dict[str, np.ndarray] = {}

    # ── warm-up ───────────────────────────────────────────────────────────────

    def warm_up(self) -> None:
        """Eagerly load all local models. Call at program startup."""
        import time
        for label, loader in [("GUIClip", self._load_guiclip), ("bge-small-zh", self._load_embed)]:
            t0 = time.time()
            print(f"  [warm_up] 加载 {label}...", end=" ", flush=True)
            loader()
            print(f"({time.time() - t0:.1f}s)")

    # ── lazy loading ──────────────────────────────────────────────────────────

    def _load_guiclip(self) -> None:
        if self._clip_model is not None:
            return
        from transformers import CLIPModel, CLIPProcessor
        self._clip_proc = CLIPProcessor.from_pretrained(
            self._guiclip_name, local_files_only=True
        )
        self._clip_model = CLIPModel.from_pretrained(
            self._guiclip_name, local_files_only=True
        )
        self._clip_model.eval()

    def _load_embed(self) -> None:
        if self._embed is not None:
            return
        from sentence_transformers import SentenceTransformer
        self._embed = SentenceTransformer(self._embed_name, local_files_only=True)

    # ── internal ops ──────────────────────────────────────────────────────────

    def _compute_visual(self, png: bytes) -> np.ndarray:
        import torch
        import torch.nn.functional as F
        self._load_guiclip()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        with torch.no_grad():
            inputs = self._clip_proc(images=img, return_tensors="pt")
            pixel = {k: v for k, v in inputs.items() if k.startswith("pixel")}
            emb = self._clip_model.visual_projection(
                self._clip_model.vision_model(**pixel).pooler_output
            )
            emb = F.normalize(emb, p=2, dim=1)
        return emb.squeeze(0).numpy()

    def _compute_text(self, fingerprint: str) -> np.ndarray:
        self._load_embed()
        return self._embed.encode(fingerprint, normalize_embeddings=True)

    def generate_fingerprint(self, png: bytes) -> PageFingerprint:
        """Generate structured semantic fingerprint (form + content + purpose)."""
        from llm.structured import invoke_structured
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
        from gui_agent.core.policies.base import resize_to_logical_png
        from gui_agent.core.config import resolve_llm_config

        cfg = resolve_llm_config(self._llm_config_key)
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key,
                         base_url=cfg.base_url, temperature=0)
        b64 = base64.b64encode(resize_to_logical_png(png)).decode()
        messages = [
            SystemMessage(content=SEMANTIC_PROMPT),
            HumanMessage(content=[
                {"type": "text", "text": "请分析这个页面："},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]),
        ]
        return invoke_structured(llm, messages, PageFingerprint)

    def _embed_purpose(self, purpose: str) -> np.ndarray:
        """Embed a purpose string; cached by text to avoid redundant bge calls."""
        if purpose not in self._purpose_cache:
            self._purpose_cache[purpose] = self._compute_text(purpose)
        return self._purpose_cache[purpose]

    # ── public embedding API ──────────────────────────────────────────────────

    def embed_visual(self, png: bytes) -> PageEmbedding:
        """Compute visual-only embedding (fast, no LLM call)."""
        return PageEmbedding(visual=self._compute_visual(png))

    def embed_full(self, png: bytes) -> PageEmbedding:
        """Compute visual + text embeddings (calls LLM for fingerprint)."""
        visual = self._compute_visual(png)
        fp = self.generate_fingerprint(png)
        text = self._embed_purpose(fp.purpose)
        return PageEmbedding(visual=visual, text=text)

    def fill_text(self, emb: PageEmbedding, png: bytes) -> None:
        """Lazily generate and attach text embedding if not already present."""
        if emb.text is None:
            fp = self.generate_fingerprint(png)
            emb.text = self._embed_purpose(fp.purpose)

    # ── similarity ────────────────────────────────────────────────────────────

    def visual_sim(self, a: PageEmbedding, b: PageEmbedding) -> float:
        return float(np.dot(a.visual, b.visual))

    def text_sim(self, a: PageEmbedding, b: PageEmbedding) -> float:
        if a.text is None or b.text is None:
            raise ValueError("text embeddings not populated; call fill_text first")
        return float(np.dot(a.text, b.text))

    def semantic_sim(self, fp1: PageFingerprint, fp2: PageFingerprint) -> tuple[bool, str]:
        """Compare two structured fingerprints.

        Returns (is_same, reason).
        Same = form matches AND content matches AND purpose embed >= 0.90.
        """
        form_match = fp1.form == fp2.form
        content_match = fp1.content == fp2.content

        self._load_embed()
        purpose_sim = float(np.dot(self._embed_purpose(fp1.purpose), self._embed_purpose(fp2.purpose)))

        if not form_match:
            return False, f"form_diff({fp1.form}!={fp2.form})"
        if not content_match:
            return False, f"content_diff({fp1.content}!={fp2.content})"
        if purpose_sim < 0.90:
            return False, f"purpose_low({purpose_sim:.3f})"
        return True, f"semantic_match(purpose={purpose_sim:.3f})"


# ---------------------------------------------------------------------------
# Process-wide singleton — avoids loading GUIClip more than once
# ---------------------------------------------------------------------------

_shared: CascadeMatcher | None = None


def get_matcher(**kwargs) -> CascadeMatcher:
    """Return the shared CascadeMatcher, creating it on first call."""
    global _shared
    if _shared is None:
        _shared = CascadeMatcher(**kwargs)
    return _shared
