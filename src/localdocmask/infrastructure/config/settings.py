"""应用配置：dataclass 结构 + YAML 加载 + 类型/范围校验（ADR-006）。

加载顺序（后者覆盖前者）：
1. 安全默认值；
2. config/config.yaml（若存在；该文件被 gitignore，仅本机使用）；
3. 环境变量：LOCALDOCMASK_HOME 覆盖数据目录（保持既有行为，便于测试隔离）。

安全默认：Ollama 仅回环且默认不启用；默认不开放外部网络；失败关闭；
一致性映射默认仅内存不落盘。非法值（非回环 Ollama 地址、置信度越界等）
显式抛出 ConfigError；缺失字段回退安全默认。

兼容层：模块级 DATA_DIR / SUCCESS_DIR / ensure_dirs 等符号仍由默认 Settings
派生，行为与既有硬编码版本一致。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.exceptions import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
CONFIG_SCHEMA_VERSION = 1
LAST_GOOD_CONFIG_FILENAME = "config_last_good.json"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ---------- 子配置 ----------
@dataclass
class PathsConfig:
    data_dir: str | None = None  # None => LOCALDOCMASK_HOME 或 <项目根>/data


@dataclass
class OllamaConfig:
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    enabled: bool = False
    timeout: int = 300


@dataclass
class NerConfig:
    """本地中文 NER（ADR-009）。默认启用随包批准模型；加载失败时组合根
    保留规则路径并标记 degraded。仅 CPU 本地推理，无网络调用。"""
    enabled: bool = True
    #: 模型目录；留空 => 开发环境 approved 目录 / 打包环境 bundle 资源
    model_dir: str = ""
    device: str = "cpu"          # cpu / cuda（无 GPU 时 onnxruntime 自动回退 CPU）
    max_length: int = 256         # 单块最大 token 长度（含 [CLS]/[SEP]）


@dataclass
class RecognitionConfig:
    enable_company_regex: bool = True
    auto_confirm_threshold: float = 0.9
    review_threshold: float = 0.5
    review_mode: str = "interactive"  # interactive / auto_safe


@dataclass
class MaskingConfig:
    mode: str = "a"                  # a=统一掩码 / b=一致性虚构替换
    mask: str = "****"
    mapping_scope: str = "batch"
    mapping_persistence: str = "memory"  # 当前仅支持 memory；disk 会拒绝启动


@dataclass
class SecurityConfig:
    allowed_networks: list[str] = field(
        default_factory=lambda: ["127.0.0.1/8", "::1/128"])
    tempfile_retention_days: float = 1.0
    #: GUI 工作线程与批处理共享的并发上限。
    max_workers: int = 2
    #: 单文件大小上限（MB）；超过即入口拒绝，不进入解析链路。
    max_file_size_mb: float = 50.0
    #: 单任务总预算（秒）。
    task_timeout_seconds: float = 1800.0
    #: T5 分阶段预算（秒）；默认值保持既有正常任务兼容。
    admission_timeout_seconds: float = 60.0
    scan_timeout_seconds: float = 300.0
    process_timeout_seconds: float = 900.0
    audit_timeout_seconds: float = 300.0
    finalize_timeout_seconds: float = 120.0
    #: 全局队列深度（含处理中与等待中的已接受文件）。
    max_queue_depth: int = 32
    #: 有限重试：默认最多 2 次尝试。
    retry_max_attempts: int = 2
    retry_base_delay_seconds: float = 0.1
    retry_max_delay_seconds: float = 1.0
    # ---- T4 文件准入资源上限 ----
    max_pdf_pages: int = 500
    max_image_pixels: int = 100_000_000
    max_zip_entries: int = 2000
    max_zip_uncompressed_mb: float = 500.0
    max_zip_compression_ratio: float = 100.0
    max_zip_entry_mb: float = 200.0


@dataclass
class ObservabilityConfig:
    metrics_enabled: bool = True
    metrics_max_bytes: int = 10 * 1024 * 1024
    diagnostics_log_lines: int = 2000


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"
    log_dir: str | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 30
    retention_days: int = 14


@dataclass(frozen=True)
class ConfigState:
    schema_version: int
    config_hash: str
    source: str
    degraded: bool = False
    warning_code: str = ""
    saved_at: str = ""


@dataclass
class ConfigLoadResult:
    settings: "Settings"
    state: ConfigState


@dataclass
class Settings:
    schema_version: int = CONFIG_SCHEMA_VERSION
    paths: PathsConfig = field(default_factory=PathsConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    ner: NerConfig = field(default_factory=NerConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    masking: MaskingConfig = field(default_factory=MaskingConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        return cls.load_with_metadata(config_path).settings

    @classmethod
    def load_with_metadata(cls, config_path: str | Path | None = None) -> ConfigLoadResult:
        data: dict[str, Any] = {}
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        candidate_error: Exception | None = None
        try:
            if path.exists():
                try:
                    import yaml
                except ImportError as e:  # pragma: no cover - 环境应含 pyyaml
                    raise ConfigError(f"缺少 pyyaml 依赖，无法加载配置：{e}") from e
                try:
                    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                    elif loaded is not None:
                        raise ConfigError("配置文件根节点必须是对象")
                except Exception as e:
                    raise ConfigError(f"配置文件 {path} 解析失败：{e}") from e
            allowed_sections = {
                "schema_version", "paths", "ollama", "ner", "recognition",
                "masking", "security", "logging", "observability",
            }
            unknown_sections = sorted(set(data) - allowed_sections)
            if unknown_sections:
                raise ConfigError(
                    f"配置包含未知顶层字段：{', '.join(unknown_sections)}")
            settings = _settings_from_mapping(data)
            settings._validate()
            config_hash = compute_config_hash(settings)
            data_dir = _resolve_data_dir(settings.paths)
            source = "file" if path.exists() else "defaults"
            state = ConfigState(
                schema_version=settings.schema_version,
                config_hash=config_hash,
                source=source,
                saved_at=_now_utc(),
            )
            try:
                _write_last_good(data_dir, settings, state)
            except Exception as exc:
                state = ConfigState(
                    schema_version=settings.schema_version,
                    config_hash=config_hash,
                    source=source,
                    degraded=True,
                    warning_code="last_good_write_failed",
                    saved_at=_now_utc(),
                )
            return ConfigLoadResult(settings=settings, state=state)
        except Exception as exc:
            candidate_error = exc

        data_dir = _fallback_data_dir()
        try:
            settings, state = _load_last_good(data_dir)
        except Exception as fallback_exc:
            raise ConfigError(
                "配置无效且最近可用配置不可用："
                f"{type(candidate_error).__name__}"
            ) from fallback_exc
        return ConfigLoadResult(
            settings=settings,
            state=ConfigState(
                schema_version=state.schema_version,
                config_hash=state.config_hash,
                source="last_good",
                degraded=True,
                warning_code="config_fallback",
                saved_at=state.saved_at,
            ),
        )

    def _validate(self) -> None:
        from urllib.parse import urlparse
        if int(self.schema_version) != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"不支持的配置 schema_version：{self.schema_version}")
        host = (urlparse(self.ollama.url).hostname or "").lower()
        if host not in _LOOPBACK_HOSTS:
            raise ConfigError(
                f"Ollama 地址必须为本机回环，拒绝：{self.ollama.url}")
        for name in ("auto_confirm_threshold", "review_threshold"):
            v = getattr(self.recognition, name)
            if not 0.0 <= float(v) <= 1.0:
                raise ConfigError(f"recognition.{name} 须在 0~1 之间：{v}")
        if self.recognition.review_mode not in ("interactive", "auto_safe"):
            raise ConfigError(f"未知 review_mode：{self.recognition.review_mode}")
        if self.masking.mode not in ("a", "b"):
            raise ConfigError(f"未知 masking.mode：{self.masking.mode}")
        if self.masking.mapping_persistence != "memory":
            raise ConfigError(
                "mapping_persistence 当前仅支持 memory；"
                "disk/protected_file 明文映射已禁用，"
                "需单独批准 DPAPI 加密方案后才能恢复"
            )
        if self.ollama.timeout < 1 or self.ollama.timeout > 3600:
            raise ConfigError(f"ollama.timeout 须在 1~3600 秒：{self.ollama.timeout}")
        if self.ner.device.lower() not in ("cpu", "cuda"):
            raise ConfigError(f"ner.device 仅支持 cpu/cuda：{self.ner.device}")
        if not (64 <= self.ner.max_length <= 512):
            raise ConfigError(f"ner.max_length 须在 64~512：{self.ner.max_length}")
        # 资源上限范围校验：非法配置即拒绝启动（失败关闭）
        if not (1 <= self.security.max_workers <= 64):
            raise ConfigError(f"security.max_workers 须在 1~64：{self.security.max_workers}")
        if not (1.0 <= float(self.security.max_file_size_mb) <= 2048.0):
            raise ConfigError(
                f"security.max_file_size_mb 须在 1~2048 MB：{self.security.max_file_size_mb}")
        if not (30 <= float(self.security.task_timeout_seconds) <= 86400):
            raise ConfigError(
                f"security.task_timeout_seconds 须在 30~86400 秒：{self.security.task_timeout_seconds}")
        for name in (
            "admission_timeout_seconds", "scan_timeout_seconds",
            "process_timeout_seconds", "audit_timeout_seconds",
            "finalize_timeout_seconds",
        ):
            value = float(getattr(self.security, name))
            if not (1.0 <= value <= 86400):
                raise ConfigError(f"security.{name} 须在 1~86400 秒：{value}")
        if not (1 <= int(self.security.max_queue_depth) <= 1000):
            raise ConfigError(
                f"security.max_queue_depth 须在 1~1000：{self.security.max_queue_depth}")
        if not (1 <= int(self.security.retry_max_attempts) <= 3):
            raise ConfigError(
                f"security.retry_max_attempts 须在 1~3：{self.security.retry_max_attempts}")
        if not (0.0 <= float(self.security.retry_base_delay_seconds) <= 60):
            raise ConfigError(
                "security.retry_base_delay_seconds 须在 0~60："
                f"{self.security.retry_base_delay_seconds}")
        if not (0.0 <= float(self.security.retry_max_delay_seconds) <= 60):
            raise ConfigError(
                "security.retry_max_delay_seconds 须在 0~60："
                f"{self.security.retry_max_delay_seconds}")
        if not (0.000001 <= float(self.security.tempfile_retention_days) <= 365):
            raise ConfigError(
                "security.tempfile_retention_days 须在 0.000001~365："
                f"{self.security.tempfile_retention_days}")
        if not (1 <= int(self.observability.metrics_max_bytes) <= 1_000_000_000):
            raise ConfigError(
                "observability.metrics_max_bytes 须在 1~1000000000")
        if not (1 <= int(self.observability.diagnostics_log_lines) <= 100000):
            raise ConfigError(
                "observability.diagnostics_log_lines 须在 1~100000")
        if not (1 <= self.security.max_pdf_pages <= 100000):
            raise ConfigError(f"security.max_pdf_pages 须在 1~100000：{self.security.max_pdf_pages}")
        if not (1 <= self.security.max_image_pixels <= 2_000_000_000):
            raise ConfigError(f"security.max_image_pixels 须在 1~2000000000：{self.security.max_image_pixels}")
        if not (1 <= self.security.max_zip_entries <= 100000):
            raise ConfigError(f"security.max_zip_entries 须在 1~100000：{self.security.max_zip_entries}")
        if not (0.1 <= float(self.security.max_zip_uncompressed_mb) <= 10240):
            raise ConfigError(f"security.max_zip_uncompressed_mb 须在 0.1~10240：{self.security.max_zip_uncompressed_mb}")
        if not (1.0 <= float(self.security.max_zip_compression_ratio) <= 10000):
            raise ConfigError(f"security.max_zip_compression_ratio 须在 1~10000：{self.security.max_zip_compression_ratio}")
        if not (0.1 <= float(self.security.max_zip_entry_mb) <= 10240):
            raise ConfigError(f"security.max_zip_entry_mb 须在 0.1~10240：{self.security.max_zip_entry_mb}")
        if self.logging.level.upper() not in (
                "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"未知日志级别：{self.logging.level}")


# ---------- 配置序列化、哈希与最近可用配置 ----------
def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings_from_mapping(data: dict[str, Any]) -> Settings:
    return Settings(
        schema_version=int(data.get("schema_version", CONFIG_SCHEMA_VERSION)),
        paths=PathsConfig(**(data.get("paths") or {})),
        ollama=OllamaConfig(**(data.get("ollama") or {})),
        ner=NerConfig(**(data.get("ner") or {})),
        recognition=RecognitionConfig(**(data.get("recognition") or {})),
        masking=MaskingConfig(**(data.get("masking") or {})),
        security=SecurityConfig(**(data.get("security") or {})),
        logging=LoggingConfig(**(data.get("logging") or {})),
        observability=ObservabilityConfig(**(data.get("observability") or {})),
    )


def compute_config_hash(settings: Settings | None = None) -> str:
    """确定性配置哈希；不包含时间、路径、日志、指标或任何秘密。"""
    s = settings or Settings()
    ner_model = Path(s.ner.model_dir).name if s.ner.model_dir else "default"
    security = {
        name: getattr(s.security, name)
        for name in (
            "allowed_networks", "tempfile_retention_days", "max_workers",
            "max_file_size_mb", "task_timeout_seconds",
            "admission_timeout_seconds", "scan_timeout_seconds",
            "process_timeout_seconds", "audit_timeout_seconds",
            "finalize_timeout_seconds", "max_queue_depth",
            "retry_max_attempts", "retry_base_delay_seconds",
            "retry_max_delay_seconds", "max_pdf_pages", "max_image_pixels",
            "max_zip_entries", "max_zip_uncompressed_mb",
            "max_zip_compression_ratio", "max_zip_entry_mb",
        )
    }
    security["allowed_networks"] = sorted(security["allowed_networks"])
    payload = {
        "schema_version": int(s.schema_version),
        "masking": {
            "mode": s.masking.mode,
            "mask": s.masking.mask,
            "mapping_scope": s.masking.mapping_scope,
            "mapping_persistence": s.masking.mapping_persistence,
        },
        "recognition": {
            "enable_company_regex": bool(s.recognition.enable_company_regex),
            "auto_confirm_threshold": float(s.recognition.auto_confirm_threshold),
            "review_threshold": float(s.recognition.review_threshold),
            "review_mode": s.recognition.review_mode,
        },
        "ner": {
            "enabled": bool(s.ner.enabled),
            "model": ner_model,
            "device": s.ner.device,
            "max_length": int(s.ner.max_length),
        },
        "ollama": {
            "enabled": bool(s.ollama.enabled),
            "url": s.ollama.url,
            "model": s.ollama.model,
            "timeout": int(s.ollama.timeout),
        },
        "security": security,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def config_state_for(settings: Settings, *, source: str = "injected",
                     degraded: bool = False,
                     warning_code: str = "") -> ConfigState:
    return ConfigState(
        schema_version=int(settings.schema_version),
        config_hash=compute_config_hash(settings),
        source=source,
        degraded=degraded,
        warning_code=warning_code,
        saved_at=_now_utc(),
    )


def _last_good_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state" / LAST_GOOD_CONFIG_FILENAME


def _write_last_good(data_dir: str | Path, settings: Settings,
                     state: ConfigState) -> None:
    target = _last_good_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": int(settings.schema_version),
        "config_hash": state.config_hash,
        "saved_at": state.saved_at,
        "settings": asdict(settings),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _load_last_good(data_dir: str | Path) -> tuple[Settings, ConfigState]:
    target = _last_good_path(data_dir)
    payload = json.loads(target.read_text(encoding="utf-8"))
    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise ConfigError("最近可用配置结构无效")
    settings = _settings_from_mapping(raw_settings)
    settings._validate()
    actual_hash = compute_config_hash(settings)
    if actual_hash != payload.get("config_hash"):
        raise ConfigError("最近可用配置哈希不匹配")
    state = ConfigState(
        schema_version=int(payload.get("schema_version", 0)),
        config_hash=actual_hash,
        source="last_good",
        saved_at=str(payload.get("saved_at") or ""),
    )
    return settings, state


def _fallback_data_dir() -> Path:
    env = os.environ.get("LOCALDOCMASK_HOME")
    return Path(env) if env else PROJECT_ROOT / "data"


_SETTINGS: Settings | None = None
_CONFIG_STATE: ConfigState | None = None


def get_config_state() -> ConfigState:
    global _SETTINGS, _CONFIG_STATE
    if _CONFIG_STATE is None:
        result = Settings.load_with_metadata()
        _SETTINGS = result.settings
        _CONFIG_STATE = result.state
    return _CONFIG_STATE


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        get_config_state()
    return _SETTINGS


def _resolve_data_dir(paths: PathsConfig) -> Path:
    env = os.environ.get("LOCALDOCMASK_HOME")
    if env:
        return Path(env)
    if paths.data_dir:
        return Path(paths.data_dir)
    return PROJECT_ROOT / "data"


# ---------- 兼容层：既有模块级符号（行为不变） ----------
_DATA = get_settings()
DATA_DIR = _resolve_data_dir(_DATA.paths)
INPUT_DIR = DATA_DIR / "input"
PROCESSING_DIR = DATA_DIR / "processing"
OUTPUT_DIR = DATA_DIR / "output"
SUCCESS_DIR = OUTPUT_DIR / "success"
QUARANTINE_DIR = OUTPUT_DIR / "quarantine"
LOG_DIR = DATA_DIR / "logs"
STATE_DIR = DATA_DIR / "state"
METRICS_DIR = STATE_DIR / "metrics"
DIAGNOSTICS_DIR = OUTPUT_DIR / "diagnostics"

DEFAULT_MASK = _DATA.masking.mask
OLLAMA_URL = _DATA.ollama.url
DEFAULT_MODEL = _DATA.ollama.model


def ensure_dirs() -> None:
    for d in (INPUT_DIR, PROCESSING_DIR, OUTPUT_DIR, SUCCESS_DIR,
              QUARANTINE_DIR, LOG_DIR, STATE_DIR, METRICS_DIR, DIAGNOSTICS_DIR):
        d.mkdir(parents=True, exist_ok=True)
