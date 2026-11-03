"""本地 OCR（RapidOCR，离线）。"""
from __future__ import annotations


class OCREngine:
    _engine = None

    @classmethod
    def get(cls):
        if cls._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            cls._engine = RapidOCR()
        return cls._engine

    @classmethod
    def ocr(cls, image) -> list[dict]:
        """image 可为路径 / PIL Image / numpy 数组，返回 [{text, confidence, box}]。"""
        try:
            engine = cls.get()
            res, _ = engine(image)
            out: list[dict] = []
            if res:
                for item in res:
                    box, text, score = item[0], item[1], item[2]
                    out.append({"text": str(text), "confidence": float(score), "box": box})
            return out
        except Exception as e:
            raise RuntimeError(f"OCR 失败：{e}") from e
