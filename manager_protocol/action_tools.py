"""Per-action tool schemas used by the manager protocol experiment.

Each physical action gets its own argument model.  The resulting tool call is
normalized back through the platform's real ActionDecision model before it is
scored, so the experiment changes the model protocol without changing the
runtime action contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Direction = Literal["up", "down", "left", "right"]
Amount = Literal["small", "medium", "large"]
TargetArea = Literal[
    "main_content",
    "left_panel",
    "right_panel",
    "top_content",
    "bottom_content",
]
IPhoneTargetArea = Literal[
    "main_content",
    "left_panel",
    "right_panel",
    "top_content",
    "bottom_content",
    "picker_left",
    "picker_center",
    "picker_right",
]


class ActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", description="操作目标的简短说明")


class TapArgs(ActionArgs):
    x: float = Field(
        ge=0,
        le=1000,
        description="目标中心的归一化 x 坐标；必须是单个数值，不能是数组或范围",
    )
    y: float = Field(
        ge=0,
        le=1000,
        description="目标中心的归一化 y 坐标；必须是单个数值，不能是数组或范围",
    )


class TypeTextArgs(ActionArgs):
    text: str = Field(min_length=1, description="要输入的完整文字")
    x: float | None = Field(default=None, ge=0, le=1000, description="输入框中心 x；已聚焦时可省略")
    y: float | None = Field(default=None, ge=0, le=1000, description="输入框中心 y；已聚焦时可省略")


class NoCoordinateArgs(ActionArgs):
    pass


class ScrollArgs(ActionArgs):
    direction: Direction = Field(description="内容查看方向")
    amount: Amount = Field(default="medium", description="滚动幅度")
    target_area: TargetArea = Field(default="main_content", description="滚动区域")
    x: float | None = Field(default=None, ge=0, le=1000, description="局部滚动区域中心 x")
    y: float | None = Field(default=None, ge=0, le=1000, description="局部滚动区域中心 y")


class DragArgs(ActionArgs):
    x: float = Field(ge=0, le=1000, description="拖动起点 x")
    y: float = Field(ge=0, le=1000, description="拖动起点 y")
    to_x: float = Field(ge=0, le=1000, description="拖动终点 x")
    to_y: float = Field(ge=0, le=1000, description="拖动终点 y")
    duration_ms: int | None = Field(default=None, ge=1, description="可选拖动持续时间")


class IPhoneGestureArgs(ActionArgs):
    direction: Direction | None = Field(default=None, description="普通内容滚动方向")
    value_direction: Literal["increase", "decrease"] | None = Field(
        default=None, description="picker 数值变化方向"
    )
    target_area: IPhoneTargetArea = Field(default="main_content", description="滚动区域或 picker 列")
    amount: Amount = Field(default="medium", description="滚动幅度")
    method: Literal["auto", "wheel", "drag"] = Field(default="auto", description="执行方式")
    x: float | None = Field(default=None, ge=0, le=1000, description="可选滚动锚点 x")
    y: float | None = Field(default=None, ge=0, le=1000, description="可选滚动锚点 y")

    @model_validator(mode="after")
    def _requires_direction(self) -> "IPhoneGestureArgs":
        if not (self.direction or self.value_direction):
            raise ValueError("需要 direction 或 value_direction")
        return self


class NavigateArgs(ActionArgs):
    url: str = Field(min_length=1, description="要打开的网址或域名")


class NewTabArgs(ActionArgs):
    url: str | None = Field(default=None, description="新标签页可选网址")


class SelectTabArgs(ActionArgs):
    tab_match: str = Field(min_length=1, description="要切换到的标签页标题或网址子串")


class CloseTabArgs(ActionArgs):
    tab_match: str | None = Field(default=None, description="要关闭的标签页；省略表示当前标签页")


class UploadFileArgs(ActionArgs):
    file_path: str = Field(min_length=1, description="任务给出的本地文件路径")
    x: float | None = Field(default=None, ge=0, le=1000, description="上传控件中心 x")
    y: float | None = Field(default=None, ge=0, le=1000, description="上传控件中心 y")


class SelectOptionArgs(ActionArgs):
    text: str = Field(min_length=1, description="要选择的选项文本")
    x: float = Field(ge=0, le=1000, description="下拉控件中心 x")
    y: float = Field(ge=0, le=1000, description="下拉控件中心 y")


class ScrollToRefArgs(ActionArgs):
    target_ref: int = Field(description="当前帧 backend DOM node id")


@dataclass(frozen=True)
class ActionTool:
    name: str
    action_type: str
    description: str
    args_model: type[BaseModel]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


_SHARED_TOOLS = (
    ActionTool("tap", "tap", "点击一个当前可见的控件或内容项", TapArgs),
    ActionTool("type_text", "type", "在输入框中输入文字；会自动聚焦并替换原内容", TypeTextArgs),
    ActionTool("clear_text", "clear_text", "清空当前已聚焦的输入框", NoCoordinateArgs),
    ActionTool("press_enter", "press_enter", "按回车提交、搜索、确认或换行", NoCoordinateArgs),
    ActionTool("scroll", "scroll", "滚动页面或局部容器以查看其他内容", ScrollArgs),
    ActionTool("drag", "drag", "从一个明确坐标拖动到另一个坐标", DragArgs),
)


def action_tools(platform: str) -> tuple[ActionTool, ...]:
    """Return only the physical actions supported by one platform."""
    if platform == "browser":
        return (*_SHARED_TOOLS,
            ActionTool("navigate", "navigate", "直接打开一个网址，不与网页输入框交互", NavigateArgs),
            ActionTool("back", "back", "浏览器历史后退", NoCoordinateArgs),
            ActionTool("new_tab", "new_tab", "新建浏览器标签页，可同时打开网址", NewTabArgs),
            ActionTool("select_tab", "select_tab", "切换到指定浏览器标签页", SelectTabArgs),
            ActionTool("close_tab", "close_tab", "关闭指定或当前浏览器标签页", CloseTabArgs),
            ActionTool("upload_file", "upload", "把任务给出的本地文件上传到当前页面", UploadFileArgs),
            ActionTool("select_option", "select_option", "在原生下拉控件中直接选择选项", SelectOptionArgs),
            ActionTool("scroll_to_ref", "scroll_to_ref", "将指定 DOM 节点滚动到视口内", ScrollToRefArgs),
        )
    if platform == "android":
        return (*_SHARED_TOOLS,
            ActionTool("home", "home", "回到 Android 主屏幕", NoCoordinateArgs),
            ActionTool("back", "back", "使用 Android 系统返回键", NoCoordinateArgs),
            ActionTool("app_switch", "app_switch", "打开 Android 最近任务视图", NoCoordinateArgs),
        )
    if platform == "iphone":
        non_gesture = _SHARED_TOOLS[:4]
        return (*non_gesture,
            ActionTool("scroll", "scroll", "滚动普通内容或 picker 列", IPhoneGestureArgs),
            ActionTool("drag", "drag", "用拖动手势滚动 picker 列或内容", IPhoneGestureArgs),
            ActionTool("home", "home", "回到 iPhone 主屏幕", NoCoordinateArgs),
            ActionTool("app_switch", "app_switch", "打开 iPhone App 切换器", NoCoordinateArgs),
        )
    raise ValueError(f"unsupported platform: {platform}")


def decision_from_tool_call(
    platform: str,
    decision_model: type[BaseModel],
    name: str,
    args: dict[str, Any],
) -> BaseModel:
    """Validate one tool call and normalize it through the real platform schema."""
    tools = {tool.name: tool for tool in action_tools(platform)}
    tool = tools.get(name)
    if tool is None:
        raise ValueError(f"unknown {platform} action tool: {name}")
    parsed = tool.args_model.model_validate(args)
    action = {
        "action_type": tool.action_type,
        **parsed.model_dump(mode="python", exclude_none=True),
    }
    return decision_model.model_validate({"action": action})


__all__ = ["ActionTool", "action_tools", "decision_from_tool_call"]
