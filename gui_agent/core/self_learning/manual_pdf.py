"""PDF manual → per-section page-knowledge via numbered-outline segmentation + vision.

A PDF manual is mostly annotated screenshots (red boxes / arrows / numbered callouts) whose
information plain text extraction drops entirely — for this kind of doc the text layer is a
thin skeleton and the meat lives in the figures. We instead exploit the document's own numbered
outline (1.1 / 2.3 / …): PyMuPDF (fitz) gives every text line and image block a bbox, so we

  1. segment by "N.N 标题" headings (a heading's bbox marks where a section starts), and
  2. assign each figure to the section whose span — heading-y to the NEXT heading-y, across page
     breaks — contains it. This fixes "页≠章节" (a figure that flows onto the next page still
     belongs to its own section).

Each section's page band (its text + figures + annotations together) is rendered and read by a
vision LLM into ONE page-knowledge .md — the same per-page knowledge shape that
``app_summary.generate_summary`` already reduces into _app.md / _elements.md. So manual ingestion
reuses the existing reduce, identical to the iPhone recon → summary flow.

``fitz`` is imported lazily inside the entry point so importing this module stays cheap.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.provider_config import dashscope_extra_body

# 形如 "1.1"/"2.3"/"1.6"(后跟空格再接文字);顶层 "2. 订单" 不算叶子小节,不匹配。
_HEAD = re.compile(r"^\s*(\d+(?:\.\d+){1,2})\s+(\S.*)$")

_SECTION_SYSTEM = load_prompt_text("task.self_learning.manual_pdf.section_system")


@dataclass
class Section:
    num: str                 # "1.1"
    title: str               # 标题文字
    page: int                # 0-based 起始页
    y: float                 # 起始 y(PDF 点)
    text: str = ""           # 该节正文(非标题行)
    imgs: list = field(default_factory=list)  # [(page, bbox)] 归属本节的图
    end_page: int = 0        # 该节结束页(= 下一节起始页)
    end_y: float = 0.0       # 该节结束 y


def extract_sections(doc) -> list[Section]:
    """Segment the document into numbered leaf sections, assigning each figure by position."""
    stream = []  # (page, y, kind, payload),按阅读顺序(页, y)
    for pno in range(len(doc)):
        for blk in doc[pno].get_text("dict")["blocks"]:
            if blk["type"] == 0:  # 文字块
                for line in blk["lines"]:
                    txt = "".join(s["text"] for s in line["spans"]).strip()
                    if txt:
                        stream.append((pno, line["bbox"][1], "text", txt))
            elif blk["type"] == 1:  # 图片块
                stream.append((pno, blk["bbox"][1], "img", blk["bbox"]))
    stream.sort(key=lambda r: (r[0], r[1]))

    secs: list[Section] = []
    cur: Optional[Section] = None
    for pno, y, kind, payload in stream:
        if kind == "text":
            m = _HEAD.match(payload)
            if m:
                cur = Section(num=m.group(1), title=m.group(2).strip("\x01 ​"), page=pno, y=y)
                secs.append(cur)
            elif cur is not None:
                cur.text += payload + "\n"
        elif cur is not None:  # 图归到当前节
            cur.imgs.append((pno, payload))

    for i, s in enumerate(secs):  # 每节结束 = 下一节起点;最后一节到文末
        if i + 1 < len(secs):
            s.end_page, s.end_y = secs[i + 1].page, secs[i + 1].y
        else:
            s.end_page, s.end_y = len(doc) - 1, doc[len(doc) - 1].rect.height
    return secs


def render_bands(doc, s: Section, zoom: float = 2.0, max_pages: Optional[int] = None) -> list[bytes]:
    """Rasterize the section's layout band (heading-y → next heading-y, per page) to PNGs.

    Cropping to the band (rather than extracting raw image xrefs) keeps vector annotations drawn
    OVER the screenshots — the red boxes / arrows are usually a separate layer, lost if you pull
    the embedded raster alone. ``max_pages`` clamps the band to a leading page range (for tests).
    """
    import fitz

    last = s.end_page if max_pages is None else min(s.end_page, max_pages - 1)
    out: list[bytes] = []
    for p in range(s.page, last + 1):
        rect = doc[p].rect
        y0 = s.y if p == s.page else 0.0
        y1 = s.end_y if p == s.end_page else rect.height
        if y1 - y0 < 8:
            continue
        clip = fitz.Rect(0, y0, rect.width, y1)
        pix = doc[p].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        out.append(pix.tobytes("png"))
    return out


def _vision_llm() -> ChatOpenAI:
    # Reuse the action_policy vision model (already proven on screenshots).
    cfg = resolve_llm_config("action_policy")
    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
        extra_body=dashscope_extra_body(cfg.model),
    )


def parse_section(llm: ChatOpenAI, s: Section, bands: list[bytes]) -> str:
    """Vision-read one section's bands into a page-knowledge markdown doc."""
    content: list = [{
        "type": "text",
        "text": f"小节标题:{s.title}\n该节抽取文字:\n{s.text.strip() or '(无)'}",
    }]
    for png in bands:
        b64 = base64.b64encode(png).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    msgs = [SystemMessage(content=_SECTION_SYSTEM), HumanMessage(content=content)]
    return str(llm.invoke(msgs).content).strip()


def _safe_name(title: str) -> str:
    """Filename from the section TITLE only — the manual's 1.1/2.3 numbering isn't useful to
    the agent (the final _app.md/_elements.md are renumbered by the reduce anyway)."""
    return re.sub(r"\W+", "_", title, flags=re.UNICODE).strip("_")[:30] or "section"


def parse_pdf_to_pages(
    pdf_path: Path,
    out_dir: Path,
    max_pages: Optional[int] = None,
    llm: Optional[ChatOpenAI] = None,
) -> int:
    """Parse a PDF manual into per-section page-knowledge .md files under ``out_dir``.

    Image-bearing sections go through the vision LLM (reading figures + annotations); pure-text
    sections (FAQ entries with no figure) are written from their extracted text alone — no
    needless vision call. Returns the number of section files written.
    """
    import fitz

    doc = fitz.open(str(pdf_path))
    secs = extract_sections(doc)
    if max_pages is not None:
        secs = [s for s in secs if s.page < max_pages]
    llm = llm or _vision_llm()
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(secs)
    n_img = sum(1 for s in secs if any(max_pages is None or im[0] < max_pages for im in s.imgs))
    print(f"  共 {total} 个编号小节(其中 {n_img} 节带图走视觉,{total - n_img} 节纯文字)", flush=True)

    written = 0
    used: set[str] = set()
    t_start = time.time()
    for i, s in enumerate(secs, 1):
        imgs_in_range = [im for im in s.imgs if max_pages is None or im[0] < max_pages]
        label = f"{len(imgs_in_range)}图·视觉" if imgs_in_range else "纯文字"
        print(f"  [{i}/{total}] {s.title}  ({label}) …", flush=True)
        t0 = time.time()
        if imgs_in_range:
            bands = render_bands(doc, s, max_pages=max_pages)
            md = parse_section(llm, s, bands)
        else:
            md = f"### {s.title}\n\n{s.text.strip()}"
        name = _safe_name(s.title)
        if name in used:  # 重名兜底(标题罕见撞名),加序号区分,非手册编号
            k = 2
            while f"{name}_{k}" in used:
                k += 1
            name = f"{name}_{k}"
        used.add(name)
        (out_dir / f"{name}.md").write_text(md, encoding="utf-8")
        written += 1
        print(f"        ✓ {time.time() - t0:.1f}s → {name}.md", flush=True)
    print(f"  小节解析完成:{written} 节,用时 {time.time() - t_start:.0f}s", flush=True)
    return written
