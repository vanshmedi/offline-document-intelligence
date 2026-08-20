"""
Confidence scoring.

The predecessor hardcoded `confidence_score = 1.0` on every extracted value and
never consulted its own threshold, so nothing could be triaged. Confidence here
is computed from signals the pipeline actually observes, and it is the mechanism
by which the system scales: high-confidence attributes publish automatically,
low-confidence ones go to a human.

Six independent factors, combined multiplicatively against a base. Multiplicative
because these are failure modes, not features -- one badly broken signal should
drag the score down even when the others look fine.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from product_intel.models import (
    METHOD_RELIABILITY,
    SOURCE_PRECEDENCE,
    AttributeValue,
    ExtractionMethod,
)
from product_intel.schema.dictionary import AttributeDef

#: Weight of each factor's deviation from 1.0. Higher = more punishing.
FACTOR_WEIGHTS: Dict[str, float] = {
    "method": 1.0,
    "quote": 1.0,
    "schema": 1.0,
    "source": 0.6,
    "corroboration": 0.8,
    "normalization": 0.5,
}


def _method_factor(av: AttributeValue) -> float:
    if av.evidence is None:
        return METHOD_RELIABILITY.get(ExtractionMethod.INHERITED, 0.5) if av.inference else 0.3
    return METHOD_RELIABILITY.get(av.evidence.method, 0.6)


def _quote_factor(av: AttributeValue) -> float:
    """A value whose quote could not be located in the mirror is not trustworthy."""
    if av.evidence is None:
        return 0.75 if av.inference else 0.35
    if not av.evidence.quote:
        return 0.55
    return 1.0 if av.evidence.quote_verified else 0.65


def _schema_factor(av: AttributeValue, attr: Optional[AttributeDef]) -> float:
    """
    Range and enum violations are hard evidence that something is wrong.

    Only actual violations count. An earlier version also penalised values
    sitting exactly on a plausibility bound, which fired on entirely normal data
    (a package quantity of 1, against a declared minimum of 1) and produced a
    steady stream of false review flags.
    """
    if av.validation_errors:
        return 0.3
    if attr is None:
        return 0.9
    return 1.0


def _source_factor(av: AttributeValue) -> float:
    if av.evidence is None:
        return 0.6
    precedence = SOURCE_PRECEDENCE.get(av.evidence.source_kind, 40)
    return 0.65 + 0.35 * (precedence / 100.0)


def _corroboration_factor(agreeing: int, disagreeing: int) -> float:
    """Independent sources agreeing is the strongest signal available."""
    if disagreeing > 0 and agreeing <= 1:
        return 0.55
    if disagreeing > 0:
        return 0.75
    if agreeing >= 3:
        return 1.0
    if agreeing == 2:
        return 0.97
    return 0.9  # single source: fine, but unconfirmed


def _normalization_factor(av: AttributeValue) -> float:
    """Assumptions made during normalization are recorded and cost a little."""
    if av.confidence_factors.get("unit_assumed"):
        return 0.88
    if any("not in allowed" in n or "expected" in n for n in av.normalization_notes):
        return 0.8
    return 1.0


def score_attribute(
    av: AttributeValue,
    attr: Optional[AttributeDef] = None,
    agreeing: int = 1,
    disagreeing: int = 0,
) -> float:
    """
    Compute and attach a confidence score in [0, 1].

    Every factor is stored on the value so the score can be explained in the
    review UI rather than appearing as an unexplained number.
    """
    factors = {
        "method": _method_factor(av),
        "quote": _quote_factor(av),
        "schema": _schema_factor(av, attr),
        "source": _source_factor(av),
        "corroboration": _corroboration_factor(agreeing, disagreeing),
        "normalization": _normalization_factor(av),
    }

    score = 1.0
    for name, value in factors.items():
        weight = FACTOR_WEIGHTS.get(name, 1.0)
        score *= 1.0 - weight * (1.0 - value)

    score = max(0.0, min(1.0, score))
    av.confidence = round(score, 4)
    av.confidence_factors = {k: round(v, 4) for k, v in factors.items()}
    return av.confidence


def explain(av: AttributeValue) -> List[str]:
    """Human-readable reasons the score is what it is. Powers the review queue."""
    reasons: List[str] = []
    f = av.confidence_factors
    if not f:
        return ["not yet scored"]

    if f.get("quote", 1.0) < 0.7:
        reasons.append("supporting quote could not be verified against the source mirror")
    if f.get("method", 1.0) < 0.7:
        method = av.evidence.method.value if av.evidence else "inference"
        reasons.append(f"recovered by {method}, which is less reliable than a native spec table")
    if f.get("schema", 1.0) < 0.9:
        reasons.append("value failed or sits at the edge of its schema plausibility range")
    if f.get("corroboration", 1.0) < 0.9:
        reasons.append("sources disagree or only one source reports this value")
    if f.get("normalization", 1.0) < 1.0:
        reasons.append("a unit or enum assumption was made during normalization")
    if f.get("source", 1.0) < 0.8:
        reasons.append("came from a lower-precedence source")
    if not reasons:
        reasons.append("verified quote from a high-precedence source, within schema bounds")
    return reasons


def mean_confidence(values: Sequence[AttributeValue]) -> float:
    scored = [v.confidence for v in values if v.confidence > 0]
    return round(sum(scored) / len(scored), 4) if scored else 0.0
