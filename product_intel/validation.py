"""
Validation and quality scoring.

Four independent axes, because "data quality" as a single undifferentiated
number tells you nothing about what to fix:

  completeness    -- filled required attributes / required attributes, per channel
  accuracy        -- share of attributes backed by a verified quote
  consistency     -- share of applicable schema and cross-attribute rules passed
  distinctiveness -- inverse of the peer-group outlier rate

The predecessor's validation gate was binary pass/fail and was globally
disabled by an `auto_approve_needs_review` flag in the shipped config. There is
no such override here. A product that fails validation is routed to review; it
is never silently marked complete.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from product_intel.config import Settings, settings as global_settings
from product_intel.models import Product, QualityScore
from product_intel.schema.dictionary import AttributeDef, CategorySchema, Rule

log = logging.getLogger(__name__)

CHANNELS = ("core", "ecommerce", "enhanced")


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

_COMPARATORS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}

_EXPR_RE = re.compile(
    r"^\s*(?P<lhs>[a-z_][a-z0-9_]*)\s*(?:\*\s*(?P<scale>[\d.]+)\s*)?"
    r"(?P<op>>=|<=|==|!=|>|<)\s*(?P<rhs>[a-z_][a-z0-9_]*|-?[\d.]+|'[^']*')\s*$",
    re.IGNORECASE,
)


def _resolve_operand(token: str, product: Product) -> Tuple[Any, bool]:
    """Resolve an operand to (value, is_available)."""
    token = token.strip()
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1], True
    try:
        return float(token), True
    except ValueError:
        pass
    av = product.attributes.get(token)
    if av is None or av.value is None:
        return None, False
    return av.value, True


def evaluate_expression(expr: str, product: Product) -> Optional[bool]:
    """
    Evaluate a small comparison expression against a product.

    Deliberately a tiny hand-rolled parser rather than eval(): rules come from a
    JSON data file, and a data file must never be able to execute code.
    Returns None when the rule is not applicable (an operand is missing).
    """
    if not expr:
        return None
    m = _EXPR_RE.match(expr)
    if not m:
        log.warning("unparseable rule expression: %r", expr)
        return None

    lhs, lhs_ok = _resolve_operand(m.group("lhs"), product)
    rhs, rhs_ok = _resolve_operand(m.group("rhs"), product)
    if not lhs_ok or not rhs_ok:
        return None

    scale = m.group("scale")
    if scale and isinstance(lhs, (int, float)):
        lhs = lhs * float(scale)

    if isinstance(lhs, (int, float)) != isinstance(rhs, (int, float)):
        lhs, rhs = str(lhs), str(rhs)

    try:
        return _COMPARATORS[m.group("op")](lhs, rhs)
    except TypeError:
        return None


def evaluate_rule(rule: Rule, product: Product) -> Tuple[Optional[bool], str]:
    """Returns (passed, message). passed=None means the rule did not apply."""
    if rule.type == "cross_attribute":
        result = evaluate_expression(rule.expression or "", product)
        return result, rule.message

    if rule.type == "implication":
        condition = evaluate_expression(rule.if_ or "", product)
        if condition is not True:
            return None, ""

        for code in rule.then_present:
            av = product.attributes.get(code)
            if av is None or av.value is None:
                return False, rule.message or f"{code} is required when {rule.if_}"

        for code, bound in rule.then_min.items():
            av = product.attributes.get(code)
            if av is None or not isinstance(av.value, (int, float)):
                continue
            if av.value < bound:
                return False, rule.message or f"{code} must be at least {bound}"

        for code, bound in rule.then_max.items():
            av = product.attributes.get(code)
            if av is None or not isinstance(av.value, (int, float)):
                continue
            if av.value > bound:
                return False, rule.message or f"{code} must not exceed {bound}"

        return True, rule.message

    return None, ""


# ---------------------------------------------------------------------------
# Peer-group outlier detection
# ---------------------------------------------------------------------------


class PeerStatistics:
    """
    Per-category, per-attribute distributions used to spot implausible values.

    Uses the median and the median absolute deviation rather than mean/stdev,
    because a catalog with three badly-parsed values would otherwise inflate the
    standard deviation enough to hide them.
    """

    def __init__(self, threshold: float = 3.5) -> None:
        self.threshold = threshold
        self._stats: Dict[Tuple[str, str], Dict[str, float]] = {}

    def fit(self, products: Sequence[Product]) -> "PeerStatistics":
        buckets: Dict[Tuple[str, str], List[float]] = {}
        for product in products:
            for code, av in product.attributes.items():
                if isinstance(av.value, (int, float)) and not isinstance(av.value, bool):
                    buckets.setdefault((product.category_id, code), []).append(float(av.value))

        for key, values in buckets.items():
            if len(values) < 4:  # too few peers to call anything an outlier
                continue
            median = statistics.median(values)
            deviations = [abs(v - median) for v in values]
            mad = statistics.median(deviations)
            self._stats[key] = {
                "median": median,
                "mad": mad,
                "n": len(values),
                "min": min(values),
                "max": max(values),
            }
        return self

    def is_outlier(self, category_id: str, code: str, value: float) -> Tuple[bool, str]:
        stats = self._stats.get((category_id, code))
        if not stats:
            return False, ""
        median, mad = stats["median"], stats["mad"]
        if mad == 0:
            # No spread among peers: flag only an order-of-magnitude departure.
            if median != 0 and (value > median * 10 or value < median / 10):
                return True, f"{value:g} is far from the peer value {median:g} (n={int(stats['n'])})"
            return False, ""
        modified_z = 0.6745 * (value - median) / mad
        if abs(modified_z) > self.threshold:
            return True, (
                f"{value:g} is {abs(modified_z):.1f} MAD from the category median "
                f"{median:g} (peer range {stats['min']:g}-{stats['max']:g}, n={int(stats['n'])})"
            )
        return False, ""

    def peer_count(self) -> int:
        return len(self._stats)


# ---------------------------------------------------------------------------
# Product validation
# ---------------------------------------------------------------------------


class ValidationReport:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.rule_results: List[Tuple[str, bool, str]] = []
        self.outliers: List[Tuple[str, str]] = []

    @property
    def rules_applicable(self) -> int:
        return len(self.rule_results)

    @property
    def rules_passed(self) -> int:
        return sum(1 for _, passed, _ in self.rule_results if passed)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "rules_applicable": self.rules_applicable,
            "rules_passed": self.rules_passed,
            "outliers": [f"{c}: {m}" for c, m in self.outliers],
        }


def validate_product(
    product: Product,
    schema: CategorySchema,
    peers: Optional[PeerStatistics] = None,
    mirrors: Optional[Dict[str, str]] = None,
) -> ValidationReport:
    """
    Run every check against one product.

    `mirrors` maps source_id -> canonical Markdown. When supplied, quote
    traceability is re-verified against the mirror on disk, which is the check
    that makes the "every field clicks through to its source" claim auditable
    rather than aspirational.
    """
    report = ValidationReport()

    # -- identity -----------------------------------------------------------
    if not product.identity.manufacturer:
        report.errors.append("product has no manufacturer")
    if not product.identity.mpn:
        report.errors.append("product has no manufacturer part number")

    # -- per-attribute schema conformance -----------------------------------
    for code, av in product.attributes.items():
        attr = schema.get(code)
        if attr is None:
            report.warnings.append(f"{code}: not defined in the '{schema.name}' attribute set")
            continue
        for err in av.validation_errors:
            report.errors.append(f"{code}: {err}")
        if attr.datatype == "enum" and attr.allowed_values and av.value is not None:
            if str(av.value) not in attr.allowed_values:
                report.errors.append(f"{code}: '{av.value}' is not an allowed value")
        if not av.is_grounded and not av.normalization_notes:
            report.warnings.append(f"{code}: value has no evidence and no declared inference")

    # -- quote traceability against the mirror on disk ----------------------
    if mirrors:
        from product_intel.pipeline.extractor import _loose_contains

        for code, av in product.attributes.items():
            if av.evidence is None or not av.evidence.quote:
                continue
            mirror = mirrors.get(av.evidence.source_id)
            if mirror is None:
                report.warnings.append(f"{code}: source mirror {av.evidence.source_id} is unavailable")
                continue
            if not _loose_contains(mirror, av.evidence.quote):
                report.errors.append(
                    f"{code}: cited quote could not be located in {av.evidence.source_id} "
                    f"({av.evidence.locator})"
                )
                av.evidence.quote_verified = False

    # -- category rules -----------------------------------------------------
    for rule in schema.rules:
        passed, message = evaluate_rule(rule, product)
        if passed is None:
            continue
        report.rule_results.append((rule.id, passed, message))
        if not passed:
            report.errors.append(f"rule {rule.id}: {message}")

    # -- peer outliers ------------------------------------------------------
    if peers is not None:
        for code, av in product.attributes.items():
            if not isinstance(av.value, (int, float)) or isinstance(av.value, bool):
                continue
            is_out, message = peers.is_outlier(product.category_id, code, float(av.value))
            if is_out:
                report.outliers.append((code, message))
                report.warnings.append(f"{code}: {message}")

    # -- conflicts ----------------------------------------------------------
    for conflict in product.conflicts:
        if conflict.severity == "critical":
            report.warnings.append(
                f"{conflict.code}: sources disagree ({conflict.resolution_rule})"
            )

    return report


def compute_quality(
    product: Product,
    schema: CategorySchema,
    report: Optional[ValidationReport] = None,
    cfg: Optional[Settings] = None,
) -> QualityScore:
    """Compute the four-axis quality score for one product."""
    cfg = cfg or global_settings
    score = QualityScore()

    # -- completeness, per channel -----------------------------------------
    # A family record is not a sellable SKU: 'nominal size' has no meaning for
    # the FV-3000 series as a whole, only for FV3000-050. Holding families to
    # variant-defining requirements floods the review queue with items no one
    # can action.
    exempt = set(schema.variant_defining_codes()) if product.is_family else set()

    for channel in CHANNELS:
        required = [c for c in schema.required_codes(channel) if c not in exempt]
        if not required:
            filled_ratio = 1.0
            missing: List[str] = []
        else:
            missing = [
                c for c in required
                if c not in product.attributes or product.attributes[c].value in (None, "", [])
            ]
            filled_ratio = (len(required) - len(missing)) / len(required)
        setattr(score, f"completeness_{channel}", round(filled_ratio, 4))
        if channel == cfg.target_channel:
            score.missing_required = sorted(missing)

    # -- accuracy -----------------------------------------------------------
    # Measures only values that claim to have been read from a document: of the
    # things we say we found in a source, what share can we still point at?
    # Declared inferences (family inheritance, schema defaults, generated copy)
    # make no such claim and are counted separately, under `inferred_attributes`.
    sourced = [
        av for av in product.attributes.values()
        if not _is_generated(av, schema) and av.inference is None
    ]
    if sourced:
        verified = sum(
            1 for av in sourced
            if av.evidence is not None and av.evidence.quote_verified
        )
        score.accuracy = round(verified / len(sourced), 4)
    else:
        score.accuracy = 0.0

    # -- consistency --------------------------------------------------------
    if report is not None:
        if report.rules_applicable:
            score.consistency = round(report.rules_passed / report.rules_applicable, 4)
        else:
            score.consistency = 1.0 if not report.errors else 0.5
        # Schema-level errors count against consistency too.
        if report.errors:
            penalty = min(0.5, 0.1 * len(report.errors))
            score.consistency = round(max(0.0, score.consistency - penalty), 4)
    else:
        score.consistency = 1.0

    # -- distinctiveness ----------------------------------------------------
    numeric = [
        av for av in product.attributes.values()
        if isinstance(av.value, (int, float)) and not isinstance(av.value, bool)
    ]
    if report is not None and numeric:
        score.distinctiveness = round(1.0 - min(1.0, len(report.outliers) / len(numeric)), 4)
    else:
        score.distinctiveness = 1.0

    target_completeness = getattr(score, f"completeness_{cfg.target_channel}")
    score.overall = round(
        0.40 * target_completeness
        + 0.25 * score.accuracy
        + 0.25 * score.consistency
        + 0.10 * score.distinctiveness,
        4,
    )
    # Family records are never "channel ready" -- they are not sold. They are
    # still scored, because a thin family record means poor inheritance for
    # every variant beneath it.
    score.channel_ready = bool(
        not product.is_family
        and not score.missing_required
        and (report is None or not report.errors)
        and score.accuracy >= 0.5
    )
    return score


def _is_generated(av, schema: CategorySchema) -> bool:
    attr = schema.get(av.code)
    return bool(attr and attr.generated)


def catalog_scorecard(products: Sequence[Product]) -> Dict[str, Any]:
    """Aggregate quality across a catalog. This is the before/after headline."""
    if not products:
        return {
            "products": 0, "completeness_core": 0.0, "completeness_ecommerce": 0.0,
            "completeness_enhanced": 0.0, "accuracy": 0.0, "consistency": 0.0,
            "distinctiveness": 0.0, "overall": 0.0, "channel_ready": 0,
            "channel_ready_pct": 0.0, "conflicts": 0, "attributes_total": 0,
        }

    def mean(fn) -> float:
        vals = [fn(p) for p in products]
        return round(sum(vals) / len(vals), 4)

    ready = sum(1 for p in products if p.quality.channel_ready)
    sellable = [p for p in products if not p.is_family]

    # Confidence is reported separately for read specifications and authored
    # copy. Blending them hides the number that matters: generated marketing
    # text is expected to score lower than a value read from a spec table, and
    # averaging the two makes a catalog of good data look worse than it is.
    from product_intel.schema.dictionary import load_taxonomy

    taxonomy = load_taxonomy()
    sourced_conf: List[float] = []
    generated_conf: List[float] = []
    for p in products:
        schema = taxonomy.get(p.category_id)
        for code, av in p.attributes.items():
            attr = schema.get(code)
            (generated_conf if (attr and attr.generated) else sourced_conf).append(av.confidence)

    return {
        "products": len(products),
        "completeness_core": mean(lambda p: p.quality.completeness_core),
        "completeness_ecommerce": mean(lambda p: p.quality.completeness_ecommerce),
        "completeness_enhanced": mean(lambda p: p.quality.completeness_enhanced),
        "accuracy": mean(lambda p: p.quality.accuracy),
        "consistency": mean(lambda p: p.quality.consistency),
        "distinctiveness": mean(lambda p: p.quality.distinctiveness),
        "overall": mean(lambda p: p.quality.overall),
        "channel_ready": ready,
        "sellable": len(sellable),
        "families": len(products) - len(sellable),
        "channel_ready_pct": round(100.0 * ready / len(sellable), 2) if sellable else 0.0,
        "conflicts": sum(len(p.conflicts) for p in products),
        "attributes_total": sum(len(p.attributes) for p in products),
        "confidence_sourced": round(sum(sourced_conf) / len(sourced_conf), 4) if sourced_conf else 0.0,
        "confidence_generated": round(sum(generated_conf) / len(generated_conf), 4) if generated_conf else 0.0,
        "attributes_sourced": len(sourced_conf),
        "attributes_generated": len(generated_conf),
        "inferred_attributes": sum(
            1 for p in products for av in p.attributes.values() if av.inference is not None
        ),
    }
