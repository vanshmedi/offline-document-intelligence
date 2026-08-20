"""
Product knowledge graph.

Product intelligence is relational in a way that a flat attribute table cannot
express. The questions Unilog's customers actually ask -- "what replaces the
discontinued X-200?", "which accessories fit this?", "show me every ATEX-rated
valve in this size" -- are traversals, not row lookups.

Deliberately dependency-free: an adjacency structure over the existing artifact
store, persisted as JSON. Swapping in Neo4j or Kuzu later means reimplementing
this one class, not touching callers.

Edges carry provenance too. An inherited attribute is only defensible if the
edge it was inherited across can itself be explained.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from product_intel.models import Evidence, Product, Relation, RelationType

log = logging.getLogger(__name__)

#: Edges that are meaningful in both directions.
_SYMMETRIC = {RelationType.COMPATIBLE_WITH}

#: Edges whose inverse is a different, also-meaningful relation.
_INVERSE = {
    RelationType.VARIANT_OF: None,
    RelationType.REPLACES: RelationType.REPLACED_BY,
    RelationType.REPLACED_BY: RelationType.REPLACES,
}


class ProductGraph:
    def __init__(self) -> None:
        self._out: Dict[str, List[Relation]] = defaultdict(list)
        self._in: Dict[str, List[Relation]] = defaultdict(list)
        self._keys: Set[str] = set()

    # -- mutation -----------------------------------------------------------

    def add(
        self,
        subject_id: str,
        predicate: RelationType,
        object_id: str,
        evidence: Optional[Evidence] = None,
        confidence: float = 1.0,
    ) -> bool:
        """Add an edge. Returns False if it already existed."""
        if subject_id == object_id:
            return False
        rel = Relation(
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            evidence=evidence,
            confidence=confidence,
        )
        if rel.key() in self._keys:
            return False
        self._keys.add(rel.key())
        self._out[subject_id].append(rel)
        self._in[object_id].append(rel)

        if predicate in _SYMMETRIC:
            mirror = Relation(
                subject_id=object_id,
                predicate=predicate,
                object_id=subject_id,
                evidence=evidence,
                confidence=confidence,
            )
            if mirror.key() not in self._keys:
                self._keys.add(mirror.key())
                self._out[object_id].append(mirror)
                self._in[subject_id].append(mirror)

        inverse = _INVERSE.get(predicate)
        if inverse is not None:
            inv = Relation(
                subject_id=object_id,
                predicate=inverse,
                object_id=subject_id,
                evidence=evidence,
                confidence=confidence,
            )
            if inv.key() not in self._keys:
                self._keys.add(inv.key())
                self._out[object_id].append(inv)
                self._in[subject_id].append(inv)

        return True

    # -- queries ------------------------------------------------------------

    def out_edges(self, node_id: str, predicate: Optional[RelationType] = None) -> List[Relation]:
        edges = self._out.get(node_id, [])
        return [e for e in edges if predicate is None or e.predicate == predicate]

    def in_edges(self, node_id: str, predicate: Optional[RelationType] = None) -> List[Relation]:
        edges = self._in.get(node_id, [])
        return [e for e in edges if predicate is None or e.predicate == predicate]

    def neighbours(self, node_id: str, predicate: Optional[RelationType] = None) -> List[str]:
        return [e.object_id for e in self.out_edges(node_id, predicate)]

    def base_of(self, product_id: str) -> Optional[str]:
        edges = self.out_edges(product_id, RelationType.VARIANT_OF)
        return edges[0].object_id if edges else None

    def variants_of(self, base_id: str) -> List[str]:
        return [e.subject_id for e in self.in_edges(base_id, RelationType.VARIANT_OF)]

    def family(self, product_id: str) -> List[str]:
        """Every product sharing a base with this one, plus the base itself."""
        base = self.base_of(product_id)
        if base is None:
            siblings = self.variants_of(product_id)
            return [product_id] + siblings if siblings else [product_id]
        return [base] + [v for v in self.variants_of(base)]

    def siblings(self, product_id: str) -> List[str]:
        return [p for p in self.family(product_id) if p != product_id]

    def traverse(
        self,
        start: str,
        predicates: Sequence[RelationType],
        max_depth: int = 3,
    ) -> List[Tuple[str, int, List[RelationType]]]:
        """Breadth-first walk. Returns (node_id, depth, path_of_predicates)."""
        seen = {start}
        queue: deque = deque([(start, 0, [])])
        out: List[Tuple[str, int, List[RelationType]]] = []
        while queue:
            node, depth, path = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self._out.get(node, []):
                if edge.predicate not in predicates or edge.object_id in seen:
                    continue
                seen.add(edge.object_id)
                new_path = path + [edge.predicate]
                out.append((edge.object_id, depth + 1, new_path))
                queue.append((edge.object_id, depth + 1, new_path))
        return out

    def all_relations(self) -> List[Relation]:
        return [r for edges in self._out.values() for r in edges]

    def stats(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = defaultdict(int)
        for rel in self.all_relations():
            by_type[rel.predicate.value] += 1
        return {
            "nodes": len(set(self._out) | set(self._in)),
            "edges": len(self._keys),
            "by_type": dict(by_type),
        }

    # -- persistence --------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [r.model_dump(mode="json") for r in self.all_relations()]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> "ProductGraph":
        graph = cls()
        if not path.exists():
            return graph
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not load graph from %s: %s", path, exc)
            return graph
        for item in payload:
            try:
                rel = Relation(**item)
            except Exception:  # noqa: BLE001
                continue
            if rel.key() in graph._keys:
                continue
            graph._keys.add(rel.key())
            graph._out[rel.subject_id].append(rel)
            graph._in[rel.object_id].append(rel)
        return graph


def build_graph(products: Sequence[Product]) -> ProductGraph:
    """
    Derive the graph from the catalog.

    Relations come from three places: explicit identity (variant_of), declared
    attributes (certified_by, belongs_to, documented_in), and inferred
    compatibility within a family.
    """
    graph = ProductGraph()
    by_id = {p.identity.product_id: p for p in products}

    for product in products:
        pid = product.identity.product_id

        graph.add(pid, RelationType.BELONGS_TO, product.category_id)

        base = product.identity.base_product_id
        if base and base != pid:
            graph.add(pid, RelationType.VARIANT_OF, base, confidence=0.9)

        for source_id in product.source_ids:
            graph.add(pid, RelationType.DOCUMENTED_IN, source_id)

        certs = product.attributes.get("certifications")
        if certs and isinstance(certs.value, list):
            for cert in certs.value:
                graph.add(
                    pid,
                    RelationType.CERTIFIED_BY,
                    f"cert:{cert}",
                    evidence=certs.evidence,
                    confidence=certs.confidence,
                )

    # Variants of the same base are interchangeable candidates within a series,
    # which is a genuinely useful cross-sell / substitution signal.
    families: Dict[str, List[str]] = defaultdict(list)
    for product in products:
        base = product.identity.base_product_id
        if base:
            families[base].append(product.identity.product_id)

    for base, members in families.items():
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                graph.add(a, RelationType.COMPATIBLE_WITH, b, confidence=0.6)

    return graph
