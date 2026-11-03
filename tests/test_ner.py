# -*- coding: utf-8 -*-
"""本地中文 NER 的针对性测试（ADR-009）。

分两层：
1) 接线与降级层：用可编程 NER 桩验证 pipeline/fusion 接线、异常吞掉、
   未装配（默认关闭）时无副作用——不依赖真实模型，保证默认 138 测试不回归。
2) 真实模型层：仅当 ``models/ner_bert4ner/model.onnx`` 存在时运行，验证
   无标点连续动词串下公司边界正确、词典外人名召回、LOC 识别；模型缺失
   （如干净克隆的 CI）自动 skip，不冒充"已验证"。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from localdocmask.application.composition import build_services
from localdocmask.domain.entities import Candidate
from localdocmask.domain.enums import EntityDecision, TaskStatus
from localdocmask.infrastructure.config.settings import Settings
from localdocmask.infrastructure.ner.base import NerUnavailable
from localdocmask.recognition.fusion import FusionEngine
from localdocmask.recognition.knowledge import KnowledgeBase
from localdocmask.recognition.pipeline import RecognitionPipeline
from localdocmask.domain.policy import ReviewPolicy
from localdocmask.security.hashing import sha256_file

_RES = Path(__file__).resolve().parents[1] / "resources"
_MODEL_DIR = (Path(__file__).resolve().parents[1]
              / "models" / "ner_bert4ner-approved")
_PROVENANCE = _MODEL_DIR / "PROVENANCE.json"
_MODEL_ONNX = _MODEL_DIR / "model.onnx"
_HAS_REAL_MODEL = _MODEL_ONNX.exists() and _PROVENANCE.exists()


class _FakeNER:
    """可编程 NER 桩：验证接线与降级，不依赖真实模型。"""

    def __init__(self, pairs=None, exc=None):
        self._pairs = list(pairs or [])
        self._exc = exc
        self.calls: list[str] = []

    def extract_entities(self, text: str):
        self.calls.append(text)
        if self._exc:
            raise self._exc
        return list(self._pairs)


def _pipeline(ner=None) -> RecognitionPipeline:
    return RecognitionPipeline(ner=ner)


# ---------------- 接线：默认关闭 ----------------
def test_ner_none_by_default():
    p = _pipeline()
    assert p.ner is None
    out = p.analyze("手机13812345678，我们公司的产品已上线。")
    # 不启用 NER 时行为与基线一致：高精度自动确认，泛指被抑制
    assert any(c.category == "手机号" and c.decision == EntityDecision.AUTO_CONFIRM
               for c in out)


# ---------------- 接线：NER 输出仅作为候选 ----------------
def test_ner_supplement_adds_low_weight_candidate():
    fake = _FakeNER(pairs=[("姓名", "陈晓峰")])
    p = _pipeline(ner=fake)
    out = p.analyze("陈晓峰周一下午到访。")
    assert fake.calls, "NER 应被调用"
    hit = [c for c in out if c.text == "陈晓峰" and c.category == "姓名"]
    assert hit, "NER 补出的词典外人名应进入候选"
    assert "ner" in hit[0].source


def test_ner_exception_does_not_break_pipeline():
    fake = _FakeNER(exc=RuntimeError("boom: model file corrupt"))
    p = _pipeline(ner=fake)
    out = p.analyze("手机13812345678，客户事项。")  # 不得抛异常
    assert any(c.category == "手机号" for c in out)


def test_ner_no_duplicate_of_existing():
    fake = _FakeNER(pairs=[("公司名", "杭州云启科技有限公司")])
    p = _pipeline(ner=fake)
    out = p.analyze("供应商：杭州云启科技有限公司。")
    companies = [c for c in out if c.category == "公司名" and c.text == "杭州云启科技有限公司"]
    assert len(companies) == 1, "NER 不应与已有 (类别,文本) 重复造候选"


# ---------------- 批准模型：清单、完整性与健康状态 ----------------
def test_approved_model_provenance_and_file_hashes():
    provenance = json.loads(_PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["schema_version"] == 1
    assert provenance["model_id"] == "shibing624/bert4ner-base-chinese"
    assert provenance["source_url"] == (
        "https://huggingface.co/shibing624/bert4ner-base-chinese")
    assert provenance["source_commit"] == (
        "5d660ed2aa9da482bf2d99c6bc8cf2ce66758f6a")
    assert provenance["license"] == "Apache-2.0"
    assert provenance["distributable"] is True
    assert (_MODEL_DIR / provenance["license_file"]).is_file()
    assert sha256_file(_PROVENANCE) == (
        "62f46310390b4d6c979fe0e8234e66696bc77b38f0172cd3551c0d51258cdfca")
    assert sha256_file(_MODEL_DIR / "LICENSE") == (
        "1eb85fc97224598dad1852b5d6483bbcf0aa8608790dcc657a5a2a761ae9c8c6")
    assert sha256_file(_MODEL_DIR / "model.safetensors") == (
        "1d2f0ea479431c1907bbaa75f872c26267ce3c19ea78e4266980b0d49b5a825a")

    declared = {entry["path"]: entry["sha256"] for entry in provenance["files"]}
    for name in ("model.onnx", "model.onnx.data", "vocab.txt",
                 "id2label.json", "config.json"):
        assert name in declared
        assert sha256_file(_MODEL_DIR / name) == declared[name]


def _link_approved_model(tmp_path: Path, *, omit: set[str] | None = None) -> Path:
    target = tmp_path / "ner-model"
    target.mkdir()
    omitted = omit or set()
    for source in _MODEL_DIR.iterdir():
        if source.is_file() and source.name not in omitted:
            os.link(source, target / source.name)
    return target


@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="批准模型资源缺失")
def test_model_loader_requires_provenance(tmp_path):
    model_dir = _link_approved_model(tmp_path, omit={"PROVENANCE.json"})
    with pytest.raises(NerUnavailable, match="PROVENANCE|清单"):
        from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
        OnnxChineseNER(model_dir=model_dir)


@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="批准模型资源缺失")
def test_model_loader_rejects_tampered_file(tmp_path):
    model_dir = _link_approved_model(tmp_path)
    target = model_dir / "id2label.json"
    original = target.read_bytes()
    target.unlink()
    target.write_bytes(original + b" ")
    with pytest.raises(NerUnavailable, match="SHA-256|哈希"):
        from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
        OnnxChineseNER(model_dir=model_dir)


@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="批准模型资源缺失")
def test_model_loader_rejects_missing_external_data(tmp_path):
    model_dir = _link_approved_model(tmp_path, omit={"model.onnx.data"})
    with pytest.raises(NerUnavailable, match="model.onnx.data|缺失"):
        from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
        OnnxChineseNER(model_dir=model_dir)


@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="批准模型资源缺失")
def test_model_loader_rejects_non_distributable_manifest(tmp_path):
    model_dir = _link_approved_model(tmp_path)
    manifest_path = model_dir / "PROVENANCE.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["distributable"] = False
    manifest_path.unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(NerUnavailable, match="分发|distributable"):
        from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
        OnnxChineseNER(model_dir=model_dir)


def test_default_model_paths_for_source_and_bundle(monkeypatch, tmp_path):
    from localdocmask.infrastructure.ner import onnx_ner

    monkeypatch.delattr(onnx_ner.sys, "frozen", raising=False)
    monkeypatch.delattr(onnx_ner.sys, "_MEIPASS", raising=False)
    assert onnx_ner._default_model_dir() == _MODEL_DIR

    monkeypatch.setattr(onnx_ner.sys, "frozen", True, raising=False)
    monkeypatch.setattr(onnx_ner.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert onnx_ner._default_model_dir() == (
        tmp_path / "resources" / "ner_bert4ner")


def test_enabled_invalid_model_health_is_not_healthy(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path / "data"))
    settings = Settings()
    settings.ner.enabled = True
    settings.ner.model_dir = str(tmp_path / "missing-model")
    svc = build_services(settings)
    try:
        report = svc.health.check()
        assert report.status != "healthy"
        assert report.components["local_ner"]["status"] == "degraded"
        assert report.components["local_ner"]["reason"] == "ner_model_unavailable"
        assert svc.pipeline.recognition.ner is None
    finally:
        svc.close()


def test_disabled_ner_health_is_explicitly_degraded(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path / "data"))
    settings = Settings()
    settings.ner.enabled = False
    svc = build_services(settings)
    try:
        report = svc.health.check()
        assert report.status == "degraded"
        assert report.components["local_ner"]["status"] == "degraded"
        assert report.components["local_ner"]["reason"] == "ner_disabled"
    finally:
        svc.close()


def test_default_ner_is_enabled_for_approved_model():
    assert Settings().ner.enabled is True
    config = (Path(__file__).resolve().parents[1]
              / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert "ner:\n  enabled: true" in config


# ---------------- 融合：ner 与 ollama 同口径 ----------------
def _fusion():
    return FusionEngine(ReviewPolicy(), KnowledgeBase.load(_RES))


def test_fusion_ner_only_context_entity_needs_review():
    eng = _fusion()
    out = eng.fuse([Candidate(text="某不知名科技公司", category="公司名",
                              confidence=0.55, raw_confidence=0.55, source="ner")],
                   "某不知名科技公司出现")
    assert out[0].decision == EntityDecision.NEEDS_REVIEW
    assert out[0].excluded is False  # 转人工复核，既不自动脱敏也不放行


def test_fusion_ner_with_reliable_source_not_downgraded():
    eng = _fusion()
    # 黑名单(regex) + ner 共同命中公司：有可靠来源印证，不落入"仅模型"支路
    out = eng.fuse([
        Candidate(text="杭州云启科技有限公司", category="公司名",
                  confidence=0.95, raw_confidence=0.95, source="regex"),
        Candidate(text="杭州云启科技有限公司", category="公司名",
                  confidence=0.55, raw_confidence=0.55, source="ner"),
    ], "供应商杭州云启科技有限公司")
    assert out[0].decision == EntityDecision.AUTO_CONFIRM


def test_fusion_ner_high_precision_not_overridden():
    eng = _fusion()
    out = eng.fuse([Candidate(text="13812345678", category="手机号",
                              confidence=0.95, raw_confidence=0.95, source="ner")],
                   "手机13812345678")
    assert out[0].decision == EntityDecision.AUTO_CONFIRM


# ---------------- 真实模型（缺模型自动 skip） ----------------
@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="未安装真实 NER 模型（model.onnx 缺失）")
class TestRealOnnxNER:
    def _ner(self):
        from localdocmask.infrastructure.ner.onnx_ner import OnnxChineseNER
        return OnnxChineseNER(model_dir=_MODEL_DIR)

    def test_extract_entities_maps_categories(self):
        ner = self._ner()
        pairs = ner.extract_entities("张伟对接杭州云启科技有限公司提供服务")
        cats = {cat for cat, _ in pairs}
        assert cats <= {"姓名", "公司名", "详细地址"}, f"标签映射异常：{pairs}"

    def test_real_company_boundary_no_verb_swallow(self):
        """无标点连续动词串：公司名边界正确，不吞动词/前导成分。"""
        ner = self._ner()
        pairs = dict((cat, val) for cat, val in ner.extract_entities(
            "张伟对接杭州云启科技有限公司提供服务"))
        assert "公司名" in pairs, f"未识别到公司名：{pairs}"
        company = pairs["公司名"]
        assert company == "杭州云启科技有限公司", f"公司边界错误：{company!r}"
        for junk in ("对接", "提供服务", "张伟"):
            assert junk not in company

    def test_real_out_of_dict_person_recall(self):
        """词典外裸人名：开 NER 才能召回，关 NER 召回不到。"""
        ner = self._ner()
        text = "陈晓峰将于周五前往上海洽谈合作"
        on_pairs = ner.extract_entities(text)
        persons = [v for c, v in on_pairs if c == "姓名"]
        assert "陈晓峰" in persons, f"NER 未补出词典外人名：{on_pairs}"

    def test_real_person_recall_end_to_end(self):
        ner = self._ner()
        text = "经办人陈晓峰确认本周付款"
        off = _pipeline(ner=None).analyze(text)
        on = _pipeline(ner=ner).analyze(text)
        assert not any(c.text == "陈晓峰" for c in off), "基线（词典）不应认识陈晓峰"
        assert any(c.text == "陈晓峰" and c.category == "姓名" for c in on), \
            "开 NER 后应补出陈晓峰"

    def test_real_loc_recognized(self):
        ner = self._ner()
        pairs = ner.extract_entities("公司位于北京市朝阳区望京街8号")
        locs = [v for c, v in pairs if c == "详细地址"]
        assert locs and any("北京市" in v for v in locs), f"LOC 未识别：{pairs}"

    def test_real_loc_fragments_merged_into_one_address(self):
        """相邻 LOC 碎片粘合为单个完整地址 span，并吸收紧跟门牌号。"""
        ner = self._ner()
        pairs = ner.extract_entities("公司位于北京市朝阳区望京街8号")
        locs = [v for c, v in pairs if c == "详细地址"]
        assert len(locs) == 1, f"LOC 应粘合为单个 span，实际：{locs}"
        assert locs[0] == "北京市朝阳区望京街8号", f"粘合结果错误：{locs}"

    def test_real_loc_merge_does_not_cross_boundary(self):
        """粘合不跨越标点/句子边界，不吞公司名与人名。"""
        ner = self._ner()
        pairs = ner.extract_entities("张伟前往北京市朝阳区，与杭州云启科技有限公司洽谈")
        locs = [v for c, v in pairs if c == "详细地址"]
        orgs = [v for c, v in pairs if c == "公司名"]
        persons = [v for c, v in pairs if c == "姓名"]
        assert locs == ["北京市朝阳区"], f"地址不应跨逗号吞并后续：{locs}"
        assert "杭州云启科技有限公司" in orgs, f"公司名应独立保留：{pairs}"
        assert "张伟" in persons, f"人名应独立保留：{pairs}"


@pytest.mark.skipif(not _HAS_REAL_MODEL, reason="批准模型资源缺失")
def test_real_people_enter_review_then_redact_and_audit(tmp_path, monkeypatch):
    from docx import Document

    monkeypatch.setenv("LOCALDOCMASK_HOME", str(tmp_path / "data"))
    source = tmp_path / "普通姓名样例.docx"
    document = Document()
    document.add_paragraph("请联系赵启明处理这份合同。")
    document.add_paragraph("项目负责人：李思远，联系电话13812345678。")
    document.save(str(source))
    source_hash = sha256_file(source)

    settings = Settings()
    settings.ner.enabled = True
    settings.ner.model_dir = str(_MODEL_DIR)
    svc = build_services(settings)
    try:
        assert svc.health.check().components["local_ner"] == {
            "status": "healthy", "reason": "ner_model_loaded"}
        task = svc.pipeline.prepare(source)
        scan = svc.pipeline.scan(task)
        names = {candidate.text: candidate for candidate in scan.candidates
                 if candidate.category == "姓名"}
        assert {"赵启明", "李思远"} <= set(names)
        assert all(names[name].decision == EntityDecision.NEEDS_REVIEW
                   for name in ("赵启明", "李思远"))

        for name in ("赵启明", "李思远"):
            candidate = names[name]
            svc.pipeline.update_candidate_review(
                task, candidate.candidate_id, confirmed=True,
                replacement="****", reason_code="manual_confirm")

        svc.pipeline.process(task, "a", "****", task.scan.candidates)
        report = svc.pipeline.audit(task, task.scan.candidates)
        task = svc.pipeline.finalize(task, report)

        assert task.status == TaskStatus.PASSED, task.error
        assert task.audit is not None and task.audit.passed is True
        output = Document(str(task.output_path))
        output_text = "\n".join(paragraph.text for paragraph in output.paragraphs)
        assert "赵启明" not in output_text
        assert "李思远" not in output_text
        assert sha256_file(source) == source_hash
    finally:
        svc.close()
