"""
Domain model -> API view mapping.

Kept in one place so there is exactly one definition of what "origin" means,
how a value is rendered for display, and which pieces of provenance travel to
the client. Scattering that logic across route handlers is how a UI ends up
showing an inferred value as though someone had read it off a datasheet.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from product_intel.api.schemas import (
    AssetView,
    AttributeView,
    ConflictView,
    EvidenceView,
    InferenceView,
    ProductDetail,
    ProductSummary,
    QualityView,
    RelationView,
    ScorecardView,
    SourceView,
)
from product_intel.confidence import explain
from product_intel.graph import ProductGraph
from product_intel.models import (
    AttributeValue,
    ExtractionMethod,
    Product,
    ProductAsset,
    QualityScore,
)
from product_intel.schema.dictionary import AttributeDef, CategorySchema, Taxonomy


def render_value(av: AttributeValue, attr: Optional[AttributeDef]) -> str:
    """One rendering rule, used by the API, the exporters' spirit, and the UI."""
    if av.value is None:
        return ""
    if isinstance(av.value, list):
        return ", ".join(str(v) for v in av.value)
    if attr is not None and attr.is_numeric:
        try:
            body = f"{float(av.value):g}"
        except (TypeError, ValueError):
            body = str(av.value)
        return f"{body} {av.unit}".strip() if av.unit else body
    return str(av.value)


def classify_origin(av: AttributeValue, attr: Optional[AttributeDef]) -> str:
    """
    Where a value came from, as a single word the UI can colour by.

    Order matters: a human correction outranks everything, and a generated
    field must never be reported as sourced even though it carries an
    InferencePath rather than an Evidence.
    """
    if av.evidence is not None and av.evidence.method == ExtractionMethod.HUMAN:
        return "human"
    if attr is not None and attr.generated:
        return "generated"
    if av.inference is not None:
        strategy = av.inference.strategy
        if strategy.startswith("generated"):
            return "generated"
        return "inferred"
    if av.evidence is not None:
        if av.evidence.method == ExtractionMethod.INHERITED:
            return "inferred"
        return "sourced"
    if any("default applied" in n for n in av.normalization_notes):
        return "default"
    return "sourced"


def evidence_view(
    av: AttributeValue,
    source_names: Optional[Dict[str, str]] = None,
) -> Optional[EvidenceView]:
    if av.evidence is None:
        return None
    ev = av.evidence
    return EvidenceView(
        source_id=ev.source_id,
        source_kind=ev.source_kind.value if hasattr(ev.source_kind, "value") else str(ev.source_kind),
        source_name=(source_names or {}).get(ev.source_id),
        locator=ev.locator,
        page=ev.page,
        quote=ev.quote,
        method=ev.method.value if hasattr(ev.method, "value") else str(ev.method),
        quote_verified=bool(ev.quote_verified),
    )


def attribute_view(
    av: AttributeValue,
    schema: CategorySchema,
    product: Optional[Product] = None,
    source_names: Optional[Dict[str, str]] = None,
    product_names: Optional[Dict[str, str]] = None,
) -> AttributeView:
    attr = schema.get(av.code)
    inference = None
    if av.inference is not None:
        inference = InferenceView(
            strategy=av.inference.strategy,
            from_product_id=av.inference.from_product_id,
            from_product_mpn=(product_names or {}).get(av.inference.from_product_id or ""),
            from_attribute=av.inference.from_attribute,
            rationale=av.inference.rationale,
        )

    observations = 1
    if product is not None:
        observations = max(1, len(product.observations.get(av.code, [])))

    return AttributeView(
        code=av.code,
        name=attr.name if attr else av.code.replace("_", " ").title(),
        value=av.value,
        display=render_value(av, attr),
        unit=av.unit,
        raw_value=av.raw_value,
        datatype=attr.datatype if attr else "string",
        confidence=round(av.confidence, 4),
        confidence_factors=av.confidence_factors,
        confidence_reasons=explain(av),
        origin=classify_origin(av, attr),
        required_for=attr.required_for if attr else [],
        variant_defining=bool(attr and attr.variant_defining),
        evidence=evidence_view(av, source_names),
        inference=inference,
        normalization_notes=av.normalization_notes,
        validation_errors=av.validation_errors,
        observation_count=observations,
    )


def quality_view(q: QualityScore) -> QualityView:
    return QualityView(
        completeness_core=q.completeness_core,
        completeness_ecommerce=q.completeness_ecommerce,
        completeness_enhanced=q.completeness_enhanced,
        accuracy=q.accuracy,
        consistency=q.consistency,
        distinctiveness=q.distinctiveness,
        overall=q.overall,
        channel_ready=q.channel_ready,
        missing_required=q.missing_required,
    )


def product_summary(
    product: Product,
    taxonomy: Taxonomy,
    open_flags: int = 0,
    channel: str = "ecommerce",
) -> ProductSummary:
    schema = taxonomy.get(product.category_id)
    completeness = getattr(product.quality, f"completeness_{channel}", product.quality.completeness_ecommerce)
    return ProductSummary(
        product_id=product.identity.product_id,
        manufacturer=product.identity.manufacturer,
        mpn=product.identity.mpn,
        gtin=product.identity.gtin,
        series=product.identity.series,
        name=product.display_name(),
        category_id=product.category_id,
        category_name=schema.name,
        vertical=schema.vertical,
        status=product.status.value if hasattr(product.status, "value") else str(product.status),
        is_family=product.identity.base_product_id is None,
        quality_overall=round(product.quality.overall, 4),
        completeness=round(completeness, 4),
        channel_ready=product.quality.channel_ready,
        conflict_count=len(product.conflicts),
        attribute_count=len(product.attributes),
        source_count=len(product.source_ids),
        open_flags=open_flags,
        suspected_duplicate_of=getattr(product.identity, "suspected_duplicate_of", None),
    )


def product_detail(
    product: Product,
    taxonomy: Taxonomy,
    graph: Optional[ProductGraph] = None,
    assets: Optional[Sequence[ProductAsset]] = None,
    source_names: Optional[Dict[str, str]] = None,
    sources: Optional[Sequence[SourceView]] = None,
    product_names: Optional[Dict[str, str]] = None,
    open_flags: int = 0,
    channel: str = "ecommerce",
) -> ProductDetail:
    schema = taxonomy.get(product.category_id)
    base = product_summary(product, taxonomy, open_flags=open_flags, channel=channel)

    # Order attributes the way a merchandiser reads them: identity, then the
    # variant-defining specs a buyer filters on, then the rest, then generated
    # copy last since it is derived from everything above it.
    def sort_key(item):
        code, av = item
        attr = schema.get(code)
        origin = classify_origin(av, attr)
        if code in ("manufacturer", "mpn", "gtin", "series"):
            rank = 0
        elif origin == "generated":
            rank = 4
        elif attr is not None and attr.variant_defining:
            rank = 1
        elif attr is not None and attr.is_required("core"):
            rank = 2
        else:
            rank = 3
        return (rank, (attr.name if attr else code).lower())

    attributes = [
        attribute_view(av, schema, product, source_names, product_names)
        for _, av in sorted(product.attributes.items(), key=sort_key)
    ]

    conflicts = [
        ConflictView(
            code=c.code,
            name=(schema.get(c.code).name if schema.get(c.code) else c.code),
            winning_value=c.winning_value,
            winning_source=c.winning_source,
            losing_values=c.losing_values,
            resolution_rule=c.resolution_rule,
            severity=c.severity,
        )
        for c in product.conflicts
    ]

    relations: List[RelationView] = []
    if graph is not None:
        for edge in graph.out_edges(product.identity.product_id):
            label = (product_names or {}).get(edge.object_id) or edge.object_id
            if edge.object_id.startswith("cert:"):
                label = edge.object_id[5:]
            elif edge.object_id.startswith("src_"):
                label = (source_names or {}).get(edge.object_id, edge.object_id)
            relations.append(
                RelationView(
                    predicate=edge.predicate.value,
                    object_id=edge.object_id,
                    object_label=label,
                    confidence=edge.confidence,
                )
            )

    asset_views = [
        AssetView(
            asset_id=a.asset_id,
            relative_path=a.relative_path,
            width=a.width,
            height=a.height,
            shot_type=a.shot_type,
            background=a.background,
            alt_text=a.alt_text,
            channel_compliant=a.channel_compliant,
            compliance_notes=a.compliance_notes,
        )
        for a in (assets or [])
    ]

    return ProductDetail(
        **base.model_dump(),
        etim=schema.etim,
        unspsc=schema.unspsc,
        category_confidence=round(product.category_confidence, 4),
        attributes=attributes,
        conflicts=conflicts,
        quality=quality_view(product.quality),
        quality_before_enrichment=(
            quality_view(product.quality_before_enrichment)
            if product.quality_before_enrichment
            else None
        ),
        relations=relations,
        assets=asset_views,
        sources=list(sources or []),
        alternate_mpns=product.identity.alternate_mpns,
        duplicate_evidence=getattr(product.identity, "duplicate_evidence", None),
    )


def scorecard_view(card: Dict[str, Any]) -> ScorecardView:
    """Tolerant of missing keys so an older catalog still renders."""
    return ScorecardView(**{k: v for k, v in card.items() if k in ScorecardView.model_fields})
