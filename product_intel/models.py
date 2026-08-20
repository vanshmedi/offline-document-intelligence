"""
Core data model for the Product Intelligence Engine.

Design rule inherited from the predecessor project and kept deliberately:
a value cannot exist in this model without an evidence pointer. Every
AttributeValue carries the source document, the location inside it, and the
verbatim text that supports it -- or, if it was inferred rather than read,
an explicit inference path. There is no way to record a fact anonymously.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class SourceKind(str, Enum):
    """Where a piece of evidence came from. Ordered loosely by trust."""

    DATASHEET = "datasheet"
    CATALOG = "catalog"
    MANUFACTURER_WEB = "manufacturer_web"
    PRICE_FILE = "price_file"
    DISTRIBUTOR_WEB = "distributor_web"
    ERP_EXPORT = "erp_export"
    IMAGE = "image"
    USER = "user"
    INFERRED = "inferred"


class ExtractionMethod(str, Enum):
    """How a value was obtained. Feeds confidence scoring."""

    NATIVE_TABLE = "native_table"
    NATIVE_TEXT = "native_text"
    STRUCTURED_FEED = "structured_feed"
    OCR = "ocr"
    VISION = "vision"
    LLM = "llm"
    INHERITED = "inherited"
    GENERATED = "generated"
    HUMAN = "human"


#: Source precedence for golden-record arbitration. Higher wins.
SOURCE_PRECEDENCE: Dict[str, int] = {
    SourceKind.USER: 100,
    SourceKind.DATASHEET: 90,
    SourceKind.ERP_EXPORT: 80,
    SourceKind.CATALOG: 70,
    SourceKind.MANUFACTURER_WEB: 60,
    SourceKind.PRICE_FILE: 50,
    SourceKind.DISTRIBUTOR_WEB: 30,
    SourceKind.IMAGE: 25,
    SourceKind.INFERRED: 10,
}

#: Reliability weight per extraction method. Feeds confidence scoring.
METHOD_RELIABILITY: Dict[str, float] = {
    ExtractionMethod.HUMAN: 1.00,
    ExtractionMethod.STRUCTURED_FEED: 0.95,
    ExtractionMethod.NATIVE_TABLE: 0.90,
    ExtractionMethod.NATIVE_TEXT: 0.80,
    ExtractionMethod.LLM: 0.65,
    ExtractionMethod.VISION: 0.60,
    ExtractionMethod.OCR: 0.55,
    ExtractionMethod.INHERITED: 0.50,
    ExtractionMethod.GENERATED: 0.40,
}


class Evidence(BaseModel):
    """A pointer to the exact place a value was read from."""

    source_id: str = Field(description="ID of the SourceDocument this came from.")
    source_kind: SourceKind = SourceKind.DATASHEET
    locator: str = Field(
        description="Human-readable location, e.g. 'p.3 / Table 2 / row 4' or 'div.specs > tr:nth-child(7)'."
    )
    page: Optional[int] = None
    quote: str = Field(description="Verbatim supporting text, as it appears in the mirror.")
    method: ExtractionMethod = ExtractionMethod.NATIVE_TEXT
    quote_verified: bool = Field(
        default=False,
        description="True when the quote was located character-for-character in the canonical mirror.",
    )


class InferencePath(BaseModel):
    """Records how a value was derived when it was not read from a source."""

    strategy: str = Field(description="e.g. 'family_inheritance', 'variant_sibling', 'rule_default'.")
    from_product_id: Optional[str] = None
    from_attribute: Optional[str] = None
    rationale: str = ""


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


class AttributeValue(BaseModel):
    """A single observed value for one attribute, from one source."""

    code: str = Field(description="Attribute code from the attribute dictionary.")
    value: Any = Field(description="Normalized value: float, str, bool, or list for multi-valued.")
    raw_value: Optional[str] = Field(default=None, description="Surface form exactly as found.")
    unit: Optional[str] = Field(default=None, description="Canonical unit code after normalization.")
    raw_unit: Optional[str] = Field(default=None, description="Unit as written in the source.")
    evidence: Optional[Evidence] = None
    inference: Optional[InferencePath] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_factors: Dict[str, float] = Field(default_factory=dict)
    normalization_notes: List[str] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def is_grounded(self) -> bool:
        """A value is grounded if it points at real evidence or a declared inference."""
        return self.evidence is not None or self.inference is not None

    def display(self) -> str:
        if isinstance(self.value, list):
            body = ", ".join(str(v) for v in self.value)
        else:
            body = str(self.value)
        return f"{body} {self.unit}".strip() if self.unit else body


class AttributeConflict(BaseModel):
    """Two or more sources disagreed. Both the winner and the losers are kept."""

    code: str
    winning_value: Any
    winning_source: str
    losing_values: List[Dict[str, Any]] = Field(default_factory=list)
    resolution_rule: str = ""
    severity: str = Field(default="warning", description="info | warning | critical")
    resolved: bool = True


# ---------------------------------------------------------------------------
# Sources and products
# ---------------------------------------------------------------------------


class SourceDocument(BaseModel):
    """A single ingested file or page, with its processing state."""

    source_id: str
    filename: str
    kind: SourceKind
    content_type: str = Field(description="pdf | html | csv | xlsx | image | xml")
    checksum: str
    relative_path: str
    page_count: Optional[int] = None
    mirror_path: Optional[str] = Field(default=None, description="Path to the canonical Markdown mirror.")
    manufacturer_hint: Optional[str] = None
    ingested_at: str = Field(default_factory=_utcnow)
    fragment_count: int = 0
    asset_count: int = 0


class ProductIdentity(BaseModel):
    """The keys that make a product one product across many documents."""

    product_id: str = Field(description="Deterministic: sha256(manufacturer|normalized_mpn)[:16].")
    manufacturer: str
    mpn: str
    normalized_mpn: str
    gtin: Optional[str] = None
    alternate_mpns: List[str] = Field(default_factory=list)
    base_product_id: Optional[str] = Field(default=None, description="Set when this SKU is a variant.")
    series: Optional[str] = None
    suspected_duplicate_of: Optional[str] = Field(
        default=None,
        description="Set when this part number is a near-match for an existing one. "
                    "Never merged automatically -- a human confirms or dismisses it.",
    )
    duplicate_evidence: Optional[str] = None


class QualityScore(BaseModel):
    """Four-axis quality measurement for one product."""

    completeness_core: float = 0.0
    completeness_ecommerce: float = 0.0
    completeness_enhanced: float = 0.0
    accuracy: float = Field(default=0.0, description="Share of attributes with verified quote-level evidence.")
    consistency: float = Field(default=0.0, description="Share of applicable rules passed.")
    distinctiveness: float = Field(default=0.0, description="Inverse of peer-group outlier rate.")
    overall: float = 0.0
    channel_ready: bool = False
    missing_required: List[str] = Field(default_factory=list)
    computed_at: str = Field(default_factory=_utcnow)


class ReviewFlag(BaseModel):
    """An item queued for a human."""

    flag_id: str
    product_id: str
    attribute_code: Optional[str] = None
    reason: str
    severity: str = "warning"
    confidence: float = 1.0
    suggested_value: Any = None
    created_at: str = Field(default_factory=_utcnow)
    resolved: bool = False
    resolution: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None


class ProductStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    ENRICHED = "enriched"
    PUBLISHED = "published"
    FAILED = "failed"


class Product(BaseModel):
    """
    The golden record. One product, assembled from every source that mentions it.

    `observations` holds every value ever seen, per attribute, from every source.
    `attributes` holds the single arbitrated winner per attribute. Keeping both is
    what makes conflict resolution explainable after the fact.
    """

    identity: ProductIdentity
    category_id: str = "industrial.generic"
    category_confidence: float = 0.0
    category_evidence: Optional[str] = None
    status: ProductStatus = ProductStatus.DRAFT

    attributes: Dict[str, AttributeValue] = Field(default_factory=dict)
    observations: Dict[str, List[AttributeValue]] = Field(default_factory=dict)
    conflicts: List[AttributeConflict] = Field(default_factory=list)

    is_family: bool = Field(
        default=False,
        description="A series/family record, not a sellable SKU. It exists so variants "
                    "have one authoritative home for shared attributes to inherit from, "
                    "and is exempt from variant-defining requirements and channel readiness.",
    )

    source_ids: List[str] = Field(default_factory=list)
    variant_ids: List[str] = Field(default_factory=list)
    asset_ids: List[str] = Field(default_factory=list)

    quality: QualityScore = Field(default_factory=QualityScore)
    quality_before_enrichment: Optional[QualityScore] = None

    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)

    def get(self, code: str, default: Any = None) -> Any:
        av = self.attributes.get(code)
        return av.value if av is not None else default

    def display_name(self) -> str:
        return str(self.get("product_name") or f"{self.identity.manufacturer} {self.identity.mpn}")


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class ProductAsset(BaseModel):
    """A digital asset (image / drawing) attached to a product."""

    asset_id: str
    product_id: Optional[str] = None
    source_id: str
    relative_path: str
    width: int = 0
    height: int = 0
    shot_type: str = Field(default="unknown", description="hero | angle | line_drawing | dimension | lifestyle | unknown")
    background: str = "unknown"
    alt_text: Optional[str] = None
    channel_compliant: bool = False
    compliance_notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class RelationType(str, Enum):
    VARIANT_OF = "variant_of"
    ACCESSORY_FOR = "accessory_for"
    SPARE_PART_OF = "spare_part_of"
    COMPATIBLE_WITH = "compatible_with"
    REPLACES = "replaces"
    REPLACED_BY = "replaced_by"
    CERTIFIED_BY = "certified_by"
    BELONGS_TO = "belongs_to"
    DOCUMENTED_IN = "documented_in"


class Relation(BaseModel):
    """A provenance-bearing edge in the product knowledge graph."""

    subject_id: str
    predicate: RelationType
    object_id: str
    evidence: Optional[Evidence] = None
    confidence: float = 1.0
    created_at: str = Field(default_factory=_utcnow)

    def key(self) -> str:
        return f"{self.subject_id}|{self.predicate.value}|{self.object_id}"


# ---------------------------------------------------------------------------
# Pipeline bookkeeping
# ---------------------------------------------------------------------------


class StepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuditStep(BaseModel):
    step_name: str
    status: StepStatus
    started_at: str
    completed_at: str
    duration_ms: int = 0
    error_message: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)


class AuditLog(BaseModel):
    subject_id: str = Field(description="source_id for ingest runs, product_id for product runs.")
    checksum: Optional[str] = None
    pipeline_version: str = "2.0"
    schema_version: str = "1.0.0"
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    embedding_model: Optional[str] = None
    status: str = "pending"
    steps: List[AuditStep] = Field(default_factory=list)
    validation_errors: List[str] = Field(default_factory=list)


class ManifestEntry(BaseModel):
    """Per-source processing state. Drives all incremental behaviour."""

    source_id: str
    filename: str
    relative_path: str
    checksum: str
    content_type: str
    kind: SourceKind = SourceKind.DATASHEET
    manufacturer_hint: Optional[str] = None
    status: str = "pending"
    steps: Dict[str, str] = Field(default_factory=dict)
    product_ids: List[str] = Field(default_factory=list)
    parser_version: str = "2.0"
    schema_version: str = "1.0.0"
    embedding_model: Optional[str] = None
    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)


class Fragment(BaseModel):
    """
    A provenance-tagged unit of content produced by a parser.

    Every downstream extractor consumes Fragments, never raw text, so a value can
    always be traced back to a locator without re-parsing the source.
    """

    fragment_id: str
    source_id: str
    kind: str = Field(description="text | table | keyvalue | image_caption | heading")
    page: Optional[int] = None
    locator: str = ""
    text: str = ""
    table: Optional[List[List[str]]] = None
    method: ExtractionMethod = ExtractionMethod.NATIVE_TEXT
    metadata: Dict[str, Any] = Field(default_factory=dict)
