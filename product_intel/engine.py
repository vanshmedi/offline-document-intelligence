"""
The engine: orchestration across all four layers.

    L1 ingest & perceive   -> sources become provenance-tagged fragments
    L2 resolve & structure -> fragments become identified, normalized products
    L3 enrich & generate   -> gaps filled from the graph, commerce copy authored
    L4 validate & govern   -> scored, flagged, indexed, exportable

Two entry points:
    ingest(paths)  -- L1 + L2, incremental and resumable
    build()        -- L3 + L4 across the whole catalog

They are separate because enrichment is a catalog-wide operation: gap filling
needs the family to exist before it can inherit from it, and outlier detection
needs peers before it can call anything an outlier.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from product_intel.config import Settings, settings as global_settings
from product_intel.confidence import score_attribute
from product_intel.graph import ProductGraph, build_graph
from product_intel.llm.provider import LLMProvider, get_provider
from product_intel.manifest import CatalogStore, ManifestManager
from product_intel.models import (
    AuditLog,
    AuditStep,
    Evidence,
    ExtractionMethod,
    Fragment,
    ManifestEntry,
    Product,
    ProductAsset,
    ProductIdentity,
    ProductStatus,
    SourceDocument,
    SourceKind,
    StepStatus,
)
from product_intel.pipeline.db_ingest import CatalogDB
from product_intel.pipeline.embedder import VectorIndex
from product_intel.pipeline.enricher import enrich_catalog
from product_intel.pipeline.extractor import SchemaDirectedExtractor
from product_intel.pipeline.golden import build_golden_record
from product_intel.pipeline.identity import (
    IdentityResolver,
    extract_mpn_candidates,
    normalize_manufacturer,
    normalize_mpn,
    product_id_for,
    variant_base_id,
)
from product_intel.pipeline.ingest.registry import (
    checksum_file,
    content_type_for,
    infer_kind,
    is_supported,
    parse_source,
    source_id_for,
)
from product_intel.pipeline.normalizer import normalize_value
from product_intel.review import LearnedRules, ReviewQueue, flag_product
from product_intel.schema.dictionary import CategorySchema, Taxonomy, load_taxonomy
from product_intel.validation import (
    PeerStatistics,
    catalog_scorecard,
    compute_quality,
    validate_product,
)

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    sources_seen: int = 0
    sources_processed: int = 0
    sources_skipped: int = 0
    sources_failed: int = 0
    products_created: int = 0
    products_updated: int = 0
    fragments: int = 0
    observations: int = 0
    llm_calls: int = 0
    rejected_quotes: int = 0
    duration_s: float = 0.0
    failures: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sources_seen": self.sources_seen,
            "sources_processed": self.sources_processed,
            "sources_skipped": self.sources_skipped,
            "sources_failed": self.sources_failed,
            "products_created": self.products_created,
            "products_updated": self.products_updated,
            "fragments": self.fragments,
            "observations": self.observations,
            "llm_calls": self.llm_calls,
            "rejected_quotes": self.rejected_quotes,
            "duration_s": round(self.duration_s, 2),
            "failures": self.failures,
        }


class ProductIntelligenceEngine:
    def __init__(
        self,
        cfg: Optional[Settings] = None,
        provider: Optional[LLMProvider] = None,
    ) -> None:
        self.cfg = cfg or global_settings
        self.taxonomy: Taxonomy = load_taxonomy()
        self.store = CatalogStore(self.cfg)
        self.manifest = ManifestManager(self.cfg)
        self.provider = provider if provider is not None else get_provider(self.cfg)
        self.extractor = SchemaDirectedExtractor(self.provider, self.cfg)
        self.resolver = IdentityResolver()
        self.queue = ReviewQueue(self.cfg)
        self.learned = LearnedRules(self.cfg)
        self._products: Dict[str, Product] = {}

        # Install reviewer-promoted mappings so corrections made yesterday are
        # applied automatically to everything ingested today.
        from product_intel.pipeline.normalizer import register_learned_synonyms

        register_learned_synonyms(self.learned.all_enum_synonyms())

    # =======================================================================
    # L1 + L2: ingestion
    # =======================================================================

    def discover(self, root: Path) -> List[Path]:
        """Find every supported file under a path."""
        if root.is_file():
            return [root] if is_supported(root) else []
        return sorted(p for p in root.rglob("*") if p.is_file() and is_supported(p))

    def ingest(
        self,
        paths: Sequence[Path],
        force: bool = False,
        manufacturer_hint: Optional[str] = None,
        parallel: bool = True,
    ) -> IngestReport:
        """Ingest sources into products. Incremental: unchanged sources are skipped."""
        started = time.time()
        report = IngestReport()

        files: List[Path] = []
        for path in paths:
            files.extend(self.discover(Path(path)))
        report.sources_seen = len(files)
        if not files:
            report.duration_s = time.time() - started
            return report

        self._load_catalog_into_memory()

        # Parsing is I/O and CPU bound and touches no shared state, so it
        # parallelizes cleanly. Attribution mutates the catalog, so it is done
        # serially afterwards -- this is the ordering that keeps identity
        # resolution deterministic regardless of worker count.
        parsed: List[Tuple[Path, str, List[Fragment], str, Dict[str, Any], SourceKind]] = []

        def _parse_one(path: Path):
            checksum = checksum_file(path)
            sid = source_id_for(checksum)
            existing = self.manifest.get(sid)
            if existing is not None and existing.status == "completed" and not force:
                return ("skip", path, sid, checksum, None, None, None, None)
            kind = infer_kind(path)
            fragments, mirror, stats = parse_source(path, sid, enable_ocr=True)
            return ("ok", path, sid, checksum, fragments, mirror, stats, kind)

        workers = max(1, self.cfg.max_workers) if parallel else 1
        results = []
        if workers > 1 and len(files) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_parse_one, p): p for p in files}
                for fut in as_completed(futures):
                    path = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        results.append(("fail", path, None, None, None, None, str(exc), None))
        else:
            for path in files:
                try:
                    results.append(_parse_one(path))
                except Exception as exc:  # noqa: BLE001
                    results.append(("fail", path, None, None, None, None, str(exc), None))

        # Deterministic order regardless of completion order.
        results.sort(key=lambda r: str(r[1]))

        for outcome, path, sid, checksum, fragments, mirror, stats, kind in results:
            if outcome == "skip":
                report.sources_skipped += 1
                continue
            if outcome == "fail":
                report.sources_failed += 1
                report.failures.append((path.name, str(stats)))
                log.error("failed to parse %s: %s", path.name, stats)
                continue
            parsed.append((path, sid, fragments, mirror, stats, kind))

        for path, sid, fragments, mirror, stats, kind in parsed:
            try:
                self._attribute_source(
                    path, sid, checksum_file(path), fragments, mirror, stats, kind,
                    manufacturer_hint, report, force,
                )
                report.sources_processed += 1
            except Exception as exc:  # noqa: BLE001
                report.sources_failed += 1
                report.failures.append((path.name, f"{type(exc).__name__}: {exc}"))
                log.exception("attribution failed for %s", path.name)

        self.manifest.flush()
        for product in self._products.values():
            self.store.save_product(product)

        report.duration_s = time.time() - started
        report.llm_calls = self.provider.call_count
        return report

    def _attribute_source(
        self,
        path: Path,
        source_id: str,
        checksum: str,
        fragments: List[Fragment],
        mirror: str,
        stats: Dict[str, Any],
        kind: SourceKind,
        manufacturer_hint: Optional[str],
        report: IngestReport,
        force: bool,
    ) -> None:
        """Turn one parsed source into product observations."""
        audit = AuditLog(
            subject_id=source_id,
            checksum=checksum,
            llm_provider=self.provider.name,
            llm_model=self.cfg.active_model,
            embedding_model=self.cfg.embedding_model,
        )
        step_started = datetime.now(timezone.utc).isoformat()

        entry = self.manifest.get(source_id) or ManifestEntry(
            source_id=source_id,
            filename=path.name,
            relative_path=str(path),
            checksum=checksum,
            content_type=content_type_for(path),
            kind=kind,
            manufacturer_hint=manufacturer_hint,
        )
        entry.steps["parse"] = StepStatus.COMPLETED.value

        self.store.save_mirror(source_id, mirror)
        self.store.save_fragments(source_id, fragments)
        report.fragments += len(fragments)
        for warning in stats.get("warnings", [])[:5]:
            report.warnings.append(f"{path.name}: {warning}")

        self.store.save_source_doc(
            SourceDocument(
                source_id=source_id,
                filename=path.name,
                kind=kind,
                content_type=content_type_for(path),
                checksum=checksum,
                relative_path=str(path),
                page_count=stats.get("pages"),
                mirror_path=str(self.store.source_dir(source_id) / "mirror.md"),
                manufacturer_hint=manufacturer_hint,
                fragment_count=len(fragments),
            )
        )

        # -- image assets attach to products by filename, not by extraction --
        if content_type_for(path) == "image":
            attached = self._attach_asset(path, source_id, fragments, entry)
            if not attached:
                report.warnings.append(f"{path.name}: no product matched this asset")
            entry.status = "completed" if attached else "unattached"
            audit.status = entry.status
            self.manifest.put(entry, flush=False)
            self.store.save_audit(source_id, audit)
            return

        # -- which products does this source describe? ----------------------
        groups = self._group_fragments_by_product(fragments, mirror, manufacturer_hint)
        entry.steps["identity"] = StepStatus.COMPLETED.value

        if not groups:
            report.warnings.append(f"{path.name}: no product identity could be resolved")
            entry.status = "no_products"
            audit.status = "no_products"
            audit.steps.append(
                AuditStep(
                    step_name="identity", status=StepStatus.FAILED,
                    started_at=step_started, completed_at=datetime.now(timezone.utc).isoformat(),
                    error_message="no manufacturer/MPN pair could be resolved from this source",
                )
            )
            self.manifest.put(entry, flush=False)
            self.store.save_audit(source_id, audit)
            return

        # OCR-recovered and vision-derived sources are low-trust for identity.
        ocr_heavy = bool(stats.get("ocr_pages")) or content_type_for(path) == "image"

        for (manufacturer, mpn, series), group_fragments in groups.items():
            product, created = self._get_or_create_product(
                manufacturer, mpn, series, allow_fuzzy=ocr_heavy
            )
            # A group keyed by its own series is the family record.
            if series and mpn == series:
                product.is_family = True
            if created:
                report.products_created += 1
            else:
                report.products_updated += 1

            schema = self._classify(product, group_fragments, mirror)

            result = self.extractor.extract(
                group_fragments, schema, source_id, kind, mirror=mirror, mpn_hint=mpn,
            )
            report.rejected_quotes += result.rejected_quotes

            for code, values in result.values.items():
                for av in values:
                    product.observations.setdefault(code, []).append(av)
                    report.observations += 1

            # Identity attributes are facts about the product, recorded with
            # the source that asserted them.
            self._record_identity_observations(product, manufacturer, mpn, series, source_id, kind, mirror)

            if source_id not in product.source_ids:
                product.source_ids.append(source_id)
            if source_id not in entry.product_ids:
                entry.product_ids.append(product.identity.product_id)

            build_golden_record(product, schema)
            product.status = ProductStatus.DRAFT
            self._products[product.identity.product_id] = product

        entry.steps["extract"] = StepStatus.COMPLETED.value
        entry.status = "completed"
        entry.embedding_model = self.cfg.embedding_model
        audit.status = "completed"
        audit.steps.append(
            AuditStep(
                step_name="ingest", status=StepStatus.COMPLETED,
                started_at=step_started, completed_at=datetime.now(timezone.utc).isoformat(),
                stats={
                    "fragments": len(fragments),
                    "products": len(groups),
                    "pages": stats.get("pages"),
                    "tables": stats.get("tables"),
                    "ocr_pages": stats.get("ocr_pages"),
                    "empty_pages": stats.get("empty_pages"),
                },
                warnings=stats.get("warnings", []),
            )
        )
        self.manifest.put(entry, flush=False)
        self.store.save_audit(source_id, audit)

    def _group_fragments_by_product(
        self,
        fragments: Sequence[Fragment],
        mirror: str,
        manufacturer_hint: Optional[str],
    ) -> Dict[Tuple[str, str, Optional[str]], List[Fragment]]:
        """
        Decide which product(s) a source describes.

        Row-scoped feeds (a price file) describe one product per row, so each
        row is its own group. Documents describe one product plus, often, a
        variant table listing its siblings -- so the whole document is shared
        across every product it mentions, and the variant-matrix reader picks
        the right row per product.
        """
        groups: Dict[Tuple[str, str, Optional[str]], List[Fragment]] = {}

        row_scoped = [f for f in fragments if f.metadata.get("row_scoped")]
        if row_scoped:
            for frag in row_scoped:
                pairs = {str(k).strip().lower(): str(v) for k, v in (frag.table or [])}
                mpn = self._first_of(pairs, ("mpn", "part number", "part no", "sku", "item number", "catalog number", "model"))
                mfr = self._first_of(pairs, ("manufacturer", "brand", "supplier", "vendor")) or manufacturer_hint
                if not mpn or not mfr:
                    continue
                series = self._first_of(pairs, ("series", "family", "product line"))
                groups.setdefault((mfr, mpn, series), []).append(frag)
            if groups:
                return groups

        manufacturer = manufacturer_hint or self._detect_manufacturer(fragments, mirror)
        if not manufacturer:
            return {}

        series = self._detect_series(fragments, mirror)
        variant_mpns = self._detect_variant_mpns(fragments)
        shared = list(fragments)

        if variant_mpns:
            # A family datasheet: the label/value table describes the series and
            # the matrix describes the SKUs. The family is registered as its own
            # product so shared attributes have a single authoritative home for
            # variants to inherit from. Creating it deliberately here -- rather
            # than hoping MPN detection stumbles onto the series string -- is
            # what makes gap filling reliable.
            if series:
                groups[(manufacturer, series, series)] = shared
            for mpn in variant_mpns:
                groups[(manufacturer, mpn, series)] = shared
            return groups

        primary_mpn = self._detect_primary_mpn(fragments, mirror)
        if not primary_mpn:
            return {}
        groups[(manufacturer, primary_mpn, series)] = shared
        return groups

    @staticmethod
    def _first_of(pairs: Dict[str, str], keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            for actual, value in pairs.items():
                if key in actual and value.strip():
                    return value.strip()
        return None

    def _detect_manufacturer(self, fragments: Sequence[Fragment], mirror: str) -> Optional[str]:
        import re

        for frag in fragments:
            if frag.kind not in ("table", "keyvalue") or not frag.table:
                continue
            for row in frag.table:
                if len(row) < 2:
                    continue
                label = str(row[0]).strip().lower()
                if label in ("manufacturer", "brand", "supplier", "vendor", "made by"):
                    value = str(row[1]).strip()
                    if value:
                        return normalize_manufacturer(value)

        m = re.search(
            r"(?:manufacturer|brand|supplier)\s*[:\-]\s*([A-Z][\w&.\- ]{2,45})",
            mirror,
            flags=re.IGNORECASE,
        )
        if m:
            return normalize_manufacturer(m.group(1))

        # Fall back to the document's first heading, which on a datasheet or a
        # product page is nearly always "<Manufacturer> <part number>". Trailing
        # part-number-looking tokens are stripped rather than splitting on '-',
        # which would truncate hyphenated company names and leave MPN fragments
        # attached to the manufacturer.
        from product_intel.pipeline.identity import looks_like_mpn

        for frag in fragments:
            if frag.kind != "heading" or not frag.text:
                continue
            candidate = frag.text.split("|")[0].strip()
            tokens = candidate.split()
            while tokens and (looks_like_mpn(tokens[-1]) or tokens[-1].lower() in ("datasheet", "submittal")):
                tokens.pop()
            candidate = " ".join(tokens).strip(" -,")
            if 2 < len(candidate) < 50:
                return normalize_manufacturer(candidate)
        return None

    def _detect_primary_mpn(self, fragments: Sequence[Fragment], mirror: str) -> Optional[str]:
        from product_intel.pipeline.identity import looks_like_mpn

        for frag in fragments:
            if frag.kind not in ("table", "keyvalue") or not frag.table:
                continue
            # A wide table is a variant matrix, not label/value pairs. Its header
            # row reads as "Part Number | Nominal Size | ...", which would
            # otherwise yield "Nominal Size" as the part number.
            if len(frag.table[0]) > 2:
                continue
            for row in frag.table:
                if len(row) < 2:
                    continue
                label = str(row[0]).strip().lower()
                if any(k in label for k in ("part number", "part no", "catalog number", "mpn", "order code", "model number")):
                    value = str(row[1]).strip()
                    if looks_like_mpn(value):
                        return value
        candidates = extract_mpn_candidates(mirror[:4000])
        return candidates[0] if candidates else None

    def _detect_series(self, fragments: Sequence[Fragment], mirror: str) -> Optional[str]:
        import re

        for frag in fragments:
            if frag.kind not in ("table", "keyvalue") or not frag.table:
                continue
            for row in frag.table:
                if len(row) >= 2 and str(row[0]).strip().lower() in ("series", "product series", "family", "product family", "product line"):
                    value = str(row[1]).strip()
                    if value:
                        return value
        m = re.search(r"(?:series|product family|product line)\s*[:\-]\s*([\w\- ]{2,40})", mirror, flags=re.IGNORECASE)
        return m.group(1).strip() if m else None

    def _detect_variant_mpns(self, fragments: Sequence[Fragment]) -> List[str]:
        """Read part numbers out of a variant/selection table's key column."""
        from product_intel.pipeline.identity import looks_like_mpn

        found: List[str] = []
        for frag in fragments:
            if frag.kind != "table" or not frag.table or len(frag.table) < 2:
                continue
            header = [str(c or "").strip().lower() for c in frag.table[0]]
            if not header:
                continue
            first = header[0]
            if not any(h in first for h in ("part", "catalog", "cat no", "model", "order", "mpn", "sku", "item", "code")):
                continue
            for row in frag.table[1:]:
                if not row:
                    continue
                token = str(row[0] or "").strip()
                if looks_like_mpn(token) and token not in found:
                    found.append(token)
        return found

    def _get_or_create_product(
        self,
        manufacturer: str,
        mpn: str,
        series: Optional[str],
        allow_fuzzy: bool = False,
    ) -> Tuple[Product, bool]:
        pid = product_id_for(manufacturer, mpn)

        # Low-trust sources (OCR, vision) frequently mangle part numbers. A
        # near-match is recorded as a suspicion for a human to confirm, never
        # merged automatically -- see IdentityResolver.find_near_duplicate.
        duplicate_of: Optional[Tuple[str, str, int]] = None
        if allow_fuzzy and pid not in self._products and self.store.load_product(pid) is None:
            duplicate_of = self.resolver.find_near_duplicate(manufacturer, mpn)
            if duplicate_of is not None:
                log.info(
                    "possible duplicate: '%s' is %d edit(s) from known part '%s' (%s)",
                    mpn, duplicate_of[2], duplicate_of[1], manufacturer,
                )

        self.resolver.register(manufacturer, mpn)

        if pid in self._products:
            return self._products[pid], False

        existing = self.store.load_product(pid)
        if existing is not None:
            self._products[pid] = existing
            return existing, False

        base_id = variant_base_id(manufacturer, mpn, series)
        product = Product(
            identity=ProductIdentity(
                product_id=pid,
                manufacturer=manufacturer,
                mpn=mpn,
                normalized_mpn=normalize_mpn(mpn),
                series=series,
                base_product_id=base_id if base_id != pid else None,
                suspected_duplicate_of=duplicate_of[0] if duplicate_of else None,
                duplicate_evidence=(
                    f"{duplicate_of[2]} edit(s) from '{duplicate_of[1]}'; this part number came "
                    f"from a low-confidence source (OCR or vision) and may be a misread"
                    if duplicate_of else None
                ),
            )
        )
        self._products[pid] = product
        return product, True

    def _record_identity_observations(
        self,
        product: Product,
        manufacturer: str,
        mpn: str,
        series: Optional[str],
        source_id: str,
        kind: SourceKind,
        mirror: str,
    ) -> None:
        """Record manufacturer/mpn/series as evidenced attributes, not just keys."""
        schema = self.taxonomy.get(product.category_id)
        for code, value in (("manufacturer", manufacturer), ("mpn", mpn), ("series", series)):
            if not value:
                continue
            if any(
                o.evidence and o.evidence.source_id == source_id
                for o in product.observations.get(code, [])
            ):
                continue
            attr = schema.get(code)
            if attr is None:
                continue
            av = normalize_value(value, attr)
            if av.value is None:
                continue
            av.evidence = Evidence(
                source_id=source_id,
                source_kind=kind,
                locator="document identity",
                quote=str(value),
                method=ExtractionMethod.NATIVE_TEXT,
                quote_verified=str(value) in mirror,
            )
            product.observations.setdefault(code, []).append(av)

    def _attach_asset(
        self,
        path: Path,
        source_id: str,
        fragments: Sequence[Fragment],
        entry: ManifestEntry,
    ) -> int:
        """
        Attach an image to the products its filename names.

        Filename matching rather than content analysis: 'VX-Series_hero.png'
        belongs to the VX-Series family and, through it, to every variant. This
        is how manufacturers actually name asset files, and it is far more
        reliable than trying to recognise a product from a photograph.
        """
        payload = next((f.metadata.get("asset") for f in fragments if f.metadata.get("asset")), None)
        if payload is None:
            return 0

        stem = normalize_mpn(path.stem)
        if len(stem) < 3:
            return 0

        matches: List[Product] = []
        for product in self._products.values():
            keys = [product.identity.normalized_mpn]
            if product.identity.series:
                keys.append(normalize_mpn(product.identity.series))
            if any(k and len(k) >= 3 and k in stem for k in keys):
                matches.append(product)

        if not matches:
            return 0

        # Prefer the most specific match: a family asset also covers its variants,
        # but a SKU-specific asset should not be attached to the whole family.
        best_len = max(len(p.identity.normalized_mpn) for p in matches)
        exact = [p for p in matches if len(p.identity.normalized_mpn) == best_len]
        targets = exact if len(exact) < len(matches) else matches

        asset = ProductAsset(**payload)
        for product in targets:
            schema = self.taxonomy.get(product.category_id)
            from product_intel.pipeline.ingest.image_parser import generate_alt_text

            product_asset = asset.model_copy(
                update={
                    "asset_id": f"{source_id}_{product.identity.product_id}",
                    "product_id": product.identity.product_id,
                    "alt_text": generate_alt_text(asset, product.display_name(), schema.name),
                }
            )
            if product_asset.asset_id not in product.asset_ids:
                product.asset_ids.append(product_asset.asset_id)
            if source_id not in product.source_ids:
                product.source_ids.append(source_id)
            if product.identity.product_id not in entry.product_ids:
                entry.product_ids.append(product.identity.product_id)
            self.store.save_asset(product_asset)
            self._products[product.identity.product_id] = product

        return len(targets)

    def _classify(
        self,
        product: Product,
        fragments: Sequence[Fragment],
        mirror: str,
    ) -> CategorySchema:
        """
        Assign a category. Keyword scoring first; the LLM only breaks ties.

        Classification is sticky: once a product has a confident category, a
        later low-signal source cannot reclassify it out from under its data.
        """
        if product.category_confidence >= 0.8 and product.category_id != "industrial.generic":
            return self.taxonomy.get(product.category_id)

        text = " ".join(f.text for f in fragments[:40])[:12000] or mirror[:12000]
        cid, confidence, keyword = self.taxonomy.classify_by_keywords(text)

        if confidence < 0.55 and self.provider.available:
            llm_cid = self._classify_with_llm(text, product)
            if llm_cid:
                cid, confidence, keyword = llm_cid, 0.7, "llm"

        if confidence > product.category_confidence:
            product.category_id = cid
            product.category_confidence = confidence
            product.category_evidence = f"matched '{keyword}'" if keyword else "no keyword match"

        return self.taxonomy.get(product.category_id)

    def _classify_with_llm(self, text: str, product: Product) -> Optional[str]:
        from product_intel.llm.provider import LLMUnavailable

        options = "\n".join(
            f"- {cid}: {cat.name} ({cat.vertical})"
            for cid, cat in self.taxonomy.categories.items()
        )
        prompt = (
            "Classify this industrial product into exactly one category.\n\n"
            f"CATEGORIES:\n{options}\n\n"
            f"PRODUCT: {product.identity.manufacturer} {product.identity.mpn}\n\n"
            f"DOCUMENT EXCERPT:\n{text[:3000]}\n\n"
            'Return JSON: {"category_id": "<one id from the list above>"}\n'
            'If none fit, return {"category_id": "industrial.generic"}.'
        )
        try:
            payload = self.provider.complete_json(prompt, expect="object")
        except LLMUnavailable:
            return None
        cid = payload.get("category_id") if isinstance(payload, dict) else None
        return cid if cid in self.taxonomy.categories else None

    # =======================================================================
    # L3 + L4: enrichment, validation, indexing
    # =======================================================================

    def build(
        self,
        enrich: bool = True,
        index: bool = True,
        rebuild_db: bool = True,
    ) -> Dict[str, Any]:
        """Run the catalog-wide phases and persist everything."""
        started = time.time()
        self._load_catalog_into_memory()
        products = list(self._products.values())

        if not products:
            return {"products": 0, "error": "catalog is empty; run ingest first"}

        out: Dict[str, Any] = {"products": len(products)}

        # -- baseline, before any enrichment ------------------------------
        # Peer statistics are fitted for the baseline too. Scoring the "before"
        # state without them would report perfect distinctiveness and then a
        # drop after enrichment, which measures the arrival of the outlier
        # detector rather than any change in the data.
        baseline_peers = PeerStatistics(self.cfg.outlier_z_threshold).fit(products)
        self._score_all(products, peers=baseline_peers, flag=False)
        before = catalog_scorecard(products)
        for product in products:
            product.quality_before_enrichment = product.quality.model_copy(deep=True)
        out["before"] = before

        # -- graph ---------------------------------------------------------
        graph = build_graph(products)
        graph.save(self.cfg.graph_path)
        out["graph"] = graph.stats()

        # -- enrichment ----------------------------------------------------
        if enrich:
            out["enrichment"] = enrich_catalog(products, self.taxonomy, graph, self.provider, self.cfg)

        # -- peer statistics need a populated catalog ----------------------
        peers = PeerStatistics(self.cfg.outlier_z_threshold).fit(products)
        out["peer_distributions"] = peers.peer_count()

        # -- score, validate, flag ----------------------------------------
        self.queue = ReviewQueue(self.cfg)
        validation = self._score_all(products, peers=peers, flag=True)
        out.update(validation)
        self.queue.flush()
        out["review"] = self.queue.stats()

        after = catalog_scorecard(products)
        out["after"] = after
        out["lift"] = {
            key: round(after[key] - before[key], 4)
            for key in ("completeness_core", "completeness_ecommerce", "completeness_enhanced",
                        "accuracy", "consistency", "overall")
        }
        out["lift"]["channel_ready"] = after["channel_ready"] - before["channel_ready"]

        # -- persist -------------------------------------------------------
        for product in products:
            self.store.save_product(product)

        if rebuild_db:
            db = CatalogDB(self.cfg)
            out["database"] = db.rebuild(products)

        if index and self.cfg.embedding_enabled:
            try:
                vindex = VectorIndex(self.cfg)
                out["index"] = vindex.build(products, self.taxonomy)
                vindex.save()
            except Exception as exc:  # noqa: BLE001
                log.warning("vector index build skipped: %s", exc)
                out["index"] = {"error": str(exc)}

        self.learned.flush()
        out["duration_s"] = round(time.time() - started, 2)
        out["llm"] = self.provider.stats()
        return out

    def _score_all(
        self,
        products: Sequence[Product],
        peers: Optional[PeerStatistics],
        flag: bool,
    ) -> Dict[str, Any]:
        mirrors = self.store.load_all_mirrors() if flag else None
        totals = {"validation_errors": 0, "validation_warnings": 0, "flags_raised": 0}

        for product in products:
            schema = self.taxonomy.get(product.category_id)

            for code, av in product.attributes.items():
                if av.confidence == 0.0:
                    score_attribute(av, schema.get(code))

            report = validate_product(product, schema, peers=peers, mirrors=mirrors)
            product.quality = compute_quality(product, schema, report, self.cfg)

            totals["validation_errors"] += len(report.errors)
            totals["validation_warnings"] += len(report.warnings)

            if flag:
                totals["flags_raised"] += flag_product(product, schema, report, self.queue, self.cfg)
                product.status = self._decide_status(product, report)

        return totals

    def _decide_status(self, product: Product, report) -> ProductStatus:
        """
        Decide publishability.

        There is no global override here. The predecessor shipped with
        `auto_approve_needs_review: true`, which meant its validation gate never
        blocked anything; a product that fails is routed to review, full stop.
        """
        if report.errors:
            return ProductStatus.NEEDS_REVIEW
        if product.quality.missing_required:
            return ProductStatus.NEEDS_REVIEW

        # Judged on read specifications only. Authored copy is expected to score
        # lower and should not by itself hold a well-sourced product back.
        schema = self.taxonomy.get(product.category_id)
        confidences = [
            av.confidence
            for code, av in product.attributes.items()
            if not (schema.get(code) and schema.get(code).generated)
        ]
        mean_conf = sum(confidences) / len(confidences) if confidences else 0.0
        if mean_conf < self.cfg.publish_confidence_threshold:
            return ProductStatus.ENRICHED
        if any(c.severity == "critical" for c in product.conflicts):
            return ProductStatus.NEEDS_REVIEW
        return ProductStatus.PUBLISHED

    # =======================================================================
    # helpers
    # =======================================================================

    def _load_catalog_into_memory(self) -> None:
        if self._products:
            return
        for product in self.store.iter_products():
            self._products[product.identity.product_id] = product
            self.resolver.register(product.identity.manufacturer, product.identity.mpn, product.identity.gtin)

    def products(self) -> List[Product]:
        self._load_catalog_into_memory()
        return list(self._products.values())

    def get_product(self, identifier: str) -> Optional[Product]:
        """Look up by product_id, or by MPN across the catalog."""
        self._load_catalog_into_memory()
        if identifier in self._products:
            return self._products[identifier]
        target = normalize_mpn(identifier)
        for product in self._products.values():
            if product.identity.normalized_mpn == target:
                return product
        return None

    def schema_for(self, product: Product) -> CategorySchema:
        return self.taxonomy.get(product.category_id)

    def scorecard(self) -> Dict[str, Any]:
        return catalog_scorecard(self.products())
