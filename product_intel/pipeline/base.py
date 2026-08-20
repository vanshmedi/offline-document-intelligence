"""Pipeline step base class with audit wrapping and resume support."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict

from product_intel.models import AuditStep, StepStatus

log = logging.getLogger(__name__)


class PipelineStep(ABC):
    """
    One unit of work.

    `execute` handles timing, audit recording and resume; subclasses implement
    `run` and mutate the shared context. A step that has already completed for
    this subject is skipped, which is what makes re-ingestion cheap.
    """

    #: When True, a failure aborts the whole run rather than degrading.
    critical: bool = True

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, subject_id: str, context: Dict[str, Any]) -> bool: ...

    def execute(self, subject_id: str, context: Dict[str, Any]) -> bool:
        audit = context.get("audit_log")
        entry = context.get("manifest_entry")

        if entry is not None and entry.steps.get(self.name) == StepStatus.COMPLETED.value:
            if not context.get("force"):
                log.debug("[%s] already completed for %s; skipping", self.name, subject_id)
                return True

        started_ts = time.time()
        started = datetime.now(timezone.utc).isoformat()
        log.info("[%s] start %s", self.name, subject_id)

        try:
            ok = self.run(subject_id, context)
            status = StepStatus.COMPLETED if ok else StepStatus.FAILED
            error = None if ok else "step returned a failure status"
        except Exception as exc:  # noqa: BLE001 - steps wrap heterogeneous libraries
            log.exception("[%s] failed for %s", self.name, subject_id)
            ok, status, error = False, StepStatus.FAILED, f"{type(exc).__name__}: {exc}"

        duration_ms = int((time.time() - started_ts) * 1000)
        step = AuditStep(
            step_name=self.name,
            status=status,
            started_at=started,
            completed_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            error_message=error,
            warnings=context.get(f"{self.name}_warnings", []),
            stats=context.get(f"{self.name}_stats", {}),
        )
        if audit is not None:
            audit.steps.append(step)
        if entry is not None:
            entry.steps[self.name] = status.value

        log.info("[%s] %s in %dms", self.name, status.value, duration_ms)
        return ok

    # -- helpers for subclasses --------------------------------------------

    def warn(self, context: Dict[str, Any], message: str) -> None:
        context.setdefault(f"{self.name}_warnings", []).append(message)
        log.warning("[%s] %s", self.name, message)

    def stat(self, context: Dict[str, Any], **kwargs: Any) -> None:
        context.setdefault(f"{self.name}_stats", {}).update(kwargs)
