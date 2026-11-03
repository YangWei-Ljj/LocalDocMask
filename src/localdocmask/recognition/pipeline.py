"""识别门面：规则 → 上下文 → 知识 → （本地 LLM 低权重补充）→ 融合仲裁。

对应 module_contracts §2 / ADR-003。本模块为可被流水线调用的独立识别门面；
不修改 application/pipeline.py（后续统一集成）。
"""
from __future__ import annotations

from pathlib import Path

from ..domain.entities import Candidate
from ..domain.policy import ReviewPolicy
from .context import ContextAnalyzer
from .fusion import FusionEngine
from .knowledge import KnowledgeBase
from .rule_based import Detector

# 词库统一放在项目根 resources/（任务一：资源归位）。
# pipeline.py 位于 src/localdocmask/recognition/，parents[3] 即项目根。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_RESOURCES = _PROJECT_ROOT / "resources"


def extract_ner_entities_worker(model_dir, device, max_length, text):
    """在独立 worker 中重建批准 NER 并执行推理（生产调用入口）。"""
    from ..infrastructure.ner.onnx_ner import OnnxChineseNER

    ner = OnnxChineseNER(model_dir=model_dir, device=device,
                         max_length=max_length)
    return ner.extract_entities(text)


class RecognitionPipeline:
    def __init__(self, knowledge: KnowledgeBase | None = None,
                 policy: ReviewPolicy | None = None,
                 llm=None,
                 ner=None,
                 enable_company_regex: bool = True,
                 detector: Detector | None = None,
                 ner_worker_target=None,
                 ner_worker_args: tuple = ()):
        self.knowledge = knowledge or KnowledgeBase.load(_DEFAULT_RESOURCES)
        self.policy = policy or ReviewPolicy()
        self.llm = llm
        self.ner = ner
        self.ner_worker_target = ner_worker_target
        self.ner_worker_args = tuple(ner_worker_args)
        self.enable_company_regex = enable_company_regex
        self.detector = detector or Detector(enable_company_regex=enable_company_regex)
        self.fusion = FusionEngine(self.policy, self.knowledge, ContextAnalyzer())

    # ---------- 从文本完整识别 ----------
    def analyze(self, text: str, location: str = "") -> list[Candidate]:
        if not text:
            return []
        cands = self.detector.detect_text(text, location=location)
        cands = self._knowledge_inject(cands, text, location)
        cands = self._llm_supplement(cands, text, location)
        cands = self._ner_supplement(cands, text, location)
        return self.fusion.fuse(cands, text)

    # ---------- 对 handler 已定位候选做裁决（不重新定位） ----------
    def adjudicate(self, candidates: list[Candidate], text: str) -> list[Candidate]:
        if not candidates:
            return []
        return self.fusion.fuse(list(candidates), text or "")

    # ---------- 内部 ----------
    def _knowledge_inject(self, cands: list[Candidate], text: str,
                          location: str) -> list[Candidate]:
        """词库主动注入：黑名单/领域词不被正则覆盖时，按命中位置补充候选。"""
        existing = {(c.category, c.text) for c in cands}

        def add(cat: str, val: str, src: str, conf: float):
            if (cat, val) in existing:
                return
            existing.add((cat, val))
            cands.append(Candidate(text=val, category=cat, location=location,
                                   confidence=conf, raw_confidence=conf, source=src))

        for term in sorted(self.knowledge.blacklist, key=len, reverse=True):
            if term and term in text:
                add("公司名", term, "blacklist", 0.95)
        for term in sorted(self.knowledge.domain_terms, key=len, reverse=True):
            if term and term in text:
                add("自定义敏感词", term, "domain", 0.92)
        # 公开实体：转人工复核（不自动脱敏，也不漏掉）
        for term in sorted(self.knowledge.public_entities, key=len, reverse=True):
            if term and term in text:
                add("公司名", term, "public", 0.60)
        return cands

    def _llm_supplement(self, cands: list[Candidate], text: str,
                        location: str) -> list[Candidate]:
        if self.llm is None:
            return cands
        try:
            pairs = self.llm.extract_entities(text[:8000])
        except Exception:
            return cands
        existing = {(c.category, c.text) for c in cands}
        for cat, val in pairs:
            if not val or (cat, val) in existing:
                continue
            cands.append(Candidate(text=val, category=cat, location=location,
                                    confidence=0.55, raw_confidence=0.55,
                                    source="ollama"))
        return cands

    def _ner_supplement(self, cands: list[Candidate], text: str,
                        location: str) -> list[Candidate]:
        """本地 NER 补充（ADR-009）：PER/ORG/LOC 仅作为低权重候选。

        - ner 未装配（默认关闭）或推理异常：原样返回，绝不中断主流程；
        - 输出 source="ner"，与 ollama 同口径进入融合仲裁；
        - 只补词典/规则未覆盖的实体，不重复已有 (类别, 文本)。
        """
        if self.ner is None:
            return cands
        try:
            pairs = self.ner.extract_entities(text[:8000])
        except Exception:
            return cands
        return self.merge_ner_entities(cands, pairs, location)

    def merge_ner_entities(self, cands: list[Candidate], pairs,
                           location: str) -> list[Candidate]:
        if not pairs:
            return cands
        existing = {(c.category, c.text) for c in cands}
        for cat, val in pairs:
            if not val or (cat, val) in existing:
                continue
            cands.append(Candidate(text=val, category=cat, location=location,
                                    confidence=0.55, raw_confidence=0.55,
                                    source="ner"))
        return cands
