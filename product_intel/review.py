"""
Human-in-the-loop review.

The predecessor had a `needs_review` status and nothing that consumed it. Here
review is a working queue with a learning loop, which is what turns
human-in-the-loop from a checkbox into a scalability argument: a reviewer does
not correct 500 SKUs, they correct one and the correction is promoted to a rule
that fixes the other 499.

Three parts:
  1. A queue, prioritized by confidence x business impact.
  2. Corrections, applied as human-authored attribute values with the highest
     precedence in golden-record arbitration.
  3. Learned rules, derived from corrections and replayed on future ingests.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from product_intel.config import Settings, settings as global_settings
from product_intel.confidence import explain, score_attribute
from product_intel.manifest import atomic_write_json
from product_intel.models import (
    AttributeValue,
    Evidence,
    ExtractionMethod,
    Product,
    ProductStatus,
    ReviewFlag,
    SourceKind,
)
from product_intel.schema.dictionary import CategorySchema, Taxonomy

log = logging.getLogger(__name__)

#: How much each flag reason contributes to review priority.
REASON_WEIGHT = {
    "conflict_critical": 1.00,
    "missing_required_core": 0.90,
    "validation_error": 0.85,
    "possible_duplicate": 0.95,
    "low_confidence": 0.70,
    "outlier": 0.65,
    "unverified_quote": 0.55,
    "missing_required": 0.50,
    "generated_unverified": 0.45,
}


def _flag_id(product_id: str, code: Optional[str], reason: str) -> str:
    key = f"{product_id}|{code or ''}|{reason}"
    return f"flag_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


class ReviewQueue:
    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.path = self.cfg.review_queue_path
        self._flags: Optional[Dict[str, ReviewFlag]] = None

    def _load(self) -> Dict[str, ReviewFlag]:
        if self._flags is not None:
            return self._flags
        flags: Dict[str, ReviewFlag] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    for item in json.load(f):
                        flag = ReviewFlag(**item)
                        flags[flag.flag_id] = flag
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read review queue: %s", exc)
        self._flags = flags
        return flags

    def flush(self) -> None:
        if self._flags is None:
            return
        atomic_write_json(self.path, [f.model_dump(mode="json") for f in self._flags.values()])

    def add(self, flag: ReviewFlag) -> bool:
        flags = self._load()
        if flag.flag_id in flags and flags[flag.flag_id].resolved:
            return False  # already dealt with; do not re-raise
        is_new = flag.flag_id not in flags
        flags[flag.flag_id] = flag
        return is_new

    def open_flags(self, product_id: Optional[str] = None) -> List[ReviewFlag]:
        flags = [f for f in self._load().values() if not f.resolved]
        if product_id:
            flags = [f for f in flags if f.product_id == product_id]
        return flags

    def all_flags(self) -> List[ReviewFlag]:
        return list(self._load().values())

    def resolve(
        self,
        flag_id: str,
        resolution: str,
        resolved_by: str = "reviewer",
    ) -> Optional[ReviewFlag]:
        flags = self._load()
        flag = flags.get(flag_id)
        if flag is None:
            return None
        flag.resolved = True
        flag.resolution = resolution
        flag.resolved_by = resolved_by
        flag.resolved_at = datetime.now(timezone.utc).isoformat()
        return flag

    def prioritized(self, limit: int = 50) -> List[ReviewFlag]:
        """
        Order the queue by expected value of a reviewer's attention.

        Priority = reason weight x (1 - confidence). A critical conflict on a
        confidently-wrong value outranks a low-confidence optional attribute.
        """
        def priority(flag: ReviewFlag) -> float:
            weight = REASON_WEIGHT.get(flag.reason.split(":")[0], 0.5)
            severity_boost = {"critical": 1.3, "warning": 1.0, "info": 0.7}.get(flag.severity, 1.0)
            return weight * severity_boost * (1.0 - min(flag.confidence, 0.99))

        return sorted(self.open_flags(), key=priority, reverse=True)[:limit]

    def stats(self) -> Dict[str, Any]:
        flags = self.all_flags()
        open_flags = [f for f in flags if not f.resolved]
        by_reason: Dict[str, int] = {}
        for f in open_flags:
            key = f.reason.split(":")[0]
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "total": len(flags),
            "open": len(open_flags),
            "resolved": len(flags) - len(open_flags),
            "by_reason": by_reason,
            "products_affected": len({f.product_id for f in open_flags}),
        }


def flag_product(
    product: Product,
    schema: CategorySchema,
    report,
    queue: ReviewQueue,
    cfg: Optional[Settings] = None,
) -> int:
    """Raise review flags for one product. Returns the number of new flags."""
    cfg = cfg or global_settings
    added = 0
    pid = product.identity.product_id

    for conflict in product.conflicts:
        if conflict.severity != "critical":
            continue
        losers = ", ".join(str(v.get("value")) for v in conflict.losing_values[:3])
        added += queue.add(
            ReviewFlag(
                flag_id=_flag_id(pid, conflict.code, "conflict_critical"),
                product_id=pid,
                attribute_code=conflict.code,
                reason=f"conflict_critical: sources disagree ({conflict.winning_value} vs {losers})",
                severity="critical",
                confidence=product.attributes[conflict.code].confidence
                if conflict.code in product.attributes else 0.5,
                suggested_value=conflict.winning_value,
            )
        )

    if product.identity.suspected_duplicate_of:
        added += queue.add(
            ReviewFlag(
                flag_id=_flag_id(pid, None, "possible_duplicate"),
                product_id=pid,
                reason=(
                    f"possible_duplicate: {product.identity.duplicate_evidence}. "
                    f"Confirm whether this is a distinct product or an OCR misread of "
                    f"{product.identity.suspected_duplicate_of}."
                ),
                severity="critical",
                confidence=0.3,
            )
        )

    core_required = set(schema.required_codes("core"))
    for code in product.quality.missing_required:
        is_core = code in core_required
        added += queue.add(
            ReviewFlag(
                flag_id=_flag_id(pid, code, "missing_required"),
                product_id=pid,
                attribute_code=code,
                reason=f"missing_required{'_core' if is_core else ''}: "
                       f"{schema.attributes[code].name if code in schema.attributes else code} "
                       f"is required for the {cfg.target_channel} channel",
                severity="critical" if is_core else "warning",
                confidence=0.0,
            )
        )

    for code, av in product.attributes.items():
        attr = schema.get(code)
        is_generated = bool(attr and attr.generated)

        # Generated copy is inherently lower-confidence than a read
        # specification, so scoring it against the same threshold would bury
        # every real problem under one flag per generated field per product.
        # It is only escalated when generation produced a claim that could not
        # be traced back to a source attribute.
        if is_generated:
            unverified = [n for n in av.normalization_notes if "unverified" in n.lower()]
            if unverified:
                added += queue.add(
                    ReviewFlag(
                        flag_id=_flag_id(pid, code, "generated_unverified"),
                        product_id=pid,
                        attribute_code=code,
                        reason=f"generated_unverified: {unverified[0]}",
                        severity="critical",
                        confidence=av.confidence,
                        suggested_value=av.value,
                    )
                )
        elif av.confidence < cfg.review_confidence_threshold and av.value is not None:
            reason_detail = "; ".join(explain(av)[:2])
            added += queue.add(
                ReviewFlag(
                    flag_id=_flag_id(pid, code, "low_confidence"),
                    product_id=pid,
                    attribute_code=code,
                    reason=f"low_confidence: {av.confidence:.2f} -- {reason_detail}",
                    severity="warning",
                    confidence=av.confidence,
                    suggested_value=av.value,
                )
            )
        if av.validation_errors:
            added += queue.add(
                ReviewFlag(
                    flag_id=_flag_id(pid, code, "validation_error"),
                    product_id=pid,
                    attribute_code=code,
                    reason=f"validation_error: {'; '.join(av.validation_errors[:2])}",
                    severity="critical",
                    confidence=av.confidence,
                    suggested_value=av.value,
                )
            )

    for code, message in getattr(report, "outliers", []):
        added += queue.add(
            ReviewFlag(
                flag_id=_flag_id(pid, code, "outlier"),
                product_id=pid,
                attribute_code=code,
                reason=f"outlier: {message}",
                severity="warning",
                confidence=product.attributes[code].confidence if code in product.attributes else 0.5,
                suggested_value=product.attributes[code].value if code in product.attributes else None,
            )
        )

    for err in getattr(report, "errors", [])[:5]:
        if err.startswith("rule "):
            added += queue.add(
                ReviewFlag(
                    flag_id=_flag_id(pid, None, f"validation_error:{err[:30]}"),
                    product_id=pid,
                    reason=f"validation_error: {err}",
                    severity="critical",
                    confidence=0.4,
                )
            )

    return added


# ---------------------------------------------------------------------------
# Corrections and the learning loop
# ---------------------------------------------------------------------------


class LearnedRules:
    """
    Corrections promoted into reusable rules.

    Two kinds are learned automatically:
      - `enum_synonym`: a reviewer mapped 'SS 316L' to 'Stainless Steel 316',
        so every future sighting of that surface form maps the same way.
      - `attribute_default`: a manufacturer always uses the same value for an
        attribute their datasheets omit, so it can be pre-filled as an
        inference rather than left blank.
    """

    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.path = self.cfg.learned_rules_path
        self._rules: Optional[Dict[str, Any]] = None

    def _load(self) -> Dict[str, Any]:
        if self._rules is not None:
            return self._rules
        rules: Dict[str, Any] = {"enum_synonyms": {}, "manufacturer_defaults": {}, "corrections": []}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                rules.update(loaded)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read learned rules: %s", exc)
        self._rules = rules
        return rules

    def flush(self) -> None:
        if self._rules is not None:
            atomic_write_json(self.path, self._rules)

    def learn_from_correction(
        self,
        product: Product,
        code: str,
        old_value: Any,
        new_value: Any,
        reviewer: str = "reviewer",
    ) -> Optional[str]:
        """Record a correction and, when the pattern is safe to generalize, promote it."""
        rules = self._load()
        rules["corrections"].append(
            {
                "product_id": product.identity.product_id,
                "manufacturer": product.identity.manufacturer,
                "category_id": product.category_id,
                "code": code,
                "old_value": str(old_value),
                "new_value": str(new_value),
                "reviewer": reviewer,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

        promoted: Optional[str] = None

        # A raw surface form corrected to a canonical enum generalizes safely.
        av = product.attributes.get(code)
        raw = (av.raw_value if av else None) or (str(old_value) if old_value is not None else None)
        if raw and isinstance(new_value, str) and raw.strip().lower() != new_value.strip().lower():
            key = raw.strip().lower()
            rules["enum_synonyms"].setdefault(code, {})[key] = new_value
            promoted = f"enum_synonym: '{key}' -> '{new_value}' for {code}"

        # Three identical corrections for one manufacturer/attribute become a default.
        same = [
            c for c in rules["corrections"]
            if c["manufacturer"] == product.identity.manufacturer
            and c["code"] == code
            and c["new_value"] == str(new_value)
        ]
        if len(same) >= 3:
            mfr_key = product.identity.manufacturer
            rules["manufacturer_defaults"].setdefault(mfr_key, {})[code] = str(new_value)
            promoted = f"manufacturer_default: {mfr_key}.{code} = '{new_value}' (learned from {len(same)} corrections)"

        return promoted

    def enum_synonyms_for(self, code: str) -> Dict[str, str]:
        return self._load()["enum_synonyms"].get(code, {})

    def all_enum_synonyms(self) -> Dict[str, Dict[str, str]]:
        return self._load()["enum_synonyms"]

    def defaults_for(self, manufacturer: str) -> Dict[str, str]:
        return self._load()["manufacturer_defaults"].get(manufacturer, {})

    def stats(self) -> Dict[str, int]:
        rules = self._load()
        return {
            "corrections": len(rules["corrections"]),
            "enum_synonyms": sum(len(v) for v in rules["enum_synonyms"].values()),
            "manufacturer_defaults": sum(len(v) for v in rules["manufacturer_defaults"].values()),
        }


def apply_correction(
    product: Product,
    code: str,
    new_value: Any,
    schema: CategorySchema,
    reviewer: str = "reviewer",
    note: str = "",
) -> AttributeValue:
    """
    Apply a human correction.

    Recorded as a HUMAN-method observation, which wins every subsequent
    arbitration -- so a correction survives re-ingestion of the source that was
    wrong in the first place.
    """
    from product_intel.pipeline.normalizer import normalize_value

    attr = schema.get(code)
    old = product.attributes.get(code)
    old_value = old.value if old else None

    if attr is not None:
        av = normalize_value(new_value, attr)
    else:
        av = AttributeValue(code=code, value=new_value, raw_value=str(new_value))

    av.evidence = Evidence(
        source_id=f"human:{reviewer}",
        source_kind=SourceKind.USER,
        locator="review queue",
        quote=note or f"corrected by {reviewer} from '{old_value}' to '{new_value}'",
        method=ExtractionMethod.HUMAN,
        quote_verified=True,
    )
    score_attribute(av, attr, agreeing=1, disagreeing=0)

    product.attributes[code] = av
    product.observations.setdefault(code, []).append(av)
    product.updated_at = datetime.now(timezone.utc).isoformat()
    return av
