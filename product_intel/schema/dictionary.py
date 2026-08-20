"""
The attribute dictionary and taxonomy loader.

This module is the contract that turns open-ended extraction into
schema-directed extraction. Instead of asking a model "find any specifications
on this page", the extractor asks "find these 22 named attributes, each with
this datatype, this unit family and these legal values -- and return null with
a reason for anything absent".

That single change is what fixes attribute-name chaos, gives completeness a
denominator, and makes two runs of the pipeline diffable.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent / "data"


class AttributeDef(BaseModel):
    code: str
    name: str
    datatype: str = "string"  # string | text | number | enum | boolean
    unit_family: Optional[str] = None
    canonical_unit: Optional[str] = None
    allowed_values: Optional[List[str]] = None
    cardinality: str = "single"  # single | multi
    required_for: List[str] = Field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    default: Optional[Any] = None
    identity: bool = False
    variant_defining: bool = False
    generated: bool = Field(
        default=False,
        description="Authored by the enrichment layer rather than read from a source.",
    )
    aliases: List[str] = Field(default_factory=list)
    etim: Optional[str] = None

    def is_required(self, channel: str) -> bool:
        """Channels are cumulative: enhanced implies ecommerce implies core."""
        order = ["core", "ecommerce", "enhanced"]
        if channel not in order:
            return "core" in self.required_for
        allowed = set(order[: order.index(channel) + 1])
        return bool(allowed.intersection(self.required_for))

    @property
    def is_numeric(self) -> bool:
        return self.datatype == "number"

    def alias_patterns(self) -> List[str]:
        """Alias surface forms, longest first so 'rated current' beats 'current'."""
        seen = {self.name.lower(), self.code.replace("_", " ")}
        seen.update(a.lower() for a in self.aliases)
        return sorted(seen, key=len, reverse=True)


class Rule(BaseModel):
    id: str
    type: str  # cross_attribute | implication
    expression: Optional[str] = None
    if_: Optional[str] = Field(default=None, alias="if")
    then_present: List[str] = Field(default_factory=list)
    then_min: Dict[str, float] = Field(default_factory=dict)
    then_max: Dict[str, float] = Field(default_factory=dict)
    message: str = ""

    model_config = {"populate_by_name": True}


class CategorySchema(BaseModel):
    id: str
    name: str
    vertical: str
    etim: Optional[str] = None
    unspsc: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    attributes: Dict[str, AttributeDef] = Field(default_factory=dict)
    rules: List[Rule] = Field(default_factory=list)

    def required_codes(self, channel: str) -> List[str]:
        return [c for c, a in self.attributes.items() if a.is_required(channel)]

    def extractable_codes(self, channel: str = "enhanced") -> List[str]:
        """
        Attributes an extractor should hunt for.

        Excludes generated attributes (authored downstream) and identity
        attributes. Identity is established by resolution, not extraction: a
        family datasheet mentions every sibling's part number, so extracting
        'mpn' from shared fragments would assign one variant's number to
        another and manufacture a conflict on the product's own primary key.
        """
        return [c for c, a in self.attributes.items() if not a.generated and not a.identity]

    def variant_defining_codes(self) -> List[str]:
        return [c for c, a in self.attributes.items() if a.variant_defining]

    def get(self, code: str) -> Optional[AttributeDef]:
        return self.attributes.get(code)


class Taxonomy(BaseModel):
    version: str
    categories: Dict[str, CategorySchema]
    verticals: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    def get(self, category_id: str) -> CategorySchema:
        if category_id in self.categories:
            return self.categories[category_id]
        return self.categories["industrial.generic"]

    def all_ids(self) -> List[str]:
        return list(self.categories.keys())

    def classify_by_keywords(self, text: str) -> tuple[str, float, Optional[str]]:
        """
        Deterministic first-pass classifier.

        Returns (category_id, confidence, matched_keyword). Scoring rewards longer,
        more specific keyword matches so 'miniature circuit breaker' outweighs a
        bare mention of 'breaker'.
        """
        lowered = text.lower()
        scores: Dict[str, float] = {}
        best_kw: Dict[str, str] = {}

        for cid, cat in self.categories.items():
            score = 0.0
            for kw in cat.keywords:
                if not kw:
                    continue
                hits = len(re.findall(rf"(?<!\w){re.escape(kw)}(?!\w)", lowered))
                if hits:
                    weight = len(kw.split()) * 1.5
                    score += hits * weight
                    if cid not in best_kw or len(kw) > len(best_kw[cid]):
                        best_kw[cid] = kw
            if score:
                scores[cid] = score

        if not scores:
            return "industrial.generic", 0.0, None

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        # Confidence reflects both absolute evidence and margin over the runner-up.
        margin = (top_score - runner_up) / top_score if top_score else 0.0
        volume = min(top_score / 12.0, 1.0)
        confidence = round(min(0.35 + 0.4 * margin + 0.35 * volume, 0.99), 3)
        return top_id, confidence, best_kw.get(top_id)


@lru_cache(maxsize=1)
def load_taxonomy() -> Taxonomy:
    with open(DATA_DIR / "taxonomy.json", "r", encoding="utf-8") as f:
        tax_raw = json.load(f)
    with open(DATA_DIR / "attribute_sets.json", "r", encoding="utf-8") as f:
        sets_raw = json.load(f)

    sets = sets_raw["sets"]
    common = {a["code"]: AttributeDef(**a) for a in sets["_common"]["attributes"]}

    categories: Dict[str, CategorySchema] = {}
    for cat in tax_raw["categories"]:
        set_name = cat["attribute_set"]
        spec = sets.get(set_name, {"attributes": [], "rules": []})

        # Common attributes first so category-specific definitions can override them.
        attrs: Dict[str, AttributeDef] = dict(common)
        for a in spec.get("attributes", []):
            attrs[a["code"]] = AttributeDef(**a)

        categories[cat["id"]] = CategorySchema(
            id=cat["id"],
            name=cat["name"],
            vertical=cat["vertical"],
            etim=cat.get("etim"),
            unspsc=cat.get("unspsc"),
            keywords=cat.get("keywords", []),
            attributes=attrs,
            rules=[Rule(**r) for r in spec.get("rules", [])],
        )

    return Taxonomy(
        version=tax_raw["version"],
        categories=categories,
        verticals=tax_raw.get("verticals", {}),
    )


@lru_cache(maxsize=1)
def load_units() -> Dict[str, Any]:
    with open(DATA_DIR / "units.json", "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def alias_index() -> Dict[str, List[str]]:
    """
    Reverse index: alias surface form -> attribute codes that claim it.

    Used by the deterministic extractor to resolve a spec-table row label
    ("Rated Current (In)") to a canonical attribute code without an LLM call.
    """
    index: Dict[str, List[str]] = {}
    tax = load_taxonomy()
    for cat in tax.categories.values():
        for code, attr in cat.attributes.items():
            for alias in attr.alias_patterns():
                index.setdefault(alias, [])
                if code not in index[alias]:
                    index[alias].append(code)
    return index
