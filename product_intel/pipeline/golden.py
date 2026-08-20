"""
Golden record assembly.

A product is described by a datasheet, a web page and a price file, and they
disagree. This module decides which value wins, and -- just as importantly --
keeps the ones that lost.

Arbitration order:
  1. Human corrections always win.
  2. Higher source precedence wins (datasheet > catalog > web > distributor).
  3. On a precedence tie, the more reliable extraction method wins.
  4. On a method tie, the higher-confidence value wins.
  5. Numeric near-agreement (within tolerance) is not a conflict; it is
     corroboration, and the winner's confidence goes up rather than down.

Nothing is discarded. Every losing value stays on the product in `observations`
and every genuine disagreement is recorded as an AttributeConflict, so any field
in the finished catalog can be explained months later.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from product_intel.confidence import score_attribute
from product_intel.models import (
    METHOD_RELIABILITY,
    SOURCE_PRECEDENCE,
    AttributeConflict,
    AttributeValue,
    ExtractionMethod,
    Product,
)
from product_intel.schema.dictionary import AttributeDef, CategorySchema

log = logging.getLogger(__name__)

#: Relative tolerance below which two numbers are treated as the same measurement.
#: Covers unit-conversion rounding (1/2" -> 12.7mm) and datasheet rounding.
NUMERIC_TOLERANCE = 0.02


def _precedence(av: AttributeValue) -> int:
    if av.evidence is None:
        return SOURCE_PRECEDENCE.get("inferred", 10)
    return SOURCE_PRECEDENCE.get(av.evidence.source_kind, 40)


def _method_rank(av: AttributeValue) -> float:
    if av.evidence is None:
        return METHOD_RELIABILITY.get(ExtractionMethod.INHERITED, 0.5)
    return METHOD_RELIABILITY.get(av.evidence.method, 0.6)


def values_agree(a: Any, b: Any, attr: Optional[AttributeDef]) -> bool:
    """Do two observations describe the same fact?"""
    if a is None or b is None:
        return False
    if isinstance(a, list) or isinstance(b, list):
        return set(map(str, a if isinstance(a, list) else [a])) == set(
            map(str, b if isinstance(b, list) else [b])
        )
    if attr is not None and attr.is_numeric and isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        scale = max(abs(a), abs(b))
        if scale == 0:
            return True
        return abs(a - b) / scale <= NUMERIC_TOLERANCE
    return str(a).strip().lower() == str(b).strip().lower()


def _group_agreeing(
    observations: Sequence[AttributeValue],
    attr: Optional[AttributeDef],
) -> List[List[AttributeValue]]:
    """Cluster observations into groups that assert the same value."""
    groups: List[List[AttributeValue]] = []
    for av in observations:
        for group in groups:
            if values_agree(group[0].value, av.value, attr):
                group.append(av)
                break
        else:
            groups.append([av])
    return groups


def _distinct_sources(group: Sequence[AttributeValue]) -> int:
    ids = {av.evidence.source_id for av in group if av.evidence is not None}
    return len(ids) or len(group)


def arbitrate(
    code: str,
    observations: Sequence[AttributeValue],
    attr: Optional[AttributeDef],
) -> Tuple[Optional[AttributeValue], Optional[AttributeConflict]]:
    """
    Pick the winning value for one attribute. Returns (winner, conflict_or_None).
    """
    usable = [av for av in observations if av.value is not None]
    if not usable:
        return None, None

    # A human correction ends the argument. Observations are appended in order,
    # so the LAST human value is the most recent one -- taking the first would
    # mean a reviewer's second correction silently never took effect.
    human = [av for av in usable if av.evidence and av.evidence.method == ExtractionMethod.HUMAN]
    if human:
        winner = human[-1]
        score_attribute(winner, attr, agreeing=1, disagreeing=0)
        return winner, None

    groups = _group_agreeing(usable, attr)
    groups.sort(
        key=lambda g: (
            max(_precedence(av) for av in g),
            _distinct_sources(g),
            max(_method_rank(av) for av in g),
        ),
        reverse=True,
    )

    winning_group = groups[0]
    winning_group.sort(key=lambda av: (_precedence(av), _method_rank(av)), reverse=True)
    winner = winning_group[0]

    agreeing = _distinct_sources(winning_group)
    disagreeing = sum(_distinct_sources(g) for g in groups[1:])
    score_attribute(winner, attr, agreeing=agreeing, disagreeing=disagreeing)

    # Corroboration is recorded on the winner so the UI can show "3 sources agree".
    winner.confidence_factors["agreeing_sources"] = float(agreeing)
    winner.confidence_factors["disagreeing_sources"] = float(disagreeing)

    if len(groups) == 1:
        return winner, None

    losing: List[Dict[str, Any]] = []
    for group in groups[1:]:
        for av in group:
            losing.append(
                {
                    "value": av.value,
                    "unit": av.unit,
                    "raw_value": av.raw_value,
                    "source_id": av.evidence.source_id if av.evidence else None,
                    "source_kind": av.evidence.source_kind.value if av.evidence else "inferred",
                    "locator": av.evidence.locator if av.evidence else None,
                    "quote": av.evidence.quote if av.evidence else None,
                }
            )

    win_prec = _precedence(winner)
    top_loser_prec = max(_precedence(av) for g in groups[1:] for av in g)
    if win_prec > top_loser_prec:
        rule = (
            f"source precedence: {winner.evidence.source_kind.value if winner.evidence else 'inferred'}"
            f" ({win_prec}) outranks the alternatives ({top_loser_prec})"
        )
    elif agreeing > 1:
        rule = f"corroboration: {agreeing} independent sources agree"
    else:
        rule = "extraction method reliability"

    # Contradicting a same-precedence source is more serious than losing to a
    # higher-precedence one, and a variant-defining attribute conflict is
    # serious full stop. But two sources of equal *kind* are not equally
    # trustworthy if one was recovered by OCR -- a mangled scan disagreeing
    # with a clean datasheet is expected, not alarming.
    severity = "warning"
    win_method = _method_rank(winner)
    top_loser_method = max(_method_rank(av) for g in groups[1:] for av in g)

    if win_prec == top_loser_prec and abs(win_method - top_loser_method) < 0.15:
        severity = "critical"
    elif attr is not None and (attr.variant_defining or attr.identity):
        severity = "critical"

    conflict = AttributeConflict(
        code=code,
        winning_value=winner.value,
        winning_source=winner.evidence.source_id if winner.evidence else "inferred",
        losing_values=losing,
        resolution_rule=rule,
        severity=severity,
        resolved=True,
    )
    return winner, conflict


def build_golden_record(product: Product, schema: CategorySchema) -> Product:
    """
    Collapse every observation on a product into one arbitrated attribute set.

    Idempotent: it reads only `observations`, so it can be re-run after new
    sources arrive without corrupting anything.
    """
    product.attributes = {}
    product.conflicts = []

    for code, observations in product.observations.items():
        attr = schema.get(code)
        winner, conflict = arbitrate(code, observations, attr)
        if winner is not None:
            product.attributes[code] = winner
        if conflict is not None:
            product.conflicts.append(conflict)

    # Defaults are applied last and only where nothing was observed, so a
    # declared default never overrides real evidence.
    for code, attr in schema.attributes.items():
        if code in product.attributes or attr.default is None or attr.generated:
            continue
        from product_intel.models import InferencePath

        av = AttributeValue(
            code=code,
            value=attr.default,
            unit=attr.canonical_unit,
            normalization_notes=[f"schema default applied ({attr.default})"],
            inference=InferencePath(
                strategy="schema_default",
                from_attribute=code,
                rationale=(
                    f"no source stated this attribute; the attribute dictionary declares "
                    f"'{attr.default}' as the category default"
                ),
            ),
        )
        # A declared default is a deliberate, documented assumption -- weaker
        # than evidence, but not the near-zero a fully unsupported value earns.
        av.confidence = 0.4
        av.confidence_factors = {"schema_default": 0.4}
        product.attributes[code] = av

    return product
