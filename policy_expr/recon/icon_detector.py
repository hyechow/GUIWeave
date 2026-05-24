"""YOLO-based icon detector for UI screenshots (ONNX Runtime backend)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

MODEL_PATH_PT = Path("models/omniparser_v2/icon_detect/model.pt")
MODEL_PATH_ONNX = MODEL_PATH_PT.with_suffix(".onnx")
DEFAULT_CONF = 0.3

# YOLO input size
_IMG_SIZE = 640
# NMS IoU threshold
_NMS_IOU = 0.45


@dataclass
class IconBbox:
    x1: float
    y1: float
    x2: float
    y2: float
    conf: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


_model_cache: dict[Path, Any] = {}


def warm_up(model_path: Path = MODEL_PATH_PT) -> None:
    """Eagerly load the YOLO model at program startup."""
    import time
    print(f"  [warm_up] 加载 YOLO...", end=" ", flush=True)
    t0 = time.time()
    path = _resolve_model_path(model_path)
    _load_model(path)
    print(f"({time.time() - t0:.1f}s)")


def _resolve_model_path(model_path: Path) -> Path:
    """Return .onnx if available, else .pt."""
    onnx_path = model_path.with_suffix(".onnx")
    return onnx_path if onnx_path.exists() else model_path


def _load_model(model_path: Path):
    """Load model once per process; subsequent calls return cached instance."""
    if model_path not in _model_cache:
        if model_path.suffix == ".onnx":
            import onnxruntime as ort
            _model_cache[model_path] = ort.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"],
            )
        else:
            from ultralytics import YOLO
            _model_cache[model_path] = YOLO(str(model_path))
    return _model_cache[model_path]


class IconDetector:
    """Wraps the OmniParser YOLO icon detection model."""

    def __init__(
        self,
        model_path: Path = MODEL_PATH_PT,
        conf: float = DEFAULT_CONF,
    ) -> None:
        self._path = _resolve_model_path(model_path)
        self._model = _load_model(self._path)
        self._conf = conf

    def detect(self, png_bytes: bytes) -> list[IconBbox]:
        """Return icon bounding boxes in pixel coordinates of the input image."""
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

        if self._path.suffix == ".onnx":
            return self._detect_onnx(img)
        # Fallback: ultralytics
        results = self._model(img, conf=self._conf, verbose=False)
        bboxes: list[IconBbox] = []
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                bboxes.append(IconBbox(x1, y1, x2, y2, float(box.conf[0])))
        return bboxes

    # ── ONNX inference path ──────────────────────────────────────────────

    def _detect_onnx(self, img: Image.Image) -> list[IconBbox]:
        import onnxruntime as ort

        session: ort.InferenceSession = self._model
        iw, ih = img.size

        # Letterbox resize
        resized, ratio, (pad_w, pad_h) = _letterbox(img, _IMG_SIZE)

        # HWC RGB → CHW BGR, normalize to [0,1]
        arr = np.array(resized, dtype=np.float32)
        arr = arr[:, :, ::-1]  # RGB → BGR
        arr = arr.transpose(2, 0, 1)  # HWC → CHW
        arr /= 255.0
        blob = arr[np.newaxis, ...]  # (1, 3, 640, 640)

        # Inference
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: blob})
        pred = outputs[0]  # (1, 5, 8400) for single-class: [cx, cy, w, h, conf]

        # Parse: shape is (1, 5, 8400) → transpose to (8400, 5)
        pred = pred[0].T  # (8400, 5)

        # Filter by confidence
        mask = pred[:, 4] >= self._conf
        pred = pred[mask]

        if len(pred) == 0:
            return []

        # cx, cy, w, h → x1, y1, x2, y2 (in 640×640 space)
        boxes_640 = np.zeros((len(pred), 4), dtype=np.float32)
        boxes_640[:, 0] = pred[:, 0] - pred[:, 2] / 2  # x1
        boxes_640[:, 1] = pred[:, 1] - pred[:, 3] / 2  # y1
        boxes_640[:, 2] = pred[:, 0] + pred[:, 2] / 2  # x2
        boxes_640[:, 3] = pred[:, 1] + pred[:, 3] / 2  # y2
        scores = pred[:, 4]

        # NMS
        keep = _nms(boxes_640, scores, _NMS_IOU)
        boxes_640 = boxes_640[keep]
        scores = scores[keep]

        # Scale back to original image coordinates
        bboxes: list[IconBbox] = []
        for i in range(len(boxes_640)):
            x1 = (boxes_640[i, 0] - pad_w) / ratio
            y1 = (boxes_640[i, 1] - pad_h) / ratio
            x2 = (boxes_640[i, 2] - pad_w) / ratio
            y2 = (boxes_640[i, 3] - pad_h) / ratio
            # Clamp to image bounds
            x1 = max(0, min(x1, iw))
            y1 = max(0, min(y1, ih))
            x2 = max(0, min(x2, iw))
            y2 = max(0, min(y2, ih))
            bboxes.append(IconBbox(x1, y1, x2, y2, float(scores[i])))

        return bboxes

    def detect_filtered(
        self,
        png_bytes: bytes,
        img_w: int,
        img_h: int,
        nav_bar_y: int = 180,
        max_width_ratio: float = 0.8,
        overlap_thresh: float = 0.8,
        min_gray_std: float = 5.0,
    ) -> list[IconBbox]:
        """Detect icons and apply three-layer filtering.

        1. Remove navigation/status-bar noise (cy < nav_bar_y)
        2. Remove wide boxes (width > max_width_ratio * img_w)
        3. Remove visually blank boxes (low grayscale standard deviation)
        4. Merge small boxes whose area >= overlap_thresh is inside a larger box
        """
        boxes = self.detect(png_bytes)

        # Filter 1: navigation/status bar
        boxes = [b for b in boxes if b.cy >= nav_bar_y]

        # Filter 2: overly wide
        max_w = img_w * max_width_ratio
        boxes = [b for b in boxes if (b.x2 - b.x1) <= max_w]

        # Filter 3: visually blank boxes
        gray = Image.open(io.BytesIO(png_bytes)).convert("L")
        boxes = [
            b for b in boxes
            if _gray_std(gray, b) >= min_gray_std
        ]

        # Filter 4: overlap-based dedup (larger boxes absorb smaller ones)
        def _overlap_ratio(small: IconBbox, big: IconBbox) -> float:
            ix1 = max(small.x1, big.x1)
            iy1 = max(small.y1, big.y1)
            ix2 = min(small.x2, big.x2)
            iy2 = min(small.y2, big.y2)
            if ix1 >= ix2 or iy1 >= iy2:
                return 0.0
            inter = (ix2 - ix1) * (iy2 - iy1)
            area = (small.x2 - small.x1) * (small.y2 - small.y1)
            return inter / area if area > 0 else 0.0

        sorted_boxes = sorted(boxes, key=lambda b: (b.x2 - b.x1) * (b.y2 - b.y1), reverse=True)
        covered: set[int] = set()
        for i, big in enumerate(sorted_boxes):
            for j in range(i + 1, len(sorted_boxes)):
                if j in covered:
                    continue
                if _overlap_ratio(sorted_boxes[j], big) >= overlap_thresh:
                    covered.add(j)
        return [b for i, b in enumerate(sorted_boxes) if i not in covered]


# ── Helpers ──────────────────────────────────────────────────────────────


def _letterbox(img: Image.Image, target: int) -> tuple[Image.Image, float, tuple[float, float]]:
    """Resize with letterbox padding. Returns (resized, ratio, (pad_w, pad_h))."""
    iw, ih = img.size
    ratio = min(target / iw, target / ih)
    new_w = int(iw * ratio)
    new_h = int(ih * ratio)
    pad_w = (target - new_w) / 2
    pad_h = (target - new_h) / 2

    resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (target, target), (114, 114, 114))
    canvas.paste(resized, (int(pad_w), int(pad_h)))
    return canvas, ratio, (pad_w, pad_h)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list[int]:
    """Non-Maximum Suppression. Returns list of kept indices."""
    order = scores.argsort()[::-1]
    keep: list[int] = []

    while len(order) > 0:
        i = order[0]
        keep.append(int(i))
        if len(order) == 1:
            break
        rest = order[1:]
        ious = _box_iou(boxes[i:i+1], boxes[rest])[0]
        mask = ious < iou_thresh
        order = rest[mask]

    return keep


def _box_iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Compute IoU between two sets of boxes. a: (N,4), b: (M,4) → (N,M)."""
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])

    inter_x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    inter_y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    inter_x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    inter_y2 = np.minimum(a[:, None, 3], b[None, :, 3])

    inter = np.maximum(0, inter_x2 - inter_x1) * np.maximum(0, inter_y2 - inter_y1)
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-6)


def _gray_std(img: Image.Image, box: IconBbox) -> float:
    crop = img.crop((
        max(0, int(box.x1)),
        max(0, int(box.y1)),
        min(img.width, int(box.x2)),
        min(img.height, int(box.x2)),
    ))
    if crop.width <= 0 or crop.height <= 0:
        return 0.0
    hist = crop.histogram()
    total = sum(hist)
    if total <= 0:
        return 0.0
    mean = sum(i * count for i, count in enumerate(hist)) / total
    variance = sum(((i - mean) ** 2) * count for i, count in enumerate(hist)) / total
    return variance ** 0.5
