"""
API response shapes.

Deliberately separate from the domain models in `product_intel.models`. The
domain model is the record of truth and carries everything; the API view is
what a client needs to render, flattened and pre-joined so the front end never
has to understand golden-record internals.

The one thing every attribute view carries is its provenance. A client that
renders an attribute without being able to show where it came from would defeat
the point of the whole system, so `origin` and `evidence` are not optional
extras here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class EvidenceView(BaseModel):
    source_id: str
    source_kind: str
    source_name: Optional[str] = None
    locator: str
    page: Optional[int] = None
    quote: str
    method: str
    quote_verified: bool


class InferenceView(BaseModel):
    strategy: str
    from_product_id: Optional[str] = None
    from_product_mpn: Optional[str] = None
    from_attribute: Optional[str] = None
    rationale: str = ""


class AttributeView(BaseModel):
    code: str
    name: str
    value: Any
    display: str
    unit: Optional[str] = None
    raw_value: Optional[str] = None
    datatype: str = "string"
    confidence: float = 0.0
    confidence_factors: Dict[str, float] = Field(default_factory=dict)
    confidence_reasons: List[str] = Field(default_factory=list)
    #: How this value came to exist. The front end colours by this.
    origin: Literal["sourced", "inferred", "generated", "human", "default"] = "sourced"
    required_for: List[str] = Field(default_factory=list)
    variant_defining: bool = False
    evidence: Optional[EvidenceView] = None
    inference: Optional[InferenceView] = None
    normalization_notes: List[str] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)
    observation_count: int = 1


class ConflictView(BaseModel):
    code: str
    name: str
    winning_value: Any
    winning_source: str
    losing_values: List[Dict[str, Any]] = Field(default_factory=list)
    resolution_rule: str
    severity: str


class QualityView(BaseModel):
    completeness_core: float = 0.0
    completeness_ecommerce: float = 0.0
    completeness_enhanced: float = 0.0
    accuracy: float = 0.0
    consistency: float = 0.0
    distinctiveness: float = 0.0
    overall: float = 0.0
    channel_ready: bool = False
    missing_required: List[str] = Field(default_factory=list)


class RelationView(BaseModel):
    predicate: str
    object_id: str
    object_label: str
    confidence: float = 1.0


class AssetView(BaseModel):
    asset_id: str
    relative_path: str
    width: int = 0
    height: int = 0
    shot_type: str = "unknown"
    background: str = "unknown"
    alt_text: Optional[str] = None
    channel_compliant: bool = False
    compliance_notes: List[str] = Field(default_factory=list)


class ProductSummary(BaseModel):
    product_id: str
    manufacturer: str
    mpn: str
    gtin: Optional[str] = None
    series: Optional[str] = None
    name: str
    category_id: str
    category_name: str
    vertical: str
    status: str
    is_family: bool = False
    quality_overall: float = 0.0
    completeness: float = 0.0
    channel_ready: bool = False
    conflict_count: int = 0
    attribute_count: int = 0
    source_count: int = 0
    open_flags: int = 0
    suspected_duplicate_of: Optional[str] = None


class SourceView(BaseModel):
    source_id: str
    filename: str
    kind: str
    content_type: str
    page_count: Optional[int] = None


class ProductDetail(ProductSummary):
    etim: Optional[str] = None
    unspsc: Optional[str] = None
    category_confidence: float = 0.0
    attributes: List[AttributeView] = Field(default_factory=list)
    conflicts: List[ConflictView] = Field(default_factory=list)
    quality: QualityView = Field(default_factory=QualityView)
    quality_before_enrichment: Optional[QualityView] = None
    relations: List[RelationView] = Field(default_factory=list)
    assets: List[AssetView] = Field(default_factory=list)
    sources: List[SourceView] = Field(default_factory=list)
    alternate_mpns: List[str] = Field(default_factory=list)
    duplicate_evidence: Optional[str] = None


class ScorecardView(BaseModel):
    products: int = 0
    sellable: int = 0
    families: int = 0
    completeness_core: float = 0.0
    completeness_ecommerce: float = 0.0
    completeness_enhanced: float = 0.0
    accuracy: float = 0.0
    consistency: float = 0.0
    distinctiveness: float = 0.0
    overall: float = 0.0
    channel_ready: int = 0
    channel_ready_pct: float = 0.0
    conflicts: int = 0
    attributes_total: int = 0
    attributes_sourced: int = 0
    attributes_generated: int = 0
    inferred_attributes: int = 0
    confidence_sourced: float = 0.0
    confidence_generated: float = 0.0


class CatalogOverview(BaseModel):
    scorecard: ScorecardView
    scorecard_before: Optional[ScorecardView] = None
    by_category: List[Dict[str, Any]] = Field(default_factory=list)
    by_manufacturer: List[Dict[str, Any]] = Field(default_factory=list)
    by_status: Dict[str, int] = Field(default_factory=dict)
    review: Dict[str, Any] = Field(default_factory=dict)
    graph: Dict[str, Any] = Field(default_factory=dict)
    sources: int = 0
    attribute_coverage: List[Dict[str, Any]] = Field(default_factory=list)
    catalog_built: bool = False


class FlagView(BaseModel):
    flag_id: str
    product_id: str
    product_mpn: str
    product_name: str
    attribute_code: Optional[str] = None
    attribute_name: Optional[str] = None
    reason: str
    reason_kind: str
    severity: str
    confidence: float
    suggested_value: Any = None
    current_value: Any = None
    allowed_values: Optional[List[str]] = None
    datatype: str = "string"
    unit: Optional[str] = None
    evidence: Optional[EvidenceView] = None
    created_at: str


class CorrectionRequest(BaseModel):
    product_id: str
    code: str
    value: Any
    reviewer: str = "reviewer"
    note: str = ""
    flag_id: Optional[str] = None


class CorrectionResponse(BaseModel):
    ok: bool
    product_id: str
    code: str
    applied_value: Any
    confidence: float
    validation_errors: List[str] = Field(default_factory=list)
    learned_rule: Optional[str] = None
    quality: QualityView


class FlagResolveRequest(BaseModel):
    resolution: str = "accepted"
    reviewer: str = "reviewer"


class SearchHit(BaseModel):
    product: ProductSummary
    score: float
    match_kind: Literal["semantic", "attribute", "identity"] = "semantic"
    matched_on: List[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    hits: List[SearchHit] = Field(default_factory=list)
    total: int = 0
    semantic_available: bool = False
    filters_applied: Dict[str, Any] = Field(default_factory=dict)


class LLMStatus(BaseModel):
    provider: str
    mode: Literal["offline", "cloud", "off"]
    model: Optional[str] = None
    enabled: bool
    available: bool
    detail: str = ""
    remediation: List[str] = Field(default_factory=list)
    region: Optional[str] = None
    credential_source: Optional[str] = None
    endpoint: Optional[str] = None
    suggested_models: List[Dict[str, str]] = Field(default_factory=list)
    env_shadowing: List[str] = Field(default_factory=list)


class LLMSwitchRequest(BaseModel):
    provider: Literal["ollama", "bedrock", "off"]
    model: Optional[str] = None
    region: Optional[str] = None
    profile: Optional[str] = None


class JobStatus(BaseModel):
    job_id: str
    kind: str
    state: Literal["queued", "running", "done", "failed"]
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    progress: float = 0.0
    message: str = ""
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    log: List[str] = Field(default_factory=list)


class ExportRequest(BaseModel):
    fmt: Literal["json", "csv", "bmecat", "gdsn"]
    ready_only: bool = False


class SchemaCategoryView(BaseModel):
    id: str
    name: str
    vertical: str
    etim: Optional[str] = None
    unspsc: Optional[str] = None
    attribute_count: int
    required_core: int
    required_ecommerce: int
    rules: int
    product_count: int = 0
