from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class OcrProvider(Protocol):
    name: str

    def recognize(self, image_path: Path) -> list[str]:
        ...


@dataclass(frozen=True)
class OcrDetection:
    text: str
    bounds: tuple[int, int, int, int]
    confidence: float = 0.0


class NullOcrProvider:
    name = "none"

    def recognize(self, image_path: Path) -> list[str]:
        return []


class PaddleOcrProvider:
    """可选的本地 OCR。未安装 PaddleOCR 时不会影响 UI/XML 采集。"""

    name = "paddleocr"

    def __init__(self) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError("请安装 OCR 依赖：uv sync --extra ocr") from exc
        self._ocr = PaddleOCR(lang="ch")

    def recognize(self, image_path: Path) -> list[str]:
        result = self._ocr.predict(str(image_path))
        texts: list[str] = []
        for item in result or []:
            payload = item if isinstance(item, dict) else getattr(item, "json", {})
            if callable(payload):
                payload = payload()
            if isinstance(payload, dict):
                for text in payload.get("rec_texts", []) or []:
                    if text and text not in texts:
                        texts.append(str(text))
        return texts


class RapidOcrProvider:
    """Mac 本地 OCR，适合抖音自绘店铺卡和商品卡；不依赖在线大模型。"""

    name = "rapidocr"

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise RuntimeError("请安装 OCR 依赖：uv sync --extra ocr") from exc
        self._ocr = RapidOCR()

    def recognize_boxes(self, image_path: Path) -> list[OcrDetection]:
        result, _elapsed = self._ocr(str(image_path))
        detections: list[OcrDetection] = []
        for item in result or []:
            if not item or len(item) < 2:
                continue
            polygon, text = item[0], str(item[1]).strip()
            if not text or not polygon:
                continue
            try:
                points = [(float(point[0]), float(point[1])) for point in polygon]
                left = int(min(point[0] for point in points))
                top = int(min(point[1] for point in points))
                right = int(max(point[0] for point in points))
                bottom = int(max(point[1] for point in points))
                confidence = float(item[2]) if len(item) > 2 else 0.0
            except (TypeError, ValueError, IndexError):
                continue
            detections.append(OcrDetection(text, (left, top, right, bottom), confidence))
        return detections

    def recognize(self, image_path: Path) -> list[str]:
        values: list[str] = []
        for detection in self.recognize_boxes(image_path):
            if detection.text not in values:
                values.append(detection.text)
        return values


def create_ocr_provider(name: str) -> OcrProvider:
    """按配置创建 OCR；默认关闭，避免首次安装被大型 OCR 依赖阻塞。"""
    normalized = name.strip().lower()
    if normalized in {"", "none", "disabled", "off"}:
        return NullOcrProvider()
    if normalized in {"rapid", "rapidocr"}:
        return RapidOcrProvider()
    if normalized in {"paddle", "paddleocr"}:
        return PaddleOcrProvider()
    raise ValueError(f"未知 OCR 提供方：{name}，可选 none、rapidocr、paddleocr")
