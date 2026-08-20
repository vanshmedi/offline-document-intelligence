"""
Manifest and catalog store.

The manifest is the single source of truth for processing state and drives all
incremental behaviour: re-running ingestion over a folder only touches sources
whose checksum changed.

Products are stored one JSON file per product beside their source artifacts, so
the catalog remains browsable and diffable without any database being intact.
The DuckDB file and the vector index are derived and can be deleted and rebuilt
at any time.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from product_intel.config import Settings, settings as global_settings
from product_intel.models import ManifestEntry, Product, SourceDocument

log = logging.getLogger(__name__)


def atomic_write_json(path: Path, payload: object) -> None:
    """
    Write JSON atomically.

    The predecessor rewrote manifest.json in place on every step, so an
    interrupted run could truncate the only record of what had been processed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ManifestManager:
    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.path = self.cfg.manifest_path
        self.cfg.catalog_path.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[Dict[str, ManifestEntry]] = None

    def _load(self) -> Dict[str, ManifestEntry]:
        if self._cache is not None:
            return self._cache
        entries: Dict[str, ManifestEntry] = {}
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for sid, data in raw.items():
                    try:
                        entries[sid] = ManifestEntry(**data)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("skipping malformed manifest entry %s: %s", sid, exc)
            except (json.JSONDecodeError, OSError) as exc:
                log.error("could not read manifest %s: %s", self.path, exc)
        self._cache = entries
        return entries

    def flush(self) -> None:
        if self._cache is None:
            return
        atomic_write_json(self.path, {sid: e.model_dump(mode="json") for sid, e in self._cache.items()})

    def get(self, source_id: str) -> Optional[ManifestEntry]:
        return self._load().get(source_id)

    def by_checksum(self, checksum: str) -> Optional[ManifestEntry]:
        return next((e for e in self._load().values() if e.checksum == checksum), None)

    def put(self, entry: ManifestEntry, flush: bool = True) -> None:
        entries = self._load()
        entry.updated_at = datetime.now(timezone.utc).isoformat()
        entries[entry.source_id] = entry
        if flush:
            self.flush()

    def list(self) -> List[ManifestEntry]:
        return list(self._load().values())

    def stats(self) -> Dict[str, int]:
        entries = self.list()
        out: Dict[str, int] = {"total": len(entries)}
        for e in entries:
            out[e.status] = out.get(e.status, 0) + 1
        return out


class CatalogStore:
    """
    Product persistence.

    Layout:
        Catalog/manifest.json
        Catalog/products/<product_id>.json
        Catalog/sources/<source_id>/{original.*, mirror.md, fragments.json, audit.json}
    """

    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.root = self.cfg.catalog_path
        self.products_dir = self.root / "products"
        self.sources_dir = self.root / "sources"
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)

    # -- products -----------------------------------------------------------

    def product_path(self, product_id: str) -> Path:
        return self.products_dir / f"{product_id}.json"

    def save_product(self, product: Product) -> None:
        product.updated_at = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.product_path(product.identity.product_id), product.model_dump(mode="json"))

    def load_product(self, product_id: str) -> Optional[Product]:
        path = self.product_path(product_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Product(**json.load(f))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load product %s: %s", product_id, exc)
            return None

    def iter_products(self) -> Iterator[Product]:
        for path in sorted(self.products_dir.glob("prod_*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    yield Product(**json.load(f))
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping unreadable product file %s: %s", path.name, exc)

    def load_all(self) -> List[Product]:
        return list(self.iter_products())

    def save_all(self, products: List[Product]) -> None:
        for product in products:
            self.save_product(product)

    def count(self) -> int:
        return sum(1 for _ in self.products_dir.glob("prod_*.json"))

    # -- sources ------------------------------------------------------------

    def source_dir(self, source_id: str) -> Path:
        return self.sources_dir / source_id

    def save_mirror(self, source_id: str, markdown: str) -> Path:
        d = self.source_dir(source_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "mirror.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def load_mirror(self, source_id: str) -> Optional[str]:
        path = self.source_dir(source_id) / "mirror.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def load_all_mirrors(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for d in self.sources_dir.iterdir():
            if not d.is_dir():
                continue
            path = d / "mirror.md"
            if path.exists():
                out[d.name] = path.read_text(encoding="utf-8", errors="replace")
        return out

    def save_fragments(self, source_id: str, fragments: List) -> Path:
        d = self.source_dir(source_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "fragments.json"
        atomic_write_json(path, [f.model_dump(mode="json") for f in fragments])
        return path

    def load_fragments(self, source_id: str) -> List:
        from product_intel.models import Fragment

        path = self.source_dir(source_id) / "fragments.json"
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [Fragment(**item) for item in json.load(f)]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load fragments for %s: %s", source_id, exc)
            return []

    def save_source_doc(self, doc: SourceDocument) -> None:
        d = self.source_dir(doc.source_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(d / "source.json", doc.model_dump(mode="json"))

    def load_source_doc(self, source_id: str) -> Optional[SourceDocument]:
        path = self.source_dir(source_id) / "source.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SourceDocument(**json.load(f))
        except Exception:  # noqa: BLE001
            return None

    # -- assets -------------------------------------------------------------

    @property
    def assets_dir(self) -> Path:
        d = self.root / "assets"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_asset(self, asset) -> None:
        atomic_write_json(self.assets_dir / f"{asset.asset_id}.json", asset.model_dump(mode="json"))

    def load_assets(self, product_id: Optional[str] = None) -> List:
        from product_intel.models import ProductAsset

        out = []
        for path in sorted(self.assets_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    asset = ProductAsset(**json.load(f))
            except Exception:  # noqa: BLE001
                continue
            if product_id is None or asset.product_id == product_id:
                out.append(asset)
        return out

    def save_audit(self, source_id: str, audit) -> None:
        d = self.source_dir(source_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_json(d / "audit.json", audit.model_dump(mode="json"))
