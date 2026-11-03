"""本地 Ollama 客户端：仅访问本机服务，模型输出只作候选。"""
from __future__ import annotations

import json
import re

import requests

from .config import OLLAMA_URL, DEFAULT_MODEL


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str = OLLAMA_URL, model: str = DEFAULT_MODEL, timeout: int = 300):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def tags(self) -> list[str]:
        try:
            r = requests.get(self.url + "/api/tags", timeout=10)
            if r.status_code != 200:
                return []
            return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            return []

    def generate(self, prompt: str, num_predict: int = 1024) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": 0},
        }
        try:
            r = requests.post(self.url + "/api/generate", json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise OllamaError(f"无法连接本地 Ollama：{e}") from e
        if r.status_code != 200:
            raise OllamaError(f"Ollama 返回 {r.status_code}：{r.text[:200]}")
        return r.json().get("response") or ""

    def extract_entities(self, text: str) -> list[tuple[str, str]]:
        """返回 (类别, 文本) 候选列表。类别为 公司名 / 姓名。"""
        prompt = (
            "你是实体识别助手。请从下面文本中找出公司名和人名，只输出一行 JSON，"
            '格式为 {"companies": ["公司名1"], "persons": ["姓名1"]}，'
            "不要输出任何解释。找不到就输出空列表。\n文本：\n" + text
        )
        out = self.generate(prompt, num_predict=1024)
        return self._parse(out)

    def _parse(self, out: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        try:
            start = out.find("{")
            end = out.rfind("}")
            if start == -1 or end <= start:
                return result
            obj = json.loads(out[start:end + 1])
            if not isinstance(obj, dict):
                return result
            for name in obj.get("companies", []):
                if isinstance(name, str) and name.strip():
                    result.append(("公司名", name.strip()))
            for name in obj.get("persons", []):
                if isinstance(name, str) and name.strip():
                    result.append(("姓名", name.strip()))
        except Exception:
            return result
        return result
