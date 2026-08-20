"""
Shared API state: the engine, derived caches, and background jobs.

Two problems this solves.

First, cost. Loading the catalog, the graph and the vector index on every
request would make the UI unusable. They are loaded once and invalidated
explicitly whenever something writes.

Second, duration. A catalog build takes minutes on a real dataset, which is far
longer than any sensible HTTP timeout. Ingest and build run as background jobs
that the client polls, so the connection is never held open and the UI can show
progress instead of a spinner that might mean anything.

Deliberately in-process: a single-worker uvicorn serving one analyst is the
target, and a job queue with a broker would be infrastructure this does not
need. `JOB_STORE` is guarded by a lock because uvicorn runs sync handlers in a
thread pool.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from product_intel.config import Settings, reload_settings, settings as global_settings
from product_intel.engine import ProductIntelligenceEngine
from product_intel.graph import ProductGraph
from product_intel.manifest import CatalogStore, ManifestManager
from product_intel.models import Product, ProductAsset
from product_intel.review import ReviewQueue
from product_intel.schema.dictionary import Taxonomy, load_taxonomy

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CatalogState:
    """
    Cached view of the catalog on disk.

    Everything here is derived from files, so invalidation is always safe: the
    worst case is a reload, never a lost write.
    """

    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self._lock = threading.RLock()
        self._engine: Optional[ProductIntelligenceEngine] = None
        self._products: Optional[Dict[str, Product]] = None
        self._graph: Optional[ProductGraph] = None
        self._assets: Optional[Dict[str, List[ProductAsset]]] = None
        self._source_names: Optional[Dict[str, str]] = None
        self.taxonomy: Taxonomy = load_taxonomy()

    # -- engine ------------------------------------------------------------

    @property
    def engine(self) -> ProductIntelligenceEngine:
        with self._lock:
            if self._engine is None:
                self._engine = ProductIntelligenceEngine(self.cfg)
            return self._engine

    def reset_engine(self) -> None:
        """Drop the engine so a provider switch takes effect on the next call."""
        with self._lock:
            self._engine = None

    # -- catalog -----------------------------------------------------------

    @property
    def store(self) -> CatalogStore:
        return CatalogStore(self.cfg)

    def products(self) -> Dict[str, Product]:
        with self._lock:
            if self._products is None:
                self._products = {
                    p.identity.product_id: p for p in self.store.iter_products()
                }
            return self._products

    def product(self, identifier: str) -> Optional[Product]:
        """Look up by product_id, then by MPN, then by an alternate MPN."""
        products = self.products()
        if identifier in products:
            return products[identifier]

        from product_intel.pipeline.identity import normalize_mpn

        target = normalize_mpn(identifier)
        for product in products.values():
            if product.identity.normalized_mpn == target:
                return product
        for product in products.values():
            if any(normalize_mpn(a) == target for a in product.identity.alternate_mpns):
                return product
        return None

    def graph(self) -> ProductGraph:
        with self._lock:
            if self._graph is None:
                self._graph = ProductGraph.load(self.cfg.graph_path)
            return self._graph

    def assets_for(self, product_id: str) -> List[ProductAsset]:
        with self._lock:
            if self._assets is None:
                grouped: Dict[str, List[ProductAsset]] = {}
                for asset in self.store.load_assets():
                    if asset.product_id:
                        grouped.setdefault(asset.product_id, []).append(asset)
                self._assets = grouped
            return self._assets.get(product_id, [])

    def source_names(self) -> Dict[str, str]:
        with self._lock:
            if self._source_names is None:
                self._source_names = {
                    e.source_id: e.filename for e in ManifestManager(self.cfg).list()
                }
            return self._source_names

    def product_names(self) -> Dict[str, str]:
        return {pid: p.identity.mpn for pid, p in self.products().items()}

    def review_queue(self) -> ReviewQueue:
        # Intentionally not cached: the queue is written by corrections and by
        # builds, and a stale review list is actively misleading.
        return ReviewQueue(self.cfg)

    def open_flag_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for flag in self.review_queue().open_flags():
            counts[flag.product_id] = counts.get(flag.product_id, 0) + 1
        return counts

    def catalog_built(self) -> bool:
        return self.cfg.db_path.exists() and bool(self.products())

    # -- invalidation ------------------------------------------------------

    def invalidate(self, products: bool = True, graph: bool = True, assets: bool = True) -> None:
        with self._lock:
            if products:
                self._products = None
                self._source_names = None
            if graph:
                self._graph = None
            if assets:
                self._assets = None

    def refresh_settings(self) -> Settings:
        self.cfg = reload_settings()
        self.reset_engine()
        return self.cfg


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


class Job:
    def __init__(self, kind: str) -> None:
        self.job_id = f"job_{uuid.uuid4().hex[:12]}"
        self.kind = kind
        self.state = "queued"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.progress = 0.0
        self.message = "queued"
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.log: List[str] = []

    def emit(self, message: str, progress: Optional[float] = None) -> None:
        self.message = message
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        stamped = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {message}"
        self.log.append(stamped)
        # Bound the log so a runaway job cannot grow without limit.
        if len(self.log) > 400:
            self.log = self.log[-400:]
        log.info("[%s] %s", self.job_id, message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": round(self.progress, 3),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "log": self.log[-60:],
        }


class JobStore:
    def __init__(self, keep: int = 20) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._keep = keep
        self._running: Optional[str] = None

    def running_job(self) -> Optional[Job]:
        with self._lock:
            if self._running is None:
                return None
            return self._jobs.get(self._running)

    def submit(self, kind: str, work: Callable[[Job], Dict[str, Any]]) -> Job:
        """
        Start a job. Refuses to run two catalog-mutating jobs at once.

        Concurrent ingests would race on the manifest and the product files, so
        serializing them here is a correctness requirement, not a nicety.
        """
        with self._lock:
            current = self._jobs.get(self._running or "")
            if current is not None and current.state in ("queued", "running"):
                raise RuntimeError(
                    f"A {current.kind} job is already running. Wait for it to finish."
                )
            job = Job(kind)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._running = job.job_id
            while len(self._order) > self._keep:
                self._jobs.pop(self._order.pop(0), None)

        def runner() -> None:
            job.state = "running"
            job.started_at = _now()
            started = time.time()
            try:
                job.result = work(job)
                job.state = "done"
                job.progress = 1.0
                job.emit(f"finished in {time.time() - started:.1f}s")
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.emit(f"FAILED: {job.error}")
                log.error("job %s failed\n%s", job.job_id, traceback.format_exc())
            finally:
                job.finished_at = _now()

        threading.Thread(target=runner, name=f"pi-{job.kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 10) -> List[Job]:
        with self._lock:
            return [self._jobs[j] for j in reversed(self._order[-limit:]) if j in self._jobs]


STATE = CatalogState()
JOBS = JobStore()
