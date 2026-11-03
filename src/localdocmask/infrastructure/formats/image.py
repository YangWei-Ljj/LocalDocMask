"""图片处理器：OCR 检测 + 像素级红化（真正删除），统一输出 PNG。"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from ...domain.entities import ScanResult
from ..ocr.engine import OCREngine
from .base import FormatHandler


class ImageHandler(FormatHandler):
    name = "image"
    extensions = [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"]

    def extract(self, path: str) -> ScanResult:
        scan = ScanResult()
        img = Image.open(path)
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        scan.warnings.append(f"图片尺寸 {img.size[0]}x{img.size[1]}，已 OCR 检测")
        # 印章/红色区域启发式
        try:
            arr = np.asarray(img.convert("RGB"))
            hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
            mask = cv2.inRange(hsv, (0, 80, 80), (12, 255, 255)) | cv2.inRange(hsv, (170, 80, 80), (180, 255, 255))
            ratio = float(mask.mean())
            if ratio > 0.02:
                scan.warnings.append("检测到疑似印章（大面积红色区域），需人工复核")
        except Exception:
            pass
        items = OCREngine.ocr(np.asarray(img))
        ocr_texts = []
        for it in items:
            text = it["text"].strip()
            if not text:
                continue
            ocr_texts.append(text)
            xs = [p[0] for p in it["box"]]
            ys = [p[1] for p in it["box"]]
            box = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
            loc = f"图片 OCR 区域({box[0]},{box[1]},{box[2]},{box[3]})"
            for cand in self.detector.detect_text(text, location=loc):
                cand.source = "ocr"
                cand.box = box
                scan.candidates.append(cand)
        scan.text = "\n".join(ocr_texts)
        return scan

    def process(self, path: str, out_path: str, candidates: list) -> list[str]:
        warnings: list[str] = []
        img = Image.open(path)
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        else:
            img = img.convert("RGB")
        arr = np.asarray(img).copy()
        for cand in candidates:
            if cand.box is None:
                warnings.append(f"无法定位图像区域：{cand.category}「{cand.text}」")
                continue
            x0, y0, x1, y1 = cand.box
            pad = 10 if cand.delete else 2
            x0 = max(0, int(x0) - pad)
            y0 = max(0, int(y0) - pad)
            x1 = min(arr.shape[1], int(x1) + pad)
            y1 = min(arr.shape[0], int(y1) + pad)
            arr[y0:y1, x0:x1] = (0, 0, 0)  # 像素级删除
        Image.fromarray(arr).save(out_path, "PNG")
        return warnings
