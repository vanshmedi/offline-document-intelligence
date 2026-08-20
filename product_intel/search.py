"""
Catalog search and grounded question answering.

Two retrieval paths, chosen by what the question is:

  structured -- attribute filters and aggregates run as parameterised SQL over
                DuckDB. "3-pole breakers rated 10 kA or better" is a WHERE
                clause, not a similarity search, and answering it by embedding
                distance gives approximately-right results, which for a
                purchasing decision is wrong.

  semantic   -- free-text description matching over product vectors, for the
                questions that are genuinely fuzzy ("something for isolating a
                steam line").

The answering layer never generates a fact. It reports attribute values, each
with the source, locator and quote behind it. If the catalog does not contain
the answer, it says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from product_intel.config import Settings, settings as global_settings
from product_intel.models import Product
from product_intel.pipeline.db_ingest import CatalogDB
from product_intel.pipeline.normalizer import normalize_enum, parse_number
from product_intel.schema.dictionary import CategorySchema, Taxonomy, load_taxonomy

log = logging.getLogger(__name__)

_COMPARATOR_WORDS = [
    (r"\b(?:at least|minimum|min\.?|no less than|or (?:more|higher|greater|better)|>=)\b", ">="),
    (r"\b(?:at most|maximum|max\.?|no more than|up to|or (?:less|lower|fewer)|<=)\b", "<="),
    (r"\b(?:greater than|more than|above|over|>)\b", ">"),
    (r"\b(?:less than|below|under|<)\b", "<"),
    (r"\b(?:exactly|equal to|=)\b", "="),
]


@dataclass
class SearchHit:
    product: Product
    score: float = 0.0
    matched_on: List[str] = field(default_factory=list)


@dataclass
class QueryPlan:
    """How a natural-language query was interpreted. Logged for auditability."""

    mode: str = "semantic"  # structured | semantic | hybrid
    category_id: Optional[str] = None
    manufacturer: Optional[str] = None
    filters: List[Tuple[str, str, Any]] = field(default_factory=list)
    text: str = ""
    explanation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "category_id": self.category_id,
            "manufacturer": self.manufacturer,
            "filters": [f"{c} {op} {v}" for c, op, v in self.filters],
            "explanation": self.explanation,
        }


class CatalogSearch:
    def __init__(self, cfg: Optional[Settings] = None, taxonomy: Optional[Taxonomy] = None) -> None:
        self.cfg = cfg or global_settings
        self.taxonomy = taxonomy or load_taxonomy()
        self.db = CatalogDB(self.cfg)

    # -- query planning -----------------------------------------------------

    def plan(self, query: str, products: Optional[Sequence[Product]] = None) -> QueryPlan:
        """
        Parse a query into filters without a language model.

        Attribute aliases and unit tokens are already in the schema, so a query
        like "3 pole breakers at least 10kA" resolves deterministically. This
        is faster, free, reproducible and -- unlike an LLM writing raw SQL --
        cannot invent a column or a value that does not exist.
        """
        plan = QueryPlan(text=query)
        lowered = query.lower()

        cid, confidence, keyword = self.taxonomy.classify_by_keywords(query)
        if confidence >= 0.5:
            plan.category_id = cid
            plan.explanation = f"category '{self.taxonomy.get(cid).name}' from '{keyword}'"

        if products:
            for product in products:
                mfr = product.identity.manufacturer
                if mfr and mfr.lower() in lowered:
                    plan.manufacturer = mfr
                    break

        schema = self.taxonomy.get(plan.category_id) if plan.category_id else None
        candidates = (
            list(schema.attributes.values())
            if schema
            else [a for c in self.taxonomy.categories.values() for a in c.attributes.values()]
        )

        seen: set = set()
        for attr in candidates:
            if attr.code in seen or attr.generated or attr.identity:
                continue
            for alias in attr.alias_patterns():
                if len(alias) < 2:
                    continue
                match = re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered)
                if not match:
                    continue

                window = lowered[max(0, match.start() - 30) : match.end() + 40]
                operator = "="
                for pattern, op in _COMPARATOR_WORDS:
                    if re.search(pattern, window):
                        operator = op
                        break

                if attr.datatype == "number":
                    from product_intel.pipeline.normalizer import normalize_value

                    number_match = re.search(
                        rf"([-+]?[\d.,/ ]*\d)\s*([a-zA-Z°\"']*)\s*{re.escape(alias)}"
                        rf"|{re.escape(alias)}\D{{0,12}}?([-+]?[\d.,/ ]*\d)\s*([a-zA-Z°\"']*)",
                        lowered,
                    )
                    if not number_match:
                        continue
                    # Normalize the whole matched span, not just the digits.
                    # Many attribute aliases *are* unit tokens ('cfm', 'psi',
                    # 'amps'), so isolating the number discards the unit and the
                    # value is then wrongly assumed to already be canonical --
                    # '1000 CFM' would filter as 1000 m3/h rather than 1699.
                    span = number_match.group(0)
                    if not any(c.isdigit() for c in span):
                        continue
                    av = normalize_value(span, attr)
                    if av.value is None:
                        continue
                    plan.filters.append((attr.code, operator, av.value))
                    seen.add(attr.code)
                    break

                if attr.datatype == "enum" and attr.allowed_values:
                    for allowed in attr.allowed_values:
                        token = allowed.lower()
                        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", lowered):
                            plan.filters.append((attr.code, "=", allowed))
                            seen.add(attr.code)
                            break
                    else:
                        window_value = lowered[match.end() : match.end() + 30].strip(" :=")
                        value, _note = normalize_enum(window_value.split()[0] if window_value.split() else "",
                                                      attr.allowed_values)
                        if value:
                            plan.filters.append((attr.code, "=", value))
                            seen.add(attr.code)
                    break

        # Enum values named without their attribute label ("full port brass").
        if schema:
            for attr in schema.attributes.values():
                if attr.code in seen or attr.datatype != "enum" or not attr.allowed_values:
                    continue
                for allowed in attr.allowed_values:
                    if len(allowed) < 3:
                        continue
                    if re.search(rf"(?<![a-z]){re.escape(allowed.lower())}(?![a-z])", lowered):
                        plan.filters.append((attr.code, "=", allowed))
                        seen.add(attr.code)
                        break

        # Bare quantities bound by unit ("at least 30A", "1200 CFM", "150 psi").
        # Trade buyers rarely name the attribute -- they say the number and the
        # unit and expect the system to know which spec that is. Within a known
        # category the unit is usually unambiguous, so it can be resolved
        # deterministically.
        if schema:
            self._bind_bare_quantities(query, schema, plan, seen)

        if plan.filters or plan.category_id or plan.manufacturer:
            plan.mode = "structured"
            bits = []
            if plan.category_id:
                bits.append(f"category={self.taxonomy.get(plan.category_id).name}")
            if plan.manufacturer:
                bits.append(f"manufacturer={plan.manufacturer}")
            bits += [f"{c} {op} {v}" for c, op, v in plan.filters]
            plan.explanation = "resolved to " + ", ".join(bits)
        else:
            plan.mode = "semantic"
            plan.explanation = "no attribute filters resolved; falling back to semantic similarity"

        return plan

    def _bind_bare_quantities(
        self,
        query: str,
        schema: CategorySchema,
        plan: QueryPlan,
        seen: set,
    ) -> None:
        """Resolve '30A' / '1200 CFM' to the category attribute using that unit."""
        from product_intel.pipeline.normalizer import detect_unit, normalize_value

        lowered = query.lower()
        for match in re.finditer(r"([-+]?\d[\d.,/]*)\s*([a-zA-Z°\"']{1,8})", lowered):
            number, unit_token = match.group(1), match.group(2)
            detected = detect_unit(unit_token, None)
            if detected is None:
                continue
            family, unit_code, _spec = detected

            candidates = [
                a for a in schema.attributes.values()
                if a.unit_family == family and a.code not in seen and not a.generated
            ]
            if not candidates:
                continue
            # Prefer the attribute a buyer would actually be filtering on.
            candidates.sort(
                key=lambda a: (a.variant_defining, a.is_required("core"), a.is_required("ecommerce")),
                reverse=True,
            )
            attr = candidates[0]

            window = lowered[max(0, match.start() - 30) : match.end() + 10]
            operator = "="
            for pattern, op in _COMPARATOR_WORDS:
                if re.search(pattern, window):
                    operator = op
                    break

            av = normalize_value(f"{number} {unit_token}", attr)
            if av.value is None:
                continue
            plan.filters.append((attr.code, operator, av.value))
            seen.add(attr.code)

    # -- execution ----------------------------------------------------------

    def structured(self, plan: QueryPlan, limit: int = 25) -> List[Dict[str, Any]]:
        """
        Execute a plan as parameterised SQL.

        Values are always bound, never interpolated -- the predecessor built its
        SQL by string-formatting user-derived text into the statement.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if plan.category_id:
            clauses.append("p.category_id = ?")
            params.append(plan.category_id)
        if plan.manufacturer:
            clauses.append("lower(p.manufacturer) = lower(?)")
            params.append(plan.manufacturer)

        for code, operator, value in plan.filters:
            column = "a.value_number" if isinstance(value, (int, float)) and not isinstance(value, bool) else "a.value_text"
            sql_op = "=" if operator == "=" else operator
            if column == "a.value_text":
                clauses.append(
                    f"p.product_id IN (SELECT product_id FROM attributes "
                    f"WHERE code = ? AND lower(value_text) {sql_op} lower(?))"
                )
            else:
                clauses.append(
                    f"p.product_id IN (SELECT product_id FROM attributes "
                    f"WHERE code = ? AND value_number {sql_op} ?)"
                )
            params.extend([code, value])

        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            "SELECT p.product_id, p.manufacturer, p.mpn, p.product_name, p.category_id, "
            "p.quality_overall, p.channel_ready "
            f"FROM products p WHERE {where} "
            "ORDER BY p.quality_overall DESC, p.mpn "
            f"LIMIT {int(limit)}"
        )
        return self.db.query(sql, params)

    def semantic(self, query: str, limit: int = 10) -> List[Tuple[str, float]]:
        from product_intel.pipeline.embedder import VectorIndex, embed_texts

        index = VectorIndex.load(self.cfg)
        if index.size() == 0:
            return []
        vectors, _ = embed_texts([query], self.cfg, cache=None)
        if not vectors or not vectors[0]:
            return []
        return index.search(vectors[0], k=limit)

    def search(
        self,
        query: str,
        products: Sequence[Product],
        limit: int = 10,
    ) -> Tuple[List[SearchHit], QueryPlan]:
        by_id = {p.identity.product_id: p for p in products}
        plan = self.plan(query, products)
        hits: List[SearchHit] = []

        if plan.mode == "structured":
            try:
                rows = self.structured(plan, limit=limit)
                for row in rows:
                    product = by_id.get(row["product_id"])
                    if product is not None:
                        hits.append(
                            SearchHit(
                                product=product,
                                score=float(row.get("quality_overall") or 0),
                                matched_on=[f"{c} {op} {v}" for c, op, v in plan.filters] or ["category"],
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("structured search failed, falling back to semantic: %s", exc)

        if not hits:
            plan.mode = "semantic" if plan.mode == "structured" else plan.mode
            for pid, score in self.semantic(query, limit=limit):
                product = by_id.get(pid)
                if product is not None:
                    hits.append(SearchHit(product=product, score=score, matched_on=["semantic similarity"]))

        return hits[:limit], plan


def answer(
    query: str,
    products: Sequence[Product],
    taxonomy: Optional[Taxonomy] = None,
    cfg: Optional[Settings] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """
    Answer a catalog question with cited attribute values.

    No generation. Every line of the answer is a value that exists in the
    catalog, presented with the source it came from. "The catalog does not
    contain this" is a correct answer and is returned as such.
    """
    taxonomy = taxonomy or load_taxonomy()
    search = CatalogSearch(cfg, taxonomy)
    hits, plan = search.search(query, products, limit=limit)

    if not hits:
        return {
            "query": query,
            "plan": plan.as_dict(),
            "answer": "No products in the catalog match this query.",
            "results": [],
            "citations": [],
        }

    results: List[Dict[str, Any]] = []
    citations: List[Dict[str, Any]] = []
    filter_codes = [c for c, _, _ in plan.filters]

    for hit in hits:
        product = hit.product
        schema = taxonomy.get(product.category_id)

        shown: Dict[str, Any] = {}
        # Always show what the buyer filtered on, plus the attributes that
        # distinguish this product from its siblings -- those are the ones that
        # make a selection decision.
        variant_codes = [c for c, a in schema.attributes.items() if a.variant_defining]
        codes = list(dict.fromkeys(filter_codes + variant_codes))[:8] or [
            c for c, a in schema.attributes.items() if a.is_required("core")
        ][:6]

        for code in codes:
            av = product.attributes.get(code)
            if av is None or av.value in (None, "", []):
                continue
            attr = schema.get(code)
            shown[attr.name if attr else code] = av.display()
            if av.evidence is not None:
                citations.append(
                    {
                        "product": product.identity.mpn,
                        "attribute": attr.name if attr else code,
                        "value": av.display(),
                        "source_id": av.evidence.source_id,
                        "locator": av.evidence.locator,
                        "page": av.evidence.page,
                        "quote": av.evidence.quote,
                        "verified": av.evidence.quote_verified,
                        "confidence": av.confidence,
                    }
                )
            elif av.inference is not None:
                citations.append(
                    {
                        "product": product.identity.mpn,
                        "attribute": attr.name if attr else code,
                        "value": av.display(),
                        "source_id": "inferred",
                        "locator": av.inference.strategy,
                        "page": None,
                        "quote": av.inference.rationale,
                        "verified": False,
                        "confidence": av.confidence,
                    }
                )

        results.append(
            {
                "product_id": product.identity.product_id,
                "manufacturer": product.identity.manufacturer,
                "mpn": product.identity.mpn,
                "name": product.display_name(),
                "category": schema.name,
                "status": product.status.value if hasattr(product.status, "value") else str(product.status),
                "quality": round(product.quality.overall, 3),
                "attributes": shown,
                "score": round(hit.score, 4),
            }
        )

    return {
        "query": query,
        "plan": plan.as_dict(),
        "answer": f"{len(results)} product(s) match. Every value below is shown with the source it was read from.",
        "results": results,
        "citations": citations,
    }
