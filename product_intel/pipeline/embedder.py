"""
Embeddings and vector search.

Three fixes over the predecessor, all aimed at catalog scale:

  1. **A content-hash cache.** Re-ingesting a catalog re-embeds nothing that
     did not change. On a real catalog this is the difference between minutes
     and hours.
  2. **Real batching and device selection.** The predecessor's runbook claimed
     GPU support that did not exist anywhere in the code; this actually
     implements it.
  3. **An ANN index when one is available.** hnswlib is used if installed and
     the numpy brute-force path remains as a fallback, so the system works out
     of the box and scales when it needs to.

Products, not documents, are the unit of embedding: one vector per product
built from its identity and its arbitrated attributes. That is what makes
"find me a 3-pole 63A breaker rated 10kA" work as a semantic query.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from product_intel.config import Settings, settings as global_settings
from product_intel.manifest import atomic_write_json
from product_intel.models import Product
from product_intel.schema.dictionary import CategorySchema, Taxonomy

log = logging.getLogger(__name__)

_model_cache: Dict[str, Any] = {}


def resolve_device(preference: str = "auto") -> str:
    if preference != "auto":
        return preference
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def get_model(model_name: str, device: str = "auto"):
    key = f"{model_name}@{device}"
    if key not in _model_cache:
        from sentence_transformers import SentenceTransformer

        resolved = resolve_device(device)
        log.info("loading embedding model %s on %s", model_name, resolved)
        _model_cache[key] = SentenceTransformer(model_name, device=resolved)
    return _model_cache[key]


def product_text(product: Product, schema: CategorySchema) -> str:
    """
    Build the text that represents a product in vector space.

    Attribute names are included alongside values so a query phrased in trade
    language ("3 pole", "full port brass") lands near the right products.
    """
    ident = product.identity
    parts: List[str] = [
        f"{ident.manufacturer} {ident.mpn}",
        schema.name,
        str(product.get("product_name") or ""),
        str(product.get("short_description") or ""),
    ]
    if ident.series:
        parts.append(f"series {ident.series}")

    for code, av in sorted(product.attributes.items()):
        attr = schema.get(code)
        if attr is None or attr.generated or av.value in (None, "", []):
            continue
        if code in ("manufacturer", "mpn", "product_name"):
            continue
        if isinstance(av.value, list):
            rendered = ", ".join(str(v) for v in av.value)
        elif attr.is_numeric:
            rendered = f"{float(av.value):g}{(' ' + av.unit) if av.unit else ''}"
        else:
            rendered = str(av.value)
        parts.append(f"{attr.name}: {rendered}")

    return " | ".join(p for p in parts if p and p.strip())


class EmbeddingCache:
    """Content-hash keyed cache. A product only re-embeds when its text changes."""

    def __init__(self, path: Path, model_name: str) -> None:
        self.path = path
        self.model_name = model_name
        self._data: Dict[str, List[float]] = {}
        self._loaded = False
        self.hits = 0
        self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_name}|{text}".encode("utf-8")).hexdigest()[:24]

    def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as exc:  # noqa: BLE001
                log.warning("embedding cache unreadable, starting fresh: %s", exc)
                self._data = {}
        self._loaded = True

    def get(self, text: str) -> Optional[List[float]]:
        self.load()
        hit = self._data.get(self._key(text))
        if hit is not None:
            self.hits += 1
        else:
            self.misses += 1
        return hit

    def put(self, text: str, vector: List[float]) -> None:
        self.load()
        self._data[self._key(text)] = vector

    def flush(self) -> None:
        if self._loaded:
            atomic_write_json(self.path, self._data)


def embed_texts(
    texts: Sequence[str],
    cfg: Optional[Settings] = None,
    cache: Optional[EmbeddingCache] = None,
) -> Tuple[List[List[float]], Dict[str, Any]]:
    """Embed a batch of texts, using the cache for anything unchanged."""
    cfg = cfg or global_settings
    stats: Dict[str, Any] = {"total": len(texts), "cached": 0, "computed": 0, "device": None}

    if not cfg.embedding_enabled or not texts:
        return [[] for _ in texts], stats

    vectors: List[Optional[List[float]]] = [None] * len(texts)
    pending: List[Tuple[int, str]] = []

    for i, text in enumerate(texts):
        if cache is not None:
            hit = cache.get(text)
            if hit is not None:
                vectors[i] = hit
                stats["cached"] += 1
                continue
        pending.append((i, text))

    if pending:
        model = get_model(cfg.embedding_model, cfg.embedding_device)
        stats["device"] = resolve_device(cfg.embedding_device)
        encoded = model.encode(
            [t for _, t in pending],
            batch_size=cfg.embedding_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        for (idx, text), vec in zip(pending, encoded):
            as_list = [float(x) for x in vec]
            vectors[idx] = as_list
            if cache is not None:
                cache.put(text, as_list)
        stats["computed"] = len(pending)

    return [v if v is not None else [] for v in vectors], stats


class VectorIndex:
    """
    Product vector index.

    Persisted as JSON so it stays inspectable and rebuildable. hnswlib is used
    for search when available; otherwise a vectorized numpy dot product, which
    is fine into the tens of thousands of products.
    """

    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.path = self.cfg.vector_index_path
        self.ids: List[str] = []
        self.matrix: Optional[np.ndarray] = None
        self.meta: Dict[str, Dict[str, Any]] = {}
        self._ann = None

    def build(self, products: Sequence[Product], taxonomy: Taxonomy) -> Dict[str, Any]:
        cache = EmbeddingCache(self.cfg.embedding_cache_path, self.cfg.embedding_model) \
            if self.cfg.embedding_cache else None

        texts, ids, meta = [], [], {}
        for product in products:
            schema = taxonomy.get(product.category_id)
            pid = product.identity.product_id
            ids.append(pid)
            texts.append(product_text(product, schema))
            meta[pid] = {
                "mpn": product.identity.mpn,
                "manufacturer": product.identity.manufacturer,
                "category_id": product.category_id,
                "name": product.display_name(),
            }

        vectors, stats = embed_texts(texts, self.cfg, cache)
        if cache is not None:
            cache.flush()
            stats["cache_hits"] = cache.hits
            stats["cache_misses"] = cache.misses

        keep = [(i, v) for i, v in zip(ids, vectors) if v]
        self.ids = [i for i, _ in keep]
        self.matrix = np.array([v for _, v in keep], dtype=np.float32) if keep else None
        self.meta = meta
        self._ann = None
        stats["indexed"] = len(self.ids)
        return stats

    def save(self) -> None:
        payload = {
            "model": self.cfg.embedding_model,
            "ids": self.ids,
            "meta": self.meta,
            "vectors": self.matrix.tolist() if self.matrix is not None else [],
        }
        atomic_write_json(self.path, payload)

    @classmethod
    def load(cls, cfg: Optional[Settings] = None) -> "VectorIndex":
        index = cls(cfg)
        if not index.path.exists():
            return index
        try:
            with open(index.path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            index.ids = payload.get("ids", [])
            index.meta = payload.get("meta", {})
            vectors = payload.get("vectors", [])
            index.matrix = np.array(vectors, dtype=np.float32) if vectors else None
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load vector index: %s", exc)
        return index

    def _ensure_ann(self):
        """Build an hnswlib index if the library is present and the catalog is big enough."""
        if self._ann is not None or self.matrix is None or len(self.ids) < 2000:
            return
        try:
            import hnswlib  # type: ignore
        except ImportError:
            return
        dim = self.matrix.shape[1]
        ann = hnswlib.Index(space="cosine", dim=dim)
        ann.init_index(max_elements=len(self.ids), ef_construction=200, M=16)
        ann.add_items(self.matrix, np.arange(len(self.ids)))
        ann.set_ef(64)
        self._ann = ann
        log.info("built hnswlib ANN index over %d products", len(self.ids))

    def search(self, query_vector: Sequence[float], k: int = 10) -> List[Tuple[str, float]]:
        if self.matrix is None or not self.ids or not len(query_vector):
            return []
        q = np.array(query_vector, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm == 0:
            return []
        q = q / norm

        self._ensure_ann()
        if self._ann is not None:
            labels, distances = self._ann.knn_query(q, k=min(k, len(self.ids)))
            return [(self.ids[int(i)], float(1.0 - d)) for i, d in zip(labels[0], distances[0])]

        norms = np.linalg.norm(self.matrix, axis=1)
        norms[norms == 0] = 1e-10
        scores = (self.matrix @ q) / norms
        top = np.argsort(scores)[::-1][:k]
        return [(self.ids[int(i)], float(scores[int(i)])) for i in top]

    def size(self) -> int:
        return len(self.ids)
