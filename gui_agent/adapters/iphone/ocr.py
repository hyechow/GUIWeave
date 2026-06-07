"""macOS Vision OCR for iPhone-mirroring screenshots (via ocrmac).

Lifted from the former top-level gui_agent.utils (S3 step 6). The iPhone
executor uses this for OCR-based tap snapping. Vision / ocrmac is macOS-specific,
so it belongs in the iphone adapter.
"""

from dataclasses import dataclass

from ocrmac.ocrmac import OCR

LANGUAGE_PREFERENCE = ["zh-Hans", "en-US"]


@dataclass
class OcrResult:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    def tap_coords(self, width: int, height: int, y_offset: float = 0.0) -> tuple[float, float]:
        """Convert to tap tool coords (top-left origin, logical pixels).
        Pass window size, not screenshot size (Retina screenshots are 2×).
        y_offset: normalized offset applied before conversion, negative = upward."""
        px = self.center_x * width
        py = (1.0 - (self.center_y + y_offset)) * height
        return px, py


def ocr_from_bytes(png_bytes: bytes) -> tuple[list[OcrResult], tuple[int, int]]:
    """Run OCR on raw PNG bytes. Returns (results, (width, height))."""
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(png_bytes))
    results = OCR(image, language_preference=LANGUAGE_PREFERENCE).recognize()
    ocr_results = [
        OcrResult(text=text, confidence=conf, x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        for text, conf, bbox in results
    ]
    return ocr_results, image.size
