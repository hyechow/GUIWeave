"""Runtime calibration for semantic page scrolling."""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from policy_expr.executor import ActionExecutor
from policy_expr.executor_constants import SCROLL_DELTA, SCROLL_TICKS
from policy_expr.gesture import action_with_drag, action_with_wheel, drag_gesture, wheel_gesture
from policy_expr.perception import LivePhoneSession
from policy_expr.schemas import Action
from policy_expr.stitch import robust_shift as _kp_robust_shift, _gray_u8 as _kp_gray


@dataclass(frozen=True)
class ScrollProfile:
    """A scroll configuration that produced observable vertical progress."""

    method: str
    x: float
    y: float
    direction: str
    ticks: int = SCROLL_TICKS
    delta_px: int = SCROLL_DELTA
    to_x: float | None = None
    to_y: float | None = None
    duration_ms: int | None = None
    observed_shift_px: int = 0
    confidence: float = 0.0


@dataclass(frozen=True)
class ScrollProbeResult:
    """Probe outcome plus the screenshot after the last attempted scroll."""

    success: bool
    profile: ScrollProfile | None
    after_png: bytes | None
    attempts: int
    reason: str


# Matches an already-appended verified-point suffix so re-applying the profile
# (e.g. every turn of a scroll loop) doesn't accumulate it: …y=760）（…y=760）（…）.
_VERIFIED_SUFFIX = re.compile(r"（使用已验证(?:拖动|滚动)点 y=\d+）")


def _with_verified_suffix(description: str | None, kind: str, y: float) -> str:
    """Append the verified-point note once, stripping any prior one first (idempotent)."""
    base = _VERIFIED_SUFFIX.sub("", description or "").rstrip()
    return f"{base}（使用已验证{kind}点 y={y:.0f}）"


def apply_profile(action: Action, profile: ScrollProfile) -> Action:
    """Return an action using a validated scroll profile."""

    if profile.method == "drag":
        return action.model_copy(
            update={
                "action_type": "drag",
                "method": "drag",
                "x": profile.x,
                "y": profile.y,
                "to_x": profile.to_x,
                "to_y": profile.to_y,
                "duration_ms": profile.duration_ms,
                "description": _with_verified_suffix(action.description, "拖动", profile.y),
            }
        )
    return action.model_copy(
        update={
            "x": profile.x,
            "y": profile.y,
            "direction": profile.direction,
            "method": "wheel",
            "description": _with_verified_suffix(action.description, "滚动", profile.y),
        }
    )


class ScrollProbe:
    """Try a few wheel coordinates and keep the first one with vertical progress."""

    def __init__(
        self,
        phone: LivePhoneSession,
        executor: ActionExecutor,
        log_dir: Path,
        *,
        settle_seconds: float = 0.8,
    ) -> None:
        self.phone = phone
        self.executor = executor
        self.log_dir = log_dir
        self.settle_seconds = settle_seconds

    def probe(self, before_png: bytes, action: Action, *, turn_no: int) -> ScrollProbeResult:
        direction = (action.direction or "").strip().lower()
        if direction not in {"down", "up", "向下", "向上", "downward", "upward"}:
            return ScrollProbeResult(False, None, None, 0, f"unsupported direction: {direction!r}")

        candidates = _candidate_actions(action)
        print(f"  [ScrollProbe] 开始探测有效滚动点，候选 {len(candidates)} 个")
        current_png = before_png
        last_after: bytes | None = None
        attempt_summaries: list[str] = []
        for idx, candidate in enumerate(candidates, start=1):
            if candidate.action_type == "scroll":
                concrete = action_with_wheel(candidate, wheel_gesture(candidate))
            elif candidate.action_type == "drag":
                concrete = action_with_drag(candidate, drag_gesture(candidate))
            else:
                continue
            # 第一个候选承担「冷启动税」：刚进页/刚切焦点时第一次手势常不生效，给它 2 次机会，
            # 重试一次(此时已被上一次手势热身)往往就成。否则会把可靠的 wheel 误判为无效、退而
            # 选复用不可靠的 drag（真机 20260530_162926：冷 wheel=0→选 drag→drag 复用失败→
            # turn2 被迫重探）。后续候选此时已是热的，单次即可，不浪费手势。
            max_attempts = 2 if idx == 1 else 1
            for attempt in range(1, max_attempts + 1):
                print(
                    "  [ScrollProbe] "
                    f"try {idx}.{attempt}: {candidate.action_type} x={concrete.x:.0f}, y={concrete.y:.0f}"
                )
                if candidate.action_type == "scroll":
                    gesture = wheel_gesture(candidate)
                    self.executor.execute_scroll(concrete, ticks=gesture.ticks, delta_px=gesture.delta_px)
                else:
                    self.executor.execute_drag(concrete)
                time.sleep(self.settle_seconds)
                after_png = self.phone.screenshot()
                last_after = after_png
                _save_probe_shot(self.log_dir, turn_no, idx, after_png)

                # 关键点匹配估位移（ORB）：在「重复行+大片白底」列表上比全局像素相关/残差鲁棒，
                # 后者会被行混叠/白对白骗到 shift=0（真机 20260530_145732 turn4 成功滚动被误判失败）。
                shift, confidence = _kp_robust_shift(_kp_gray(current_png), _kp_gray(after_png))
                changed = _changed_ratio(current_png, after_png)
                print(
                    "  [ScrollProbe] "
                    f"shift={shift:+d}px confidence={confidence:.3f} changed={changed:.3f}"
                )
                attempt_summaries.append(f"{candidate.action_type}@y={concrete.y:.0f}:shift={shift:+d}px")
                current_png = after_png  # 更新基准：下一次(重试或下个候选)从当前态比
                if _is_expected_progress(direction, shift, confidence, changed):
                    wheel = wheel_gesture(candidate) if candidate.action_type == "scroll" else None
                    profile = ScrollProfile(
                        method=candidate.action_type,
                        x=float(concrete.x or 500),
                        y=float(concrete.y or 500),
                        direction=str(candidate.direction or direction),
                        ticks=wheel.ticks if wheel else SCROLL_TICKS,
                        delta_px=wheel.delta_px if wheel else SCROLL_DELTA,
                        to_x=concrete.to_x,
                        to_y=concrete.to_y,
                        duration_ms=concrete.duration_ms,
                        observed_shift_px=shift,
                        confidence=confidence,
                    )
                    print(
                        "  [ScrollProbe] 命中有效滚动点: "
                        f"method={profile.method}, x={profile.x:.0f}, y={profile.y:.0f}, shift={shift:+d}px"
                    )
                    return ScrollProbeResult(True, profile, after_png, idx, "vertical progress observed")

        return ScrollProbeResult(
            False,
            None,
            last_after,
            len(candidates),
            "no candidate produced reliable vertical progress; tried "
            + ", ".join(attempt_summaries),
        )


def estimate_vertical_shift(before_png: bytes, after_png: bytes, *, max_shift: int = 180) -> tuple[int, float]:
    """Estimate dominant vertical translation from before to after.

    Negative shift means content moved upward in the after image, which is the
    expected visual effect of scrolling down to later page content.
    """

    before = _analysis_array(before_png)
    after = _analysis_array(after_png)
    h = min(before.shape[0], after.shape[0])
    best_shift = 0
    best_score = -1.0
    zero_score = _score_shift(before, after, 0, h)
    for shift in range(-max_shift, max_shift + 1, 2):
        score = _score_shift(before, after, shift, h)
        if score > best_score:
            best_score = score
            best_shift = shift
    confidence = max(0.0, best_score - zero_score)
    return best_shift, confidence


def _candidate_actions(action: Action) -> list[Action]:
    direction = action.direction or "down"
    # Keep this intentionally tiny: one wheel attempt at a stable lower-center
    # content point, then one touch-like drag at the same point.
    wheel = action.model_copy(
        update={
            "method": "wheel",
            "direction": direction,
            "description": f"{action.description}（滚动探测-wheel）",
        }
    )
    drag = action.model_copy(
        update={
            "action_type": "drag",
            "method": "drag",
            "direction": direction,
            "description": f"{action.description}（滚动探测-drag）",
        }
    )
    return [wheel, drag]


def _analysis_array(png_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape
    # Keep the main app content and avoid frame corners, status/nav chrome, and
    # bottom safe-area noise. The crop is intentionally broad so it works across apps.
    y1, y2 = int(h * 0.18), int(h * 0.90)
    x1, x2 = int(w * 0.08), int(w * 0.92)
    crop = arr[y1:y2, x1:x2]
    crop -= crop.mean()
    std = crop.std()
    if std > 1e-6:
        crop /= std
    return crop


def _score_shift(before: np.ndarray, after: np.ndarray, shift: int, height: int) -> float:
    if shift < 0:
        a = before[-shift:height]
        b = after[: height + shift]
    elif shift > 0:
        a = before[: height - shift]
        b = after[shift:height]
    else:
        a = before
        b = after
    if a.size == 0 or b.size == 0:
        return -1.0
    return float((a * b).mean())


def _changed_ratio(before_png: bytes, after_png: bytes) -> float:
    before = np.asarray(Image.open(io.BytesIO(before_png)).convert("RGB"), dtype=np.int16)
    after = np.asarray(Image.open(io.BytesIO(after_png)).convert("RGB"), dtype=np.int16)
    h = min(before.shape[0], after.shape[0])
    w = min(before.shape[1], after.shape[1])
    diff = np.abs(before[:h, :w] - after[:h, :w]).sum(axis=2)
    return float((diff > 60).mean())


def _is_expected_progress(direction: str, shift: int, confidence: float, changed: float) -> bool:
    # confidence 现为关键点主导簇内点占比（0~1）：真实滚动 ~0.3+，无滚动时 robust_shift 直接返回 0。
    if abs(shift) < 14:
        return False
    if confidence < 0.10:
        return False
    d = (direction or "").lower()
    if d == "down" and shift > 0:   # 向下滚 → 内容上移 shift<0；符号相反说明不是预期滚动
        return False
    if d == "up" and shift < 0:
        return False
    return changed >= 0.015


def _save_probe_shot(log_dir: Path, turn_no: int, idx: int, png_bytes: bytes) -> None:
    path = log_dir / f"scroll_probe_turn_{turn_no}_{idx}.png"
    try:
        path.write_bytes(png_bytes)
    except OSError:
        pass
