# -*- coding: utf-8 -*-
"""本地健康检查：healthy / degraded / unhealthy（T6）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"


@dataclass
class HealthReport:
    status: str
    components: dict = field(default_factory=dict)
    checked_at: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "components": self.components,
            "checked_at": self.checked_at,
        }


class HealthService:
    def __init__(self, repository=None, metrics=None, config_state=None):
        self.repository = repository
        self.metrics = metrics
        self.config_state = config_state
        self._component_status: dict[str, dict] = {}

    def mark_degraded(self, component: str, reason_code: str) -> None:
        self._component_status[str(component)] = {
            "status": DEGRADED,
            "reason": self._safe_code(reason_code, "degraded"),
        }

    def mark_unhealthy(self, component: str, reason_code: str) -> None:
        self._component_status[str(component)] = {
            "status": UNHEALTHY,
            "reason": self._safe_code(reason_code, "unhealthy"),
        }

    def clear(self, component: str) -> None:
        self._component_status.pop(str(component), None)

    def check(self) -> HealthReport:
        components = dict(self._component_status)
        if self.config_state is not None:
            components["configuration"] = {
                "status": DEGRADED if self.config_state.degraded else HEALTHY,
                "reason": (self.config_state.warning_code
                           if self.config_state.degraded else "config_ok"),
            }
        if self.metrics is not None:
            try:
                self.metrics.snapshot()
                components.setdefault("metrics", {
                    "status": HEALTHY, "reason": "metrics_ok"})
            except Exception:
                components["metrics"] = {
                    "status": DEGRADED, "reason": "metrics_read_failed"}

        if self.repository is None:
            self._merge_component(
                components, "database", UNHEALTHY, "repository_unavailable")
        else:
            try:
                self.repository.list_tasks()
                chain = self.repository.verify_event_chain()
                self._merge_component(
                    components, "database", HEALTHY, "database_ok")
                if chain.valid:
                    self._merge_component(
                        components, "event_chain", HEALTHY, "chain_ok")
                else:
                    self._merge_component(
                        components, "event_chain", UNHEALTHY,
                        "event_chain_invalid")
                    if self.metrics is not None:
                        try:
                            self.metrics.record(
                                "event_chain_invalid", 1,
                                code="chain_invalid")
                        except Exception:
                            pass
            except Exception:
                self._merge_component(
                    components, "database", UNHEALTHY, "database_unavailable")
                self._merge_component(
                    components, "event_chain", UNHEALTHY, "chain_uncheckable")

        statuses = {item.get("status") for item in components.values()}
        if UNHEALTHY in statuses:
            overall = UNHEALTHY
        elif DEGRADED in statuses:
            overall = DEGRADED
        else:
            overall = HEALTHY
        return HealthReport(
            status=overall,
            components=components,
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _merge_component(components: dict, name: str, status: str,
                         reason: str) -> None:
        existing = components.get(name)
        if existing and existing.get("status") == UNHEALTHY:
            return
        if (existing and existing.get("status") == DEGRADED
                and status == HEALTHY):
            return
        components[name] = {"status": status, "reason": reason}

    @staticmethod
    def _safe_code(value, default: str) -> str:
        text = "".join(ch for ch in str(value or "")
                       if ch.isascii() and (ch.isalnum() or ch in "_.-"))
        return text[:64] or default
