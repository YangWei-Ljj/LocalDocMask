# LocalDocMask 模块契约与接口规范

> 版本：v1.0 日期：2026-10-26
> 用途：固定各模块职责、端口（Protocol）、关键数据结构与字段语义，作为并行重构的统一契约。签名为目标约束，实现可补充私有辅助，但不得破坏契约。

## 1. domain（领域层）

### 1.1 enums.py

```python
class TaskStatus(str, Enum):
    PENDING / PROCESSING / NEEDS_REVIEW / PROCESSED_UNAUDITED /
    AUDIT_FAILED / UNSAFE / PASSED / INTERRUPTED

class ItemStatus(str, Enum):
    DETECTED / REDACTED / AUDIT_PASSED / AUDIT_FAILED /
    DELETION_PENDING / DELETED / DELETION_VERIFIED / FAILED / UNRESOLVED

class AuditVerdict(str, Enum):
    RESIDUAL_FOUND / NO_RESIDUAL / UNCHECKABLE / INSUFFICIENT / NEEDS_CONFIRM

class RiskLevel(str, Enum):
    LOW = "low"; MEDIUM = "medium"; HIGH = "high"; CRITICAL = "critical"

class EntityDecision(str, Enum):
    AUTO_CONFIRM = "auto_confirm"
    NEEDS_REVIEW = "needs_review"
    SUPPRESS = "suppress"
    CONFIRMED = "confirmed"      # 用户确认
    DISMISSED = "dismissed"      # 用户放行
```

### 1.2 entities.py

```python
@dataclass
class Candidate:
    text: str
    category: str
    location: str = ""
    confidence: float = 0.0          # 校准后置信度
    raw_confidence: float = 0.0      # 原始置信度
    source: str = "regex"            # regex/dict/ollama/blacklist/whitelist/public
    risk_level: RiskLevel = RiskLevel.MEDIUM
    decision: EntityDecision = EntityDecision.NEEDS_REVIEW
    decision_reason: str = ""
    confirmed: bool = False
    excluded: bool = False
    replacement: str = ""
    page: int = -1
    box: list | None = None
    image_part: str = ""
    status: str = ItemStatus.DETECTED.value
    failure_reason: str = ""
    deletion_verified: bool = False
    delete: bool = False

@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    text: str = ""
    warnings: list[str] = field(default_factory=list)

@dataclass
class AuditFinding:
    verdict: AuditVerdict
    detail: str
    scope: str = ""                  # text/ocr/metadata/hidden
@dataclass
class AuditReport:
    passed: bool = False
    findings: list[AuditFinding] = field(default_factory=list)
    def summary(self) -> str: ...

@dataclass
class Task:
    source_path: Path
    task_id: str = ""
    file_type: str = ""
    status: TaskStatus = TaskStatus.PENDING
    source_hash: str = ""
    output_path: Path | None = None
    final_hash: str = ""
    error: str = ""
    scan: ScanResult | None = None
    audit: AuditReport | None = None
    report_path: Path | None = None
    records: list[dict] = field(default_factory=list)
    recognized_count: int = 0
    redacted_count: int = 0
    deleted_count: int = 0
```

### 1.3 exceptions.py

```python
class ErrorCode(str, Enum):
    FILE_NOT_FOUND / UNSUPPORTED_FORMAT / PARSE_ERROR / OCR_ERROR /
    LLM_UNAVAILABLE / LLM_TIMEOUT / RESIDUAL_FOUND / UNCHECKABLE /
    MAPPING_COLLISION / CONFIG_INVALID / PERSISTENCE_ERROR /
    TASK_CANCELLED / SECURITY_VIOLATION

class LocalDocMaskError(RuntimeError):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.SECURITY_VIOLATION): ...
class ParseError(LocalDocMaskError): ...
class LLMError(LocalDocMaskError): ...
class SecurityViolation(LocalDocMaskError): ...
class UncheckableError(LocalDocMaskError): ...
class MappingCollisionError(LocalDocMaskError): ...
class ConfigError(LocalDocMaskError): ...
```

### 1.4 policy.py（纯逻辑）

```python
@dataclass
class ReviewPolicy:
    review_mode: str = "interactive"   # interactive / auto_safe
    auto_confirm_threshold: float = 0.9
    review_threshold: float = 0.5
    def decide(self, category, sources, confidence, knowledge_hits) -> EntityDecision: ...
def risk_for(category: str, confidence: float, context: dict) -> RiskLevel: ...
```

## 2. recognition（识别层）

```python
# rule_based.py
class RuleDetector:
    def detect(self, text: str, location: str = "") -> list[Candidate]: ...

# context.py
@dataclass
class ContextFeatures:
    is_generic_reference: bool
    preceding: str
    proper_chars: int
    context_signals: list[str]
class ContextAnalyzer:
    def analyze(self, text: str, match_span: tuple[int,int], category: str) -> ContextFeatures: ...

# knowledge.py
@dataclass
class KnowledgeBase:
    whitelist: set[str]; blacklist: set[str]
    public_entities: set[str]; domain_terms: set[str]
    @classmethod
    def load(cls, resources_dir: Path) -> "KnowledgeBase": ...
    def classify(self, text: str) -> list[str]: ...   # 命中的词库标签

# fusion.py
class FusionEngine:
    def __init__(self, policy: ReviewPolicy, knowledge: KnowledgeBase): ...
    def fuse(self, candidates: list[Candidate], text: str) -> list[Candidate]:
        """合并、仲裁、校准、三档分流，输出最终候选。"""
```

## 3. application（应用层）

### 3.1 ports.py

```python
class ContentHandler(Protocol):
    name: str
    def extract(self, path: str) -> ScanResult: ...
    def process(self, path: str, out_path: str, candidates: list) -> list[str]: ...

class LLMClient(Protocol):
    def is_available(self) -> bool: ...
    def extract_entities(self, text: str) -> list[tuple[str,str]]: ...

class OCREngine(Protocol):
    def ocr(self, image) -> list[dict]: ...   # [{"text","confidence","box"}]

class AuditorPort(Protocol):
    def audit(self, out_path: str, forbidden_terms: list[str]) -> AuditReport: ...
    def residual_terms(self, out_path: str, terms: list[str]) -> tuple[list[str],list[str]]: ...

class TaskRepository(Protocol):
    def save(self, task: Task) -> None: ...
    def load(self, task_id: str) -> Task | None: ...
    def list_open(self) -> list[Task]: ...

class MapperPort(Protocol):
    def replacement(self, category: str, original: str) -> str: ...
```

### 3.2 pipeline.py

```python
class PipelineService:
    def __init__(self, handler_factory, recognizer: RecognitionPipeline,
                 auditor: AuditorPort, mapper_factory, repository: TaskRepository,
                 config: Settings): ...
    def prepare(self, source) -> Task: ...
    def scan(self, task: Task) -> ScanResult: ...
    def process(self, task: Task, mode: str, mask: str, candidates: list) -> Path: ...
    def finalize(self, task: Task, report: AuditReport) -> Task: ...
```

### 3.3 batch.py

```python
class BatchService:
    def __init__(self, pipeline: PipelineService, config: Settings): ...
    def run(self, sources: list, mode: str, mask: str,
            progress_cb=None, cancel_token=None) -> BatchResult:
        """批次共享 Mapper；逐文件处理；失败隔离；返回成功/待复核/失败分组。"""
```

## 4. infrastructure（基础设施层）

```python
# config/settings.py
@dataclass
class Settings:
    paths: PathsConfig; ollama: OllamaConfig; recognition: RecognitionConfig
    masking: MaskingConfig; security: SecurityConfig; logging: LoggingConfig
    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings": ...

# logging/logger.py
def configure_logging(cfg: LoggingConfig) -> None: ...
def get_logger(name: str) -> logging.Logger: ...

# persistence/sqlite_repo.py
class SqliteTaskRepository(TaskRepository): ...
def initialize_database(db_path: Path) -> None: ...

# ocr/engine.py
class RapidOCREngine: ...   # 单例，返回含 confidence

# llm/ollama.py
class OllamaClient: ...     # 回环校验、禁代理、候选 only

# formats/registry.py
def supported_extensions() -> list[str]: ...
def handler_for(path, detector) -> ContentHandler: ...
```

## 5. security（安全层）

```python
# hashing.py
def sha256_file(path) -> str: ...
# network.py
LOOPBACK_HOSTS = {...}
def assert_loopback(url: str) -> None: ...
# tempfiles.py
class TempFileManager:
    def new_path(self, suffix: str) -> Path: ...
    def cleanup(self) -> None: ...
    def verify_all_cleaned(self) -> bool: ...
# auditor.py
class IndependentAuditor(AuditorPort): ...
# redaction/masker.py
class Masker:
    def __init__(self, mode: str, mask: str, mapper: MapperPort | None = None): ...
    def replacement(self, category, original) -> str: ...
def mask_display(value, category="") -> str: ...
# redaction/mapping.py
class ConsistencyMapper(MapperPort):
    def __init__(self, scope="batch", persistence="memory", store_dir: Path | None = None): ...
```

## 6. presentation（表现层）

```python
# gui/main_window.py
class MainWindow(QMainWindow): ...
# gui/workers.py
class WorkerSignals(QObject):
    progress = object; finished = object; error = object
class TaskWorker(QRunnable): ...
```

## 7. evaluation（评估层）

```python
# dataset.py
@dataclass
class LabeledCase:
    text: str
    true_entities: list[tuple[str,str]]   # (类别, 文本)
    file_type: str = "text"
class Dataset:
    @classmethod
    def load(cls, path: Path) -> "Dataset": ...
# metrics.py
def prf1(tp,fp,fn) -> dict: ...
def evaluate(recognizer, dataset) -> EvalReport: ...
# runner.py
class EvaluationRunner: ...
```

## 8. 字段与命名约定

- 类别（category）统一取值：`身份证号/手机号/固定电话/邮箱/护照号/银行卡号/详细地址/编号/公司名/姓名/自定义敏感词`。
- 来源（source）：`regex/dict/ollama/blacklist/whitelist/public/domain`。
- 时间统一 ISO 8601；路径对外用字符串、内部用 Path；哈希为小写十六进制 SHA-256。
- 不向日志/报告写入完整原文或完整映射。
