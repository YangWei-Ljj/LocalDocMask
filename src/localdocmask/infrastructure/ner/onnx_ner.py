"""基于 onnxruntime 的本地中文 NER 适配器（BERT-CRF 风格 token-classification）。

模型：shibing624/bert4ner-base-chinese（人民日报 NER 微调，BIO 标签），
一次性导出为 ONNX 存于 ``models/ner_bert4ner/``。运行时仅依赖 onnxruntime +
numpy（均为现有栈），不 import torch / transformers。

标签映射（ADR-009）：
    B-/I-PER -> 姓名；B-/I-ORG -> 公司名；B-/I-LOC -> 详细地址；TIME/O 忽略。

失败关闭：构造时先校验 PROVENANCE.json、许可证字段和全部运行时文件哈希；
任何缺失、损坏或不匹配都抛出 NerUnavailable。推理异常同样显式抛出，由上层
识别门面决定是否继续规则路径，不再在适配器内部伪装成“无实体”。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

from .base import NerUnavailable

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEVELOPMENT_MODEL_DIR = _PROJECT_ROOT / "models" / "ner_bert4ner-approved"
_PROVENANCE_FILENAME = "PROVENANCE.json"
_REQUIRED_RUNTIME_FILES = frozenset({
    "model.onnx",
    "model.onnx.data",
    "vocab.txt",
    "id2label.json",
    "config.json",
})
_REQUIRED_PROVENANCE_FIELDS = (
    "schema_version",
    "model_id",
    "model_name",
    "model_version",
    "source_url",
    "source_commit",
    "license",
    "license_file",
    "distributable",
    "files",
)


def _default_model_dir() -> Path:
    """源码使用批准的模型目录；冻结包使用 PyInstaller bundle 资源。"""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root) / "resources" / "ner_bert4ner"
    return _DEVELOPMENT_MODEL_DIR


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

# 模型标签 -> 内部类别
_LABEL2CAT = {
    "PER": "姓名",
    "ORG": "公司名",
    "LOC": "详细地址",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# ORG 边界保守归一（仅去除、绝不发明）：
# BERT-NER 在无标点连续文本下偶尔把前导通用动词吞进公司名（如
# "对接星轨数据服务（示例）有限公司"）。仅当剥离前缀后剩余串仍以公司
# 后缀结尾时才剥离前导动词/主语，避免误伤真实字号。
_ORG_SUFFIX = re.compile(
    r"(有限公司|股份有限公司|集团|公司|事务所|研究院|研究中心|银行|医院|大学)$")
_ORG_LEADING_STRIPS = (
    "对接", "聘请", "签约", "合作", "采购", "销售", "投资", "控股",
    "联系", "拜访", "考察", "我司", "我方", "甲方", "乙方",
)

# LOC 粘合：两段相邻 LOC 片段之间仅允许出现空白与标点（不跨动词/ORG/PER/句子）。
_LOC_GAP_RE = re.compile(r"[\s，。、；：,.;:（）()]*")
# 紧跟地址末尾的门牌号/楼栋号：数字 + 单位（8号 / 12室 / 3栋 / 5层 / 2号楼…）
_HOUSE_NO_RE = re.compile(r"\d+(号|室|栋|幢|座|层|楼|弄|单元)")


def _fix_org_span(val: str) -> str:
    if not _ORG_SUFFIX.search(val):
        return val
    cur = val
    while True:
        nxt = cur
        for v in _ORG_LEADING_STRIPS:
            if cur.startswith(v) and len(cur) - len(v) >= 4:
                cand = cur[len(v):]
                if _ORG_SUFFIX.search(cand):
                    nxt = cand
                    break
        if nxt == cur:
            return cur
        cur = nxt


class OnnxChineseNER:
    """onnxruntime 中文 NER。线程安全说明：InferenceSession 本身线程可重复 run。"""

    def __init__(self, model_dir: str | Path | None = None,
                 device: str = "cpu", max_length: int = 256):
        self.model_dir = Path(model_dir) if model_dir else _default_model_dir()
        self.max_length = int(max_length)
        self._session = None
        self._validate_provenance()

        try:
            import onnxruntime as ort  # noqa: F401
        except Exception as e:  # pragma: no cover - 环境应已含 onnxruntime
            raise NerUnavailable(f"缺少 onnxruntime，NER 不可用：{e}") from e

        onnx_path = self.model_dir / "model.onnx"
        vocab_path = self.model_dir / "vocab.txt"
        id2label_path = self.model_dir / "id2label.json"
        try:
            self._vocab = self._load_vocab(vocab_path)
        except Exception as e:
            raise NerUnavailable(f"NER 词表读取失败：{e}") from e
        self._cls_id = self._vocab.get("[CLS]", 101)
        self._sep_id = self._vocab.get("[SEP]", 102)
        self._pad_id = self._vocab.get("[PAD]", 0)
        self._unk_id = self._vocab.get("[UNK]", 100)
        try:
            with open(id2label_path, encoding="utf-8") as f:
                self._id2label = {int(k): v for k, v in json.load(f).items()}
        except Exception as e:
            raise NerUnavailable(f"NER 标签映射格式错误：{e}") from e

        providers = ["CPUExecutionProvider"]
        if device and device.lower().startswith("cuda"):
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        try:
            self._session = ort.InferenceSession(
                str(onnx_path), providers=providers)
        except Exception as e:
            raise NerUnavailable(f"ONNX 推理会话初始化失败：{e}") from e

    def _validate_provenance(self) -> None:
        """校验批准清单、许可证元数据和运行时文件 SHA-256。"""
        if not self.model_dir.is_dir():
            raise NerUnavailable(f"NER 模型目录缺失：{self.model_dir}")
        manifest_path = self.model_dir / _PROVENANCE_FILENAME
        if not manifest_path.is_file():
            raise NerUnavailable(f"NER 模型清单缺失：{manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise NerUnavailable(f"NER 模型清单无法解析：{e}") from e
        if not isinstance(manifest, dict):
            raise NerUnavailable("NER 模型清单根节点必须是对象")
        missing = [name for name in _REQUIRED_PROVENANCE_FIELDS
                   if name not in manifest]
        if missing:
            raise NerUnavailable(
                f"NER 模型清单字段缺失：{', '.join(missing)}")
        try:
            schema_version = int(manifest.get("schema_version", -1))
        except (TypeError, ValueError) as e:
            raise NerUnavailable("NER 模型清单 schema_version 无效") from e
        if schema_version != 1:
            raise NerUnavailable("NER 模型清单 schema_version 不受支持")
        for name in ("model_id", "model_name", "model_version", "source_url",
                     "source_commit", "license", "license_file"):
            value = manifest.get(name)
            if not isinstance(value, str) or not value.strip():
                raise NerUnavailable(f"NER 模型清单字段无效：{name}")
        if manifest.get("distributable") is not True:
            raise NerUnavailable("NER 模型未获准分发：distributable != true")
        license_rel = Path(str(manifest["license_file"]))
        if license_rel.is_absolute() or ".." in license_rel.parts:
            raise NerUnavailable("NER 模型许可证文件路径越界")
        license_path = self.model_dir / license_rel
        if not license_path.is_file():
            raise NerUnavailable(f"NER 模型许可证文件缺失：{license_path}")

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise NerUnavailable("NER 模型清单 files 必须是非空数组")
        declared: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                raise NerUnavailable("NER 模型清单 files 条目格式错误")
            rel_text = item.get("path")
            expected = item.get("sha256")
            if not isinstance(rel_text, str) or not rel_text.strip():
                raise NerUnavailable("NER 模型清单文件路径缺失")
            rel = Path(rel_text)
            if rel.is_absolute() or ".." in rel.parts:
                raise NerUnavailable(f"NER 模型清单路径越界：{rel_text}")
            path = self.model_dir / rel
            if not path.is_file():
                raise NerUnavailable(f"NER 模型文件缺失：{rel_text}")
            if not isinstance(expected, str) or not re.fullmatch(
                    r"[0-9a-fA-F]{64}", expected):
                raise NerUnavailable(f"NER 模型文件哈希无效：{rel_text}")
            actual = _sha256_file(path)
            if actual.lower() != expected.lower():
                raise NerUnavailable(
                    f"NER 模型文件 SHA-256 不匹配：{rel_text}")
            declared.add(rel.as_posix())
        missing_runtime = sorted(_REQUIRED_RUNTIME_FILES - declared)
        if missing_runtime:
            raise NerUnavailable(
                f"NER 模型清单缺少必需运行时文件：{', '.join(missing_runtime)}")

    # ---------- 分词（手写 BERT 字符级，避免 tokenizers 依赖）----------
    @staticmethod
    def _load_vocab(path: Path) -> dict[str, int]:
        vocab: dict[str, int] = {}
        with open(path, encoding="utf-8") as f:
            for idx, line in enumerate(f):
                tok = line.rstrip("\n")
                if tok:
                    vocab[tok] = idx
        return vocab

    def _encode(self, text: str):
        """把文本编码为 BERT 输入，并保留 token->原字符下标的对齐。

        返回 input_ids, attention_mask, token_type_ids, char_align（每个 token
        对应的原字符下标；特殊符号 token 对齐为 -1）。
        """
        ids = [self._cls_id]
        align = [-1]
        for i, ch in enumerate(text):
            if ch.isspace():
                continue  # BERT 中文分词忽略空白
            tid = self._vocab.get(ch, self._unk_id)
            ids.append(tid)
            align.append(i)
        ids.append(self._sep_id)
        align.append(-1)
        return ids, align

    # ---------- 推理与 BIO 解码 ----------
    def extract_entities(self, text: str) -> list[tuple[str, str]]:
        if not text or not text.strip():
            return []
        results: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            # 分块处理长文本，块长略小于 max_length（留 [CLS]/[SEP]）
            chunk = max(32, self.max_length - 2)
            for start in range(0, len(text), chunk):
                seg = text[start:start + chunk]
                for cat, val in self._predict_segment(seg, offset=start):
                    key = (cat, val)
                    if val and key not in seen:
                        seen.add(key)
                        results.append(key)
        except Exception as e:
            raise NerUnavailable(f"NER 推理失败：{type(e).__name__}") from e
        return results

    def _predict_segment(self, seg: str, offset: int) -> list[tuple[str, str]]:
        ids, align = self._encode(seg)
        if len(ids) > self.max_length:
            ids = ids[:self.max_length - 1] + [self._sep_id]
            align = align[:self.max_length - 1] + [-1]
        n = len(ids)
        input_ids = np.array([ids], dtype=np.int64)
        attn = np.array([[1] * n], dtype=np.int64)
        type_ids = np.zeros((1, n), dtype=np.int64)

        out = self._session.run(
            ["logits"],
            {"input_ids": input_ids, "attention_mask": attn,
             "token_type_ids": type_ids},
        )
        logits = out[0][0]  # [seq, num_labels]
        pred = logits.argmax(axis=-1)

        # 先按 BIO 收集带位置的原始片段：{cat, start, end(exclusive), span}
        raw: list[dict] = []
        cur_cat: str | None = None
        cur_label: str | None = None
        span: list[int] = []

        def _flush():
            if cur_cat is not None and span:
                raw.append({"cat": cur_cat, "start": span[0],
                            "end": span[-1] + 1, "span": list(span)})

        for pos, label_id in enumerate(pred):
            label = self._id2label.get(int(label_id), "O")
            if label == "O" or label.startswith("B-"):
                _flush()
                cur_cat, cur_label, span = None, None, []
                if label.startswith("B-"):
                    ne = label[2:]
                    cat = _LABEL2CAT.get(ne)
                    if cat:
                        cur_cat, cur_label, span = cat, ne, [align[pos]]
            else:  # I-XXX
                ne = label[2:]
                cat = _LABEL2CAT.get(ne)
                if cat and ne == cur_label and align[pos] >= 0:
                    span.append(align[pos])
                # I 与起始类别不一致：丢弃该片段（不跨类粘合）
        _flush()

        return self._finalize(seg, raw)

    # ---------- 后处理：ORG 边界剥离 + LOC 碎片粘合 ----------
    @staticmethod
    def _finalize(seg: str, raw: list[dict]) -> list[tuple[str, str]]:
        locs = sorted((r for r in raw if r["cat"] == "详细地址"),
                      key=lambda r: r["start"])
        others = [r for r in raw if r["cat"] != "详细地址"]

        # 非 LOC：沿用原字符拼接（跳过分词时忽略的空白），ORG 做前导剥离
        out: list[tuple[str, str]] = []
        for r in others:
            txt = "".join(seg[i] for i in r["span"])
            if r["cat"] == "公司名":
                txt = _fix_org_span(txt)
            if txt:
                out.append((r["cat"], txt))

        # LOC 粘合：合并紧邻（间隔仅空白/标点）的 LOC 片段，再吸收紧跟门牌号
        merged: list[dict] = []
        i = 0
        while i < len(locs):
            cur = {"start": locs[i]["start"], "end": locs[i]["end"]}
            j = i + 1
            while j < len(locs):
                nxt = locs[j]
                gap = seg[cur["end"]:nxt["start"]]
                if _LOC_GAP_RE.fullmatch(gap):
                    cur["end"] = nxt["end"]
                    j += 1
                else:
                    break
            # 向后吸收紧跟的门牌号/楼栋号（仅一次，保守）
            tail = _HOUSE_NO_RE.match(seg, cur["end"])
            if tail:
                cur["end"] = tail.end()
            merged.append(cur)
            i = j

        for m in merged:
            txt = seg[m["start"]:m["end"]].strip()
            if txt:
                out.append(("详细地址", txt))
        return out
