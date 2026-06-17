"""Export page knowledge from recon results.

One LLM call per page: reads initial_result.json + recon_result.json,
produces page_meta.json (structured identity) + knowledge.md (skill text).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config


PageType = Literal["list", "detail", "chat", "form", "modal", "home", "other"]


# ── Output models ─────────────────────────────────────────

class ElementKnowledge(BaseModel):
    label: str
    position: str = Field(description="语义位置，如「右上角」「底部导航栏」「列表区域」")
    function: str = Field(description="功能描述，如「进入搜索页面」「弹出操作菜单」")


class PageKnowledge(BaseModel):
    """LLM output: normalized page knowledge."""
    page_title: str = Field(description="2-4个字的页面标题，如「聊天列表」「个人主页」")
    page_type: PageType = Field(description="页面类型")
    description: str = Field(description="1-2句话概括页面功能，通用化，不含私有信息")
    operations: list[ElementKnowledge]

    def to_skill(self, app: str, parent_page: str = "") -> str:
        lines = [
            "---",
            f"app: {app}",
            f"page_title: {self.page_title}",
            f"page_type: {self.page_type}",
        ]
        if parent_page:
            lines.append(f"parent_page: {parent_page}")
        lines += ["---", "", f"# {self.page_title}", "", self.description]
        if self.operations:
            lines.append("")
            for op in self.operations:
                lines.append(f"- [{op.label}] {op.position} → {op.function}")
        return "\n".join(lines)


@dataclass
class PageMeta:
    page_title: str
    page_type: str
    parent_page: str
    description: str

    def to_dict(self) -> dict:
        return {
            "page_title": self.page_title,
            "page_type": self.page_type,
            "parent_page": self.parent_page,
            "description": self.description,
        }


@dataclass
class ExportResult:
    meta: PageMeta
    knowledge: PageKnowledge


# ── LLM prompt ───────────────────────────────────────────

_EXPORT_COMMON = """\
你是一个 iPhone 应用页面分析专家。给定一个页面的探测数据，完成以下任务：

## 输出要求

**page_title**：页面的唯一标识名称，4-8个字。要求：
- 必须包含功能域 + 页面形态，如「公众号订阅列表」「群聊消息详情」「联系人个人资料」
- 禁止使用纯通用词（「列表页」「详情页」「主页」），必须加上具体功能域
- 同一应用内每个页面的 page_title 必须互不相同，可区分

**page_type**：页面类型
- list：列表页（聊天列表、联系人、消息列表）
- detail：详情页（个人资料、文章详情）
- chat：聊天/对话界面
- form：表单/输入页
- modal：弹窗/底部弹出
- home：应用主页
- other：其他

**description**：1-2句话概括页面功能。要求：
- 通用化，不含具体联系人、消息内容等私有信息
- 说明页面用途和关键功能区

**operations**：抽象操作列表。核心原则：**label 和 function 都必须是通用描述，绝对不能出现具体内容**。

通用抽象规则：
1. **列表行合并**：同类型的多行（聊天、文章、联系人、商品等）合并为一条，label 用通用名如「聊天行」「文章行」「联系人行」
2. **去除所有具体内容**：人名、店铺名、商品名、文章标题、消息正文、地名、账号名、金额等一律替换为通用描述
3. **保留功能性标签**：搜索栏、+号按钮、设置按钮等本身就是通用名称，保持原样
"""

EXPORT_PROMPT = _EXPORT_COMMON + """\
所有元素均经过实测探测。function 规则：
4. 有导航结果的标注「进入…」，将「实测→」中的具体页面名抽象为通用类型（如实测→「个人中心概览」写为「进入个人中心」）
5. 无导航的描述其交互用途（如「收藏」「分享」）

示例（错误 → 正确）：
- ✗ [Mythos 限] 实测→「…」 → ✓ [文章行]，function：进入文章详情页
- ✗ [张三] 实测→「…」 → ✓ [聊天行]，function：进入该联系人的聊天详情
- ✗ [长安网咖] 实测→「…」 → ✓ [店铺行]，function：进入店铺详情页
"""

EXPORT_PROMPT_ENHANCED = _EXPORT_COMMON + """\
元素数据分为两类：
- **已探测**（带「实测→」标记）：经过真实点击，有导航结果，可信度高
- **未探测**（带「未探测（视觉检测）」标记）：仅通过截图识别，无实测导航数据

4. **function 按数据来源区分**：
  - 已探测元素：标注「进入…」，将具体页面名抽象为通用类型（如实测→「个人中心概览」写为「进入个人中心」）
  - 未探测元素：根据元素类型推测交互用途（如「进入搜索页面」「查看活动详情」）

示例（错误 → 正确）：
- ✗ [Mythos 限] 实测→「…」 → ✓ [文章行]，function：进入文章详情页
- ✗ [长安网咖] 实测→「…」 → ✓ [店铺行]，function：进入店铺详情页
- ✗ [入口图标] 未探测 → ✓ [功能入口]，function：进入对应功能页面
"""

_EXPORT_LEAF = _EXPORT_COMMON + """\
你将收到一张页面截图和该页面的文字描述。请直接分析截图中所有可交互元素，生成页面知识。

4. **function 按元素类型推断**：根据元素的视觉特征推测交互用途（如「进入搜索页面」「查看活动详情」）
5. 底部导航栏/Tab栏统一写「切换应用功能模块」
"""


def _resize_for_api(png_bytes: bytes, max_dim: int = 768) -> bytes:
    """Resize image to fit within max_dim for API call."""
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _semantic_position(x: float, y: float, element_type: str) -> str:
    if element_type == "tab":
        return "底部导航栏"
    if element_type == "back_button":
        return "左上角"
    if y < 180:
        return "右上角" if x > 700 else "顶部"
    if y > 900:
        return "底部"
    if element_type == "input":
        return "输入区域"
    return "列表区域"


def _build_element_lines(taps: list[dict]) -> list[str]:
    """Build element description lines from probe taps only (verified data)."""
    lines = []
    for tap in taps:
        label = tap.get("label", "")
        el_type = tap.get("element_type", "area")
        x, y = tap.get("x", 500), tap.get("y", 500)
        pos = _semantic_position(x, y, el_type)

        if tap.get("navigated"):
            ident = tap.get("identity") or {}
            if ident.get("phase") == "overlay_skip":
                nav_note = "实测→弹窗/浮层"
            else:
                dest = ident.get("description", "") or ident.get("page_name", "")
                nav_note = f"实测→「{dest[:30]}」" if dest else "实测→已导航"
            lines.append(f"[{label}]  {pos}  {nav_note}")
        else:
            lines.append(f"[{label}]  {pos}  无导航")

    return lines


# ── Public API ────────────────────────────────────────────

def build_export(page_dir: Path, mode: str = "strict") -> ExportResult:
    """Build export for one page directory.

    mode:
      - "strict": only use probe tap results
      - "enhanced": probe results + PageParser-detected elements (unprobed marked as visual-only)
    """
    page_dir = page_dir.resolve()

    init_path = page_dir / "initial_result.json"
    recon_path = page_dir / "recon_result.json"

    if not init_path.exists():
        raise FileNotFoundError(f"initial_result.json not found: {page_dir}")
    if not recon_path.exists():
        raise FileNotFoundError(f"recon_result.json not found: {page_dir}")

    init_data = json.loads(init_path.read_text("utf-8"))
    recon_data = json.loads(recon_path.read_text("utf-8"))

    raw_description = init_data.get("fingerprint") or init_data.get("page", {}).get("description", "")
    taps: list[dict] = recon_data.get("taps", [])
    parent_page: str = recon_data.get("parent_page", "")

    element_lines = _build_element_lines(taps)

    # Enhanced mode: supplement with elements from initial parse
    if mode == "enhanced":
        init_areas = init_data.get("areas", [])
        if init_areas:
            # Build set of probed element positions for dedup
            probed_positions = set()
            for tap in taps:
                probed_positions.add((round(tap.get("x", 0)), round(tap.get("y", 0))))
            for area in init_areas:
                xy = area.get("center_xy", [])
                if len(xy) < 2:
                    continue
                ax, ay = xy
                # Skip if close to an already-probed element
                if any(abs(round(ax) - px) < 50 and abs(round(ay) - py) < 50 for px, py in probed_positions):
                    continue
                pos = _semantic_position(ax, ay, area.get("element_type", "area"))
                element_lines.append(f"[{area.get('label', '')}]  {pos}  未探测（视觉检测）")

    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )

    import base64

    element_text = "\n".join(f"  {l}" for l in element_lines)
    prompt = EXPORT_PROMPT_ENHANCED if mode == "enhanced" else EXPORT_PROMPT
    text_content = (
        f"父页面：{parent_page or '无（根页面）'}\n"
        f"页面描述：{raw_description}\n\n"
        f"可交互元素（{len(element_lines)} 个）：\n{element_text}"
    )
    human_parts: list[dict] = [{"type": "text", "text": text_content}]
    screenshot_path = page_dir / "initial.png"
    if screenshot_path.exists():
        png_bytes = _resize_for_api(screenshot_path.read_bytes())
        b64 = base64.b64encode(png_bytes).decode()
        human_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    messages = [
        SystemMessage(content=prompt),
        HumanMessage(content=human_parts),
    ]

    knowledge = invoke_structured(llm, messages, PageKnowledge)

    meta = PageMeta(
        page_title=knowledge.page_title,
        page_type=knowledge.page_type,
        parent_page=parent_page,
        description=knowledge.description,
    )

    return ExportResult(meta=meta, knowledge=knowledge)


def save_export(result: ExportResult, page_dir: Path, knowledge_dir: Path) -> None:
    """Save page_meta.json locally and knowledge.md to knowledge_dir."""
    # page_meta.json in the recon page directory
    meta_path = page_dir / "page_meta.json"
    meta_path.write_text(
        json.dumps(result.meta.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Derive app name from knowledge_dir name
    app = knowledge_dir.name

    # knowledge.md: local copy + sync to knowledge/{app}/
    skill_text = result.knowledge.to_skill(app, result.meta.parent_page)
    (page_dir / "knowledge.md").write_text(skill_text, encoding="utf-8")

    knowledge_dir.mkdir(parents=True, exist_ok=True)
    safe_title = result.meta.page_title.replace("/", "_").replace(" ", "_")
    (knowledge_dir / f"{safe_title}.md").write_text(skill_text, encoding="utf-8")


def collect_leaf_pages(app_log_dir: Path) -> list[tuple[str, dict]]:
    """Scan parent recon_result.json files for leaf pages without knowledge.

    Collects:
    - child_status=new_depth_limit  : explored but depth-limited
    - identity.phase=overlay_skip   : overlay/popup pages (detected but not probed)
    """
    leaves: list[tuple[str, dict]] = []
    for recon_path in sorted(app_log_dir.rglob("recon_result.json")):
        data = json.loads(recon_path.read_text("utf-8"))
        parent_name = recon_path.parent.name
        for tap in data.get("taps", []):
            if tap.get("child_status") == "new_depth_limit":
                leaves.append((parent_name, tap))
            elif tap.get("identity", {}).get("phase") == "overlay_skip":
                leaves.append((parent_name, tap))
    return leaves


def build_leaf_export(parent_name: str, tap: dict) -> ExportResult:
    """Build export for a leaf page from parent tap entry data.

    Single LLM call: sends screenshot image + prompt to generate full PageKnowledge.
    """
    import base64

    identity = tap.get("identity") or {}
    screenshot_path = tap.get("screenshot", "")

    if identity.get("phase") == "overlay_skip":
        # Overlay page: no fingerprint/description in identity; derive from triggering element
        label = tap.get("label", "")
        description = f"由「{label}」触发的弹窗/浮层" if label else "弹窗/浮层页面"
    else:
        raw_name = identity.get("page_name", "未知页面")
        description = identity.get("description", "")
        fingerprint = identity.get("fingerprint", "")
        if not description:
            description = fingerprint[:80].strip() if fingerprint else raw_name

    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )

    # Build message: screenshot image + context text
    human_parts: list[dict] = [
        {"type": "text", "text": f"父页面：{parent_name}\n页面描述：{description}"},
    ]
    if screenshot_path and Path(screenshot_path).exists():
        png_bytes = _resize_for_api(Path(screenshot_path).read_bytes())
        b64 = base64.b64encode(png_bytes).decode()
        human_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    messages = [
        SystemMessage(content=_EXPORT_LEAF),
        HumanMessage(content=human_parts),
    ]

    knowledge = invoke_structured(llm, messages, PageKnowledge)

    meta = PageMeta(
        page_title=knowledge.page_title,
        page_type=knowledge.page_type,
        parent_page=parent_name,
        description=knowledge.description,
    )
    return ExportResult(meta=meta, knowledge=knowledge)
