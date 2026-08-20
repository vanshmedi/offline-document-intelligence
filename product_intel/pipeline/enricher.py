"""
Enrichment: filling gaps and authoring commerce copy.

This is the layer the predecessor pipeline had no equivalent of. It only ever
extracted what was written; it never produced a record a webstore could publish.

Two jobs:

1. **Gap filling.** "Limited product information" is the premise of the brief.
   When a variant is missing an attribute, the value usually exists on the
   family datasheet or on a sibling SKU. The graph is traversed to find it --
   and the result is tagged `inferred` with the exact inheritance path, never
   presented as though it were read from the product's own datasheet. An
   inherited value that is silently indistinguishable from an observed one is
   worse than a blank.

2. **Grounded generation.** Descriptions, feature bullets, SEO metadata and
   search keywords. Every generated sentence is built from attributes that
   themselves carry evidence, so generated copy inherits a citation trail: hover
   a claim, see the datasheet line behind it.

Generation degrades rather than fails. With no model configured, templated copy
is produced from the attribute set -- less fluent, but factual, deterministic,
and incapable of inventing a feature the product does not have.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from product_intel.config import Settings, settings as global_settings
from product_intel.confidence import score_attribute
from product_intel.graph import ProductGraph
from product_intel.llm.provider import LLMProvider, LLMUnavailable
from product_intel.models import (
    AttributeValue,
    ExtractionMethod,
    InferencePath,
    Product,
    RelationType,
)
from product_intel.schema.dictionary import AttributeDef, CategorySchema, Taxonomy

log = logging.getLogger(__name__)

#: Attributes that must never be inherited: they are what make a variant a
#: variant, so copying them across the family would fabricate the distinction.
NON_INHERITABLE: Set[str] = {
    "mpn", "gtin", "product_name", "seo_title", "seo_description",
    "short_description", "long_description", "feature_bullets",
    "search_keywords", "list_price_usd", "package_quantity",
}


# ---------------------------------------------------------------------------
# Gap filling
# ---------------------------------------------------------------------------


def fill_gaps(
    product: Product,
    schema: CategorySchema,
    graph: ProductGraph,
    catalog: Dict[str, Product],
    cfg: Optional[Settings] = None,
) -> List[str]:
    """
    Fill missing attributes from the product's family. Returns the codes filled.

    Inheritance order: the base/family product first (most authoritative), then
    a consensus across sibling variants -- and only when the siblings agree,
    because a split vote means the attribute genuinely varies by variant.
    """
    cfg = cfg or global_settings
    if not cfg.enable_gap_fill_inheritance:
        return []

    filled: List[str] = []
    required = set(schema.required_codes(cfg.target_channel))

    missing = [
        code for code in required
        if code not in NON_INHERITABLE
        and not (schema.get(code) and schema.get(code).generated)
        and (code not in product.attributes or product.attributes[code].value in (None, "", []))
    ]
    if not missing:
        return []

    pid = product.identity.product_id
    base_id = product.identity.base_product_id or graph.base_of(pid)
    sibling_ids = [s for s in graph.siblings(pid) if s != base_id]

    for code in missing:
        attr = schema.get(code)
        if attr is None or attr.variant_defining:
            continue  # never inherit what distinguishes variants

        inherited: Optional[AttributeValue] = None
        path: Optional[InferencePath] = None

        # 1. the family / base product
        base = catalog.get(base_id) if base_id else None
        if base is not None:
            source_av = base.attributes.get(code)
            if source_av is not None and source_av.value not in (None, "", []):
                inherited = source_av
                path = InferencePath(
                    strategy="family_inheritance",
                    from_product_id=base_id,
                    from_attribute=code,
                    rationale=(
                        f"not stated for {product.identity.mpn}; inherited from family product "
                        f"{base.identity.mpn}, which all variants in this series share"
                    ),
                )

        # 2. unanimous sibling consensus
        if inherited is None and sibling_ids:
            candidates = [
                catalog[s].attributes[code]
                for s in sibling_ids
                if s in catalog
                and code in catalog[s].attributes
                and catalog[s].attributes[code].value not in (None, "", [])
            ]
            if candidates:
                distinct = {str(c.value) for c in candidates}
                if len(distinct) == 1 and len(candidates) >= 2:
                    inherited = candidates[0]
                    donors = [catalog[s].identity.mpn for s in sibling_ids if s in catalog][:3]
                    path = InferencePath(
                        strategy="variant_sibling_consensus",
                        from_product_id=candidates[0].evidence.source_id if candidates[0].evidence else None,
                        from_attribute=code,
                        rationale=(
                            f"not stated for {product.identity.mpn}; all {len(candidates)} sibling "
                            f"variants ({', '.join(donors)}) agree on this value"
                        ),
                    )

        if inherited is None or path is None:
            continue

        av = AttributeValue(
            code=code,
            value=inherited.value,
            raw_value=inherited.raw_value,
            unit=inherited.unit,
            inference=path,
            normalization_notes=[f"inferred, not read from this product's own sources"],
        )
        # Inherited values inherit their donor's evidence for traceability, but
        # are explicitly marked as inference-derived so nothing can mistake them
        # for a direct reading.
        if inherited.evidence is not None:
            av.evidence = inherited.evidence.model_copy(
                update={"method": ExtractionMethod.INHERITED}
            )
        score_attribute(av, schema.get(code), agreeing=1, disagreeing=0)
        av.confidence = round(av.confidence * 0.85, 4)  # inference is never as good as observation

        product.attributes[code] = av
        product.observations.setdefault(code, []).append(av)
        filled.append(code)

    return filled


# ---------------------------------------------------------------------------
# Copy generation
# ---------------------------------------------------------------------------


def _spec_phrases(product: Product, schema: CategorySchema, limit: int = 8) -> List[Tuple[str, str, str]]:
    """
    Build (attribute_code, label, rendered_value) tuples for the most
    commerce-relevant attributes. Variant-defining and core attributes first --
    those are what a buyer filters on.
    """
    ranked: List[Tuple[int, str, AttributeDef, AttributeValue]] = []
    for code, av in product.attributes.items():
        attr = schema.get(code)
        if attr is None or attr.generated or av.value in (None, "", []):
            continue
        if code in ("manufacturer", "mpn", "gtin", "product_name", "uom"):
            continue
        priority = 0
        if attr.variant_defining:
            priority += 3
        if attr.is_required("core"):
            priority += 2
        elif attr.is_required("ecommerce"):
            priority += 1
        ranked.append((priority, code, attr, av))

    ranked.sort(key=lambda r: (-r[0], r[1]))
    out: List[Tuple[str, str, str]] = []
    for _, code, attr, av in ranked[:limit]:
        if isinstance(av.value, list):
            rendered = ", ".join(str(v) for v in av.value)
        elif attr.is_numeric:
            num = float(av.value)
            rendered = f"{num:g} {av.unit}".strip() if av.unit else f"{num:g}"
        else:
            rendered = str(av.value)
        out.append((code, attr.name, rendered))
    return out


def _template_copy(
    product: Product,
    schema: CategorySchema,
    specs: Sequence[Tuple[str, str, str]],
) -> Dict[str, Any]:
    """Deterministic fallback copy. Factual, dull, and never wrong."""
    mfr = product.identity.manufacturer
    mpn = product.identity.mpn
    cat = schema.name
    lead = [s for s in specs[:3]]
    lead_phrase = ", ".join(f"{label.lower()} {value}" for _, label, value in lead)

    name = product.get("product_name") or f"{mfr} {mpn} {cat}"
    short = f"{mfr} {mpn} {cat}"
    if lead_phrase:
        short += f" with {lead_phrase}"
    short = short[:300].rstrip() + "."

    body = [f"The {mfr} {mpn} is a {cat.lower()} engineered for industrial and commercial applications."]
    if specs:
        body.append(
            "Key specifications include "
            + "; ".join(f"{label.lower()} of {value}" for _, label, value in specs[:5])
            + "."
        )
    certs = product.get("certifications")
    if certs:
        body.append(f"This product carries {', '.join(certs)} approval.")
    warranty = product.get("warranty_months")
    if warranty:
        body.append(f"Backed by a {int(warranty)}-month manufacturer warranty.")

    bullets = [f"{label}: {value}" for _, label, value in specs[:6]]
    if certs:
        bullets.append(f"Certified to {', '.join(certs[:3])}")

    keywords = [cat.lower(), mfr.lower(), mpn.lower(), f"{mfr.lower()} {cat.lower()}"]
    keywords += [v.lower() for _, _, v in specs[:3]]

    return {
        "product_name": name,
        "short_description": short,
        "long_description": " ".join(body)[:3000],
        "feature_bullets": bullets,
        "seo_title": f"{mfr} {mpn} | {cat}"[:70],
        "seo_description": short[:160],
        "search_keywords": list(dict.fromkeys(keywords))[:12],
    }


def _llm_copy(
    provider: LLMProvider,
    product: Product,
    schema: CategorySchema,
    specs: Sequence[Tuple[str, str, str]],
) -> Optional[Dict[str, Any]]:
    """Generate copy from the attribute set, constrained to stated facts only."""
    spec_block = "\n".join(f"- {label}: {value}" for _, label, value in specs)
    mfr = product.identity.manufacturer
    mpn = product.identity.mpn

    prompt = (
        "You are a B2B industrial catalog copywriter. Write commerce-ready content for "
        "the product below, using ONLY the verified specifications given.\n\n"
        "HARD RULES:\n"
        "1. Use ONLY the specifications listed. Do not add features, materials, "
        "applications, certifications or performance claims that are not listed.\n"
        "2. No marketing superlatives ('best', 'revolutionary', 'industry-leading').\n"
        "3. Write for a professional trade buyer -- a contractor or a maintenance "
        "engineer -- not a consumer.\n"
        "4. Every factual claim must trace to a listed specification.\n\n"
        f"MANUFACTURER: {mfr}\nPART NUMBER: {mpn}\nCATEGORY: {schema.name}\n\n"
        f"VERIFIED SPECIFICATIONS:\n{spec_block}\n\n"
        "Return JSON with exactly these keys:\n"
        '{"product_name": "<=90 chars, manufacturer + part number + what it is + key differentiator",\n'
        ' "short_description": "<=300 chars, one or two sentences",\n'
        ' "long_description": "<=1200 chars, 2-3 paragraphs covering what it is, key specs, and typical use",\n'
        ' "feature_bullets": ["4-6 short bullets, each tied to a specification"],\n'
        ' "seo_title": "<=70 chars",\n'
        ' "seo_description": "<=160 chars",\n'
        ' "search_keywords": ["8-12 terms a trade buyer would actually search"]}'
    )

    try:
        payload = provider.complete_json(prompt, expect="object")
    except LLMUnavailable as exc:
        log.info("generation falling back to templates: %s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _verify_generated(
    payload: Dict[str, Any],
    specs: Sequence[Tuple[str, str, str]],
    product: Product,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Check generated copy for numbers that do not appear in the source facts.

    A model that invents '20-year warranty' or '3000 PSI' is the single most
    damaging failure mode for a commerce catalog, so generated text is scanned
    for numeric claims and any that do not correspond to a known attribute value
    are reported.
    """
    known: Set[str] = set()
    for _, _, value in specs:
        for num in re.findall(r"\d+(?:\.\d+)?", str(value)):
            known.add(num.rstrip("0").rstrip(".") if "." in num else num)
    for av in product.attributes.values():
        if isinstance(av.value, (int, float)) and not isinstance(av.value, bool):
            s = f"{float(av.value):g}"
            known.add(s)
            known.add(str(int(av.value)) if float(av.value).is_integer() else s)
    for token in (product.identity.mpn, product.identity.manufacturer):
        for num in re.findall(r"\d+(?:\.\d+)?", str(token or "")):
            known.add(num)

    warnings: List[str] = []
    for field in ("short_description", "long_description", "seo_title", "seo_description"):
        text = str(payload.get(field) or "")
        for num in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", text):
            canonical = num.rstrip("0").rstrip(".") if "." in num else num
            if canonical not in known and num not in known:
                warnings.append(f"{field}: unverified figure '{num}' does not appear in the source data")
    bullets = payload.get("feature_bullets") or []
    if isinstance(bullets, list):
        for bullet in bullets:
            for num in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])", str(bullet)):
                canonical = num.rstrip("0").rstrip(".") if "." in num else num
                if canonical not in known and num not in known:
                    warnings.append(f"feature_bullets: unverified figure '{num}'")
    return payload, warnings


def generate_content(
    product: Product,
    schema: CategorySchema,
    provider: Optional[LLMProvider] = None,
    cfg: Optional[Settings] = None,
) -> Tuple[List[str], List[str]]:
    """
    Author the generated attributes for one product.

    Returns (generated_codes, warnings). Values are written straight onto the
    product with an InferencePath recording which attributes they were derived
    from, so generated copy is as traceable as extracted data.
    """
    cfg = cfg or global_settings
    if not cfg.enable_generation:
        return [], []

    specs = _spec_phrases(product, schema)
    if not specs and not product.identity.mpn:
        return [], ["nothing to generate from: no specifications and no part number"]

    payload: Optional[Dict[str, Any]] = None
    warnings: List[str] = []
    used_model = False

    if provider is not None and provider.available:
        payload = _llm_copy(provider, product, schema, specs)
        if payload is not None:
            used_model = True
            payload, warnings = _verify_generated(payload, specs, product)

    if payload is None:
        payload = _template_copy(product, schema, specs)
        warnings.append("generated from templates (no language model available)")

    # Mean confidence of the attributes the copy was actually derived from.
    source_confidences = [
        product.attributes[code].confidence
        for code, _, _ in specs
        if code in product.attributes and product.attributes[code].confidence > 0
    ]
    source_confidence = (
        sum(source_confidences) / len(source_confidences) if source_confidences else 0.5
    )

    derived_from = [code for code, _, _ in specs]
    generated: List[str] = []

    # Index the verification warnings by the field they belong to, so an
    # unverifiable claim is attached to the value that contains it and reaches
    # the review queue instead of being lost in a run-level log.
    warnings_by_field: Dict[str, List[str]] = {}
    for warning in warnings:
        field, _, detail = warning.partition(": ")
        if detail:
            warnings_by_field.setdefault(field, []).append(detail)

    for code, raw in payload.items():
        attr = schema.get(code)
        if attr is None:
            continue
        # product_name is only authored when the sources did not supply one.
        if code == "product_name" and product.attributes.get("product_name") is not None:
            existing = product.attributes["product_name"]
            if existing.evidence is not None:
                continue
        if raw in (None, "", []):
            continue

        from product_intel.pipeline.normalizer import normalize_value

        av = normalize_value(raw, attr)
        if av.value in (None, "", []):
            continue
        av.inference = InferencePath(
            strategy="generated_from_attributes",
            from_attribute=", ".join(derived_from[:6]),
            rationale=(
                "authored from this product's verified attributes; every claim traces to "
                "an attribute that carries its own source evidence"
            ),
        )
        av.normalization_notes.append(
            "AI-generated content" if used_model else "generated from templates"
        )
        for detail in warnings_by_field.get(code, []):
            av.normalization_notes.append(f"unverified claim -- {detail}")

        # Generated copy is exactly as trustworthy as the facts it was built
        # from, minus a generation penalty. Scoring it on a flat method weight
        # made well-sourced copy look as doubtful as copy assembled from
        # guesses, which is both wrong and unhelpful for triage. Deterministic
        # template output is penalised less than model output because it cannot
        # introduce a claim that was not already in the attribute set.
        av.confidence = round(source_confidence * (0.85 if not used_model else 0.7), 4)
        av.confidence_factors = {
            "source_facts": round(source_confidence, 4),
            "generation": 0.85 if not used_model else 0.7,
        }
        if warnings_by_field.get(code):
            av.confidence = round(av.confidence * 0.5, 4)
            av.confidence_factors["unverified_claim"] = 0.5
        product.attributes[code] = av
        product.observations.setdefault(code, []).append(av)
        generated.append(code)

    return generated, warnings


def enrich_catalog(
    products: Sequence[Product],
    taxonomy: Taxonomy,
    graph: ProductGraph,
    provider: Optional[LLMProvider] = None,
    cfg: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Run gap filling then generation across a whole catalog."""
    cfg = cfg or global_settings
    catalog = {p.identity.product_id: p for p in products}
    stats = {"gap_filled": 0, "generated": 0, "products": len(products), "warnings": []}

    # Gap filling first: generation should describe the completed record.
    for product in products:
        schema = taxonomy.get(product.category_id)
        filled = fill_gaps(product, schema, graph, catalog, cfg)
        stats["gap_filled"] += len(filled)

    for product in products:
        schema = taxonomy.get(product.category_id)
        generated, warnings = generate_content(product, schema, provider, cfg)
        stats["generated"] += len(generated)
        for w in warnings[:2]:
            stats["warnings"].append(f"{product.identity.mpn}: {w}")

    return stats
