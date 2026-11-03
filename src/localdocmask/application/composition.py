"""组合根（应用工厂）：把识别、持久化、一致性映射等模块真实装配成可用服务。

对应 docs/architecture/module_contracts §3 / data_flow §7。本模块是唯一允许
"new" 出具体基础设施实现的地方；应用层其余代码只依赖其构造出的服务。

用法：
    services = build_services()
    services.pipeline.prepare(...)
    services.close()
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..domain.policy import ReviewPolicy
from ..domain.exceptions import ErrorCode, LocalDocMaskError
from ..infrastructure.config.settings import (
    DIAGNOSTICS_DIR,
    LOG_DIR,
    METRICS_DIR,
    PROCESSING_DIR,
    PROJECT_ROOT,
    QUARANTINE_DIR,
    STATE_DIR,
    SUCCESS_DIR,
    config_state_for,
    ensure_dirs,
    get_config_state,
    get_settings,
)
from ..infrastructure.logging import logger as logging_mod
from ..infrastructure.logging.health import HEALTHY, HealthService, UNHEALTHY
from ..infrastructure.logging.metrics import LocalMetrics
from ..security.diagnostics import DiagnosticService
from ..security.temp_lifecycle import TempLifecycle
from ..recognition.knowledge import KnowledgeBase
from ..recognition.pipeline import (
    RecognitionPipeline, extract_ner_entities_worker,
)
from ..recognition.rule_based import Detector
from ..security.auditor import Auditor
from ..security.redaction.mapping import ConsistencyMapper
from .batch import BatchService
from .pipeline import TaskManager

#: 词库统一位置（任务一：资源归位到项目根）
RESOURCES_DIR = PROJECT_ROOT / "resources"


class _AppHealthService(HealthService):
    """补充显式健康组件，区分 NER 已启用、关闭和加载失败。"""

    def mark_healthy(self, component: str, reason_code: str) -> None:
        self._component_status[str(component)] = {
            "status": HEALTHY,
            "reason": self._safe_code(reason_code, "healthy"),
        }


def build_mapper(settings) -> ConsistencyMapper:
    """按 settings 构建一致性映射器（scope + persistence）。

    settings.masking.mapping_persistence: memory=不落盘 / disk=落盘(protected_file)。
    GUI 与会话、批次共享同一构造口径，使配置项真正生效（m5 修复）。
    """
    persistence = ("protected_file" if getattr(settings.masking, "mapping_persistence", "memory") == "disk"
                   else "memory")
    scope = getattr(settings.masking, "mapping_scope", "batch") or "batch"
    return ConsistencyMapper(scope=scope, persistence=persistence)


def _resolve_data_dir(settings) -> Path:
    """与 settings._resolve_data_dir 同序：env > 显式配置 > 项目根 data/。"""
    env = os.environ.get("LOCALDOCMASK_HOME")
    if env:
        return Path(env)
    if getattr(settings.paths, "data_dir", None):
        return Path(settings.paths.data_dir)
    return PROJECT_ROOT / "data"


@dataclass
class ApplicationServices:
    """装配完成的应用服务集合。"""
    settings: object
    pipeline: TaskManager
    batch: BatchService
    knowledge: KnowledgeBase
    repository: object
    metrics: object
    health: HealthService
    temp_lifecycle: TempLifecycle
    diagnostics: DiagnosticService
    config_state: object

    def make_mapper(self) -> ConsistencyMapper:
        """按 settings 构建一个一致性映射器（GUI 会话/预览复用）。"""
        return build_mapper(self.settings)

    def close(self) -> None:
        """关闭仓储连接等持有的外部资源。"""
        repo = getattr(self, "repository", None)
        if repo is not None:
            try:
                repo.close()
            except Exception:
                pass


def build_services(settings=None, resources_dir: str | Path | None = None, *,
                   extra_companies=None, extra_custom_words=None,
                   extra_persons=None, enable_company_regex=None,
                   ollama_enabled=None) -> ApplicationServices:
    """构造完整应用服务（组合根）。

    Parameters
    ----------
    settings:
        可选注入；缺省走 ``get_settings()``（安全默认 + config.yaml + env）。
    resources_dir:
        词库目录；缺省为项目根 ``resources/``。显式传入时以其为准（测试/定制）。
    extra_companies / extra_custom_words / extra_persons:
        界面追加的企业名/自定义敏感词/人名名单，追加在默认词库之后。
    enable_company_regex:
        覆盖设置中的公司名正则开关（来自界面勾选）。
    ollama_enabled:
        覆盖 settings.ollama.enabled（来自界面勾选）；其余 LLM 参数仍取设置。
    """
    if settings is None:
        settings = get_settings()
        config_state = get_config_state()
    else:
        config_state = config_state_for(settings, source="injected")

    ensure_dirs()
    metrics = LocalMetrics(
        METRICS_DIR,
        max_bytes=settings.observability.metrics_max_bytes,
        enabled=settings.observability.metrics_enabled,
    )

    # 结构化日志（幂等、失败不阻断）
    logging_mod.configure_logging(settings.logging)
    optional_degraded: list[tuple[str, str]] = []
    optional_healthy: list[tuple[str, str]] = []

    # 词库：优先项目根 resources；显式传入 resources_dir 时以其为准
    res_dir = Path(resources_dir) if resources_dir else RESOURCES_DIR
    knowledge = KnowledgeBase.load(res_dir)

    # 预加载词库的 Detector：黑名单作为组织类名单、领域词作为自定义敏感词，
    # 使格式处理器（handler）在抽取阶段就能定位这些词。
    companies = sorted(knowledge.blacklist) + [
        w for w in (extra_companies or []) if w and w.strip()]
    custom_words = sorted(knowledge.domain_terms) + [
        w for w in (extra_custom_words or []) if w and w.strip()]
    detector = Detector(
        companies=companies,
        custom_words=custom_words,
        persons=[w for w in (extra_persons or []) if w and w.strip()],
        enable_company_regex=(settings.recognition.enable_company_regex
                              if enable_company_regex is None else enable_company_regex),
    )

    # LLM：仅当显式启用时构造（回环/禁代理已在 OllamaClient 内建）
    llm = None
    _ollama_enabled = settings.ollama.enabled if ollama_enabled is None else ollama_enabled
    if _ollama_enabled:
        from ..infrastructure.llm.ollama import OllamaClient
        llm = OllamaClient(
            url=settings.ollama.url,
            model=settings.ollama.model,
            timeout=settings.ollama.timeout,
        )

    # NER：默认使用随包批准的模型；加载失败保留规则路径但明确标记 degraded。
    ner = None
    if getattr(settings.ner, "enabled", False):
        try:
            from ..infrastructure.ner.onnx_ner import OnnxChineseNER
            ner = OnnxChineseNER(
                model_dir=settings.ner.model_dir or None,
                device=settings.ner.device,
                max_length=settings.ner.max_length,
            )
            optional_healthy.append(("local_ner", "ner_model_loaded"))
        except Exception:
            ner = None
            optional_degraded.append(("local_ner", "ner_model_unavailable"))
    else:
        optional_degraded.append(("local_ner", "ner_disabled"))

    if _ollama_enabled and llm is None:
        optional_degraded.append(("local_llm", "optional_service_unavailable"))

    policy = ReviewPolicy(
        review_mode=settings.recognition.review_mode,
        auto_confirm_threshold=settings.recognition.auto_confirm_threshold,
        review_threshold=settings.recognition.review_threshold,
    )

    recognition = RecognitionPipeline(
        knowledge=knowledge,
        policy=policy,
        llm=llm,
        ner=ner,
        enable_company_regex=settings.recognition.enable_company_regex,
        detector=detector,
        ner_worker_target=extract_ner_entities_worker,
        ner_worker_args=(
            settings.ner.model_dir or None,
            settings.ner.device,
            settings.ner.max_length,
        ),
    )

    # 持久化与 T6 本地运行状态
    data_dir = _resolve_data_dir(settings)
    from ..infrastructure.persistence.recovery import RecoveryService
    from ..infrastructure.persistence.sqlite_repo import SqliteTaskRepository
    repository = SqliteTaskRepository(data_dir / "tasks.db")
    health = _AppHealthService(
        repository=repository, metrics=metrics, config_state=config_state)
    for component, reason in optional_healthy:
        health.mark_healthy(component, reason)
    for component, reason in optional_degraded:
        health.mark_degraded(component, reason)

    try:
        temp_lifecycle = TempLifecycle(
            PROCESSING_DIR, STATE_DIR / "temp_manifest.jsonl")
    except Exception as exc:
        repository.close()
        raise LocalDocMaskError(
            f"临时目录初始化失败：{type(exc).__name__}",
            code=ErrorCode.PERSISTENCE_ERROR,
        ) from exc

    # 启动恢复：失败必须显式暴露，不能带着不确定状态启动
    try:
        affected = RecoveryService().recover(
            repository, PROCESSING_DIR,
            success_dir=SUCCESS_DIR, quarantine_dir=QUARANTINE_DIR)
        if affected:
            try:
                metrics.record("recovery_event", len(affected), stage="recover")
            except Exception:
                health.mark_degraded("metrics", "metrics_write_failed")
        repository.append_operation("*", "startup", "服务启动，崩溃恢复完成")
    except Exception as exc:
        repository.close()
        raise LocalDocMaskError(
            f"启动恢复失败：{type(exc).__name__}: {exc}",
            code=ErrorCode.PERSISTENCE_ERROR,
        ) from exc

    # TTL 清理只作用于 processing；quarantine 永不自动删除。
    try:
        cleanup_report = temp_lifecycle.cleanup_expired(
            repository,
            ttl_seconds=float(settings.security.tempfile_retention_days) * 86400,
        )
        if cleanup_report.cleaned:
            try:
                metrics.record("temp_ttl_cleaned", cleanup_report.cleaned,
                               stage="recover")
            except Exception:
                health.mark_degraded("metrics", "metrics_write_failed")
    except Exception as exc:
        health.mark_degraded("temp_cleanup", "startup_cleanup_failed")
        try:
            metrics.record("temp_cleanup_failed", 1, stage="recover",
                           code=type(exc).__name__)
        except Exception:
            health.mark_degraded("metrics", "metrics_write_failed")
        repository.append_operation(
            "*", "recovery.temp_cleanup_failed",
            f"error={type(exc).__name__}")

    auditor = Auditor()
    pipeline = TaskManager(
        recognition=recognition,
        repository=repository,
        settings=settings,
        auditor=auditor,
        metrics=metrics,
        health=health,
        temp_lifecycle=temp_lifecycle,
    )
    batch = BatchService(pipeline, settings)
    diagnostics = DiagnosticService(
        settings=settings,
        config_state=config_state,
        health=health,
        repository=repository,
        metrics=metrics,
        log_dir=LOG_DIR,
        diagnostics_dir=DIAGNOSTICS_DIR,
    )

    health_report = health.check()
    try:
        metrics.record("health_status", 1, outcome=health_report.status)
    except Exception:
        health.mark_degraded("metrics", "metrics_write_failed")
    if health_report.status == UNHEALTHY:
        repository.close()
        raise LocalDocMaskError(
            "健康检查失败，拒绝启动",
            code=ErrorCode.PERSISTENCE_ERROR,
        )

    return ApplicationServices(
        settings=settings,
        pipeline=pipeline,
        batch=batch,
        knowledge=knowledge,
        repository=repository,
        metrics=metrics,
        health=health,
        temp_lifecycle=temp_lifecycle,
        diagnostics=diagnostics,
        config_state=config_state,
    )
