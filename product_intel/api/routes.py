"""
HTTP routes.

Organised by what a user is trying to do rather than by domain object:
overview, browse, inspect, review, search, run, configure, export.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from product_intel.api import schemas as S
from product_intel.api.state import JOBS, STATE
from product_intel.api.views import (
    attribute_view,
    evidence_view,
    product_detail,
    product_summary,
    quality_view,
    render_value,
    scorecard_view,
)
from product_intel.export.exporters import DEFAULT_EXTENSIONS, EXPORTERS
from product_intel.validation import catalog_scorecard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/overview", response_model=S.CatalogOverview)
def overview() -> S.CatalogOverview:
    products = list(STATE.products().values())
    taxonomy = STATE.taxonomy

    if not products:
        return S.CatalogOverview(
            scorecard=S.ScorecardView(),
            catalog_built=False,
            sources=len(STATE.source_names()),
        )

    card = catalog_scorecard(products)

    before_products = [p for p in products if p.quality_before_enrichment]
    scorecard_before = None
    if before_products:
        # Reconstruct the pre-enrichment picture from the snapshot each product
        # keeps, so the UI can show lift without re-running the build.
        snapshot = []
        for p in before_products:
            clone = p.model_copy()
            clone.quality = p.quality_before_enrichment
            snapshot.append(clone)
        scorecard_before = scorecard_view(catalog_scorecard(snapshot))

    by_cat: Dict[str, Dict[str, Any]] = {}
    for p in products:
        schema = taxonomy.get(p.category_id)
        row = by_cat.setdefault(
            p.category_id,
            {
                "category_id": p.category_id,
                "name": schema.name,
                "vertical": schema.vertical,
                "etim": schema.etim,
                "products": 0,
                "channel_ready": 0,
                "completeness": 0.0,
                "conflicts": 0,
            },
        )
        row["products"] += 1
        row["channel_ready"] += 1 if p.quality.channel_ready else 0
        row["completeness"] += p.quality.completeness_ecommerce
        row["conflicts"] += len(p.conflicts)
    for row in by_cat.values():
        row["completeness"] = round(row["completeness"] / max(1, row["products"]), 4)

    by_mfr: Dict[str, Dict[str, Any]] = {}
    for p in products:
        row = by_mfr.setdefault(
            p.identity.manufacturer,
            {"manufacturer": p.identity.manufacturer, "products": 0, "channel_ready": 0, "completeness": 0.0},
        )
        row["products"] += 1
        row["channel_ready"] += 1 if p.quality.channel_ready else 0
        row["completeness"] += p.quality.completeness_ecommerce
    for row in by_mfr.values():
        row["completeness"] = round(row["completeness"] / max(1, row["products"]), 4)

    # Which attributes are systematically missing? This is the view that tells a
    # data manager what to chase the manufacturer for.
    sellable = [p for p in products if p.identity.base_product_id is not None] or products
    filled: Counter = Counter()
    applicable: Counter = Counter()
    names: Dict[str, str] = {}
    for p in sellable:
        schema = taxonomy.get(p.category_id)
        for code in schema.required_codes("ecommerce"):
            applicable[code] += 1
            names[code] = schema.attributes[code].name
            av = p.attributes.get(code)
            if av is not None and av.value not in (None, "", []):
                filled[code] += 1
    coverage = [
        {
            "code": code,
            "name": names.get(code, code),
            "filled": filled.get(code, 0),
            "applicable": total,
            "coverage": round(filled.get(code, 0) / total, 4) if total else 0.0,
        }
        for code, total in applicable.items()
    ]
    coverage.sort(key=lambda r: r["coverage"])

    return S.CatalogOverview(
        scorecard=scorecard_view(card),
        scorecard_before=scorecard_before,
        by_category=sorted(by_cat.values(), key=lambda r: -r["products"]),
        by_manufacturer=sorted(by_mfr.values(), key=lambda r: -r["products"]),
        by_status=dict(Counter(
            p.status.value if hasattr(p.status, "value") else str(p.status) for p in products
        )),
        review=STATE.review_queue().stats(),
        graph=STATE.graph().stats(),
        sources=len(STATE.source_names()),
        attribute_coverage=coverage,
        catalog_built=STATE.catalog_built(),
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@router.get("/products", response_model=Dict[str, Any])
def list_products(
    q: Optional[str] = None,
    category: Optional[str] = None,
    manufacturer: Optional[str] = None,
    status: Optional[str] = None,
    vertical: Optional[str] = None,
    ready: Optional[bool] = None,
    families: Optional[bool] = Query(None, description="true = families only, false = SKUs only"),
    flagged: Optional[bool] = None,
    sort: str = "mpn",
    limit: int = Query(200, le=2000),
    offset: int = 0,
) -> Dict[str, Any]:
    products = list(STATE.products().values())
    flags = STATE.open_flag_counts()
    taxonomy = STATE.taxonomy

    if q:
        needle = q.lower().strip()
        products = [
            p for p in products
            if needle in p.identity.mpn.lower()
            or needle in p.identity.manufacturer.lower()
            or needle in p.display_name().lower()
            or needle in (p.identity.series or "").lower()
        ]
    if category:
        products = [p for p in products if p.category_id == category]
    if vertical:
        products = [p for p in products if taxonomy.get(p.category_id).vertical == vertical]
    if manufacturer:
        products = [p for p in products if p.identity.manufacturer == manufacturer]
    if status:
        products = [
            p for p in products
            if (p.status.value if hasattr(p.status, "value") else str(p.status)) == status
        ]
    if ready is not None:
        products = [p for p in products if p.quality.channel_ready is ready]
    if families is not None:
        products = [p for p in products if (p.identity.base_product_id is None) is families]
    if flagged:
        products = [p for p in products if flags.get(p.identity.product_id, 0) > 0]

    sorters = {
        "mpn": lambda p: (p.identity.manufacturer.lower(), p.identity.mpn.lower()),
        "quality": lambda p: -p.quality.overall,
        "completeness": lambda p: -p.quality.completeness_ecommerce,
        "flags": lambda p: -flags.get(p.identity.product_id, 0),
        "conflicts": lambda p: -len(p.conflicts),
    }
    products.sort(key=sorters.get(sort, sorters["mpn"]))

    total = len(products)
    page = products[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            product_summary(p, taxonomy, flags.get(p.identity.product_id, 0)).model_dump()
            for p in page
        ],
    }


@router.get("/products/{identifier}", response_model=S.ProductDetail)
def get_product(identifier: str) -> S.ProductDetail:
    product = STATE.product(identifier)
    if product is None:
        raise HTTPException(404, f"No product matching '{identifier}'")

    manifest_by_id = {e.source_id: e for e in __import__(
        "product_intel.manifest", fromlist=["ManifestManager"]
    ).ManifestManager(STATE.cfg).list()}

    sources = [
        S.SourceView(
            source_id=sid,
            filename=manifest_by_id[sid].filename,
            kind=manifest_by_id[sid].kind.value if hasattr(manifest_by_id[sid].kind, "value") else str(manifest_by_id[sid].kind),
            content_type=manifest_by_id[sid].content_type,
        )
        for sid in product.source_ids
        if sid in manifest_by_id
    ]

    return product_detail(
        product,
        STATE.taxonomy,
        graph=STATE.graph(),
        assets=STATE.assets_for(product.identity.product_id),
        source_names=STATE.source_names(),
        sources=sources,
        product_names=STATE.product_names(),
        open_flags=STATE.open_flag_counts().get(product.identity.product_id, 0),
        channel=STATE.cfg.target_channel,
    )


@router.get("/products/{identifier}/observations/{code}")
def get_observations(identifier: str, code: str) -> Dict[str, Any]:
    """
    Every value ever seen for one attribute, not just the winner.

    This is what makes conflict resolution auditable: the client can show what
    each source claimed and why one of them won.
    """
    product = STATE.product(identifier)
    if product is None:
        raise HTTPException(404, f"No product matching '{identifier}'")

    schema = STATE.taxonomy.get(product.category_id)
    winner = product.attributes.get(code)
    observations = product.observations.get(code, [])

    return {
        "code": code,
        "name": schema.get(code).name if schema.get(code) else code,
        "winner": attribute_view(winner, schema, product, STATE.source_names()).model_dump() if winner else None,
        "observations": [
            attribute_view(av, schema, product, STATE.source_names()).model_dump()
            for av in observations
        ],
        "conflict": next(
            (c.model_dump() for c in product.conflicts if c.code == code), None
        ),
    }


@router.get("/sources/{source_id}/mirror")
def get_mirror(source_id: str, highlight: Optional[str] = None) -> Dict[str, Any]:
    """
    The canonical Markdown mirror of a source.

    This is what makes "click through to the evidence" real: the client can show
    the actual document text with the cited quote located inside it.
    """
    mirror = STATE.store.load_mirror(source_id)
    if mirror is None:
        raise HTTPException(404, f"No mirror for source '{source_id}'")

    offset = None
    if highlight:
        from product_intel.pipeline.extractor import _normalize_for_match

        needle = _normalize_for_match(highlight)
        # Map the normalized match position back to an index in the raw text so
        # the client can scroll to it.
        if needle:
            index_map: List[int] = []
            normalized_chars: List[str] = []
            for i, ch in enumerate(mirror.lower()):
                if ch.isalnum():
                    normalized_chars.append(ch)
                    index_map.append(i)
            hay = "".join(normalized_chars)
            pos = hay.find(needle)
            if pos == -1 and len(needle) > 20:
                pos = hay.find(needle[: int(len(needle) * 0.6)])
            if pos != -1 and pos < len(index_map):
                offset = index_map[pos]

    return {
        "source_id": source_id,
        "filename": STATE.source_names().get(source_id, source_id),
        "markdown": mirror,
        "highlight_offset": offset,
        "length": len(mirror),
    }


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


_REASON_RE = re.compile(r"^([a-z_]+)\s*:")


@router.get("/review", response_model=Dict[str, Any])
def list_flags(
    limit: int = Query(100, le=1000),
    severity: Optional[str] = None,
    reason: Optional[str] = None,
    product_id: Optional[str] = None,
) -> Dict[str, Any]:
    queue = STATE.review_queue()
    flags = queue.prioritized(limit=2000)

    if severity:
        flags = [f for f in flags if f.severity == severity]
    if reason:
        flags = [f for f in flags if f.reason.split(":")[0] == reason]
    if product_id:
        flags = [f for f in flags if f.product_id == product_id]

    products = STATE.products()
    taxonomy = STATE.taxonomy
    out: List[S.FlagView] = []

    for flag in flags[:limit]:
        product = products.get(flag.product_id)
        if product is None:
            continue
        schema = taxonomy.get(product.category_id)
        attr = schema.get(flag.attribute_code) if flag.attribute_code else None
        current = product.attributes.get(flag.attribute_code) if flag.attribute_code else None

        out.append(
            S.FlagView(
                flag_id=flag.flag_id,
                product_id=flag.product_id,
                product_mpn=product.identity.mpn,
                product_name=product.display_name(),
                attribute_code=flag.attribute_code,
                attribute_name=attr.name if attr else None,
                reason=flag.reason,
                reason_kind=(_REASON_RE.match(flag.reason).group(1) if _REASON_RE.match(flag.reason) else "other"),
                severity=flag.severity,
                confidence=round(flag.confidence, 4),
                suggested_value=flag.suggested_value,
                current_value=current.value if current else None,
                allowed_values=attr.allowed_values if attr else None,
                datatype=attr.datatype if attr else "string",
                unit=attr.canonical_unit if attr else None,
                evidence=evidence_view(current, STATE.source_names()) if current else None,
                created_at=flag.created_at,
            )
        )

    return {"stats": queue.stats(), "items": [f.model_dump() for f in out]}


@router.post("/review/correct", response_model=S.CorrectionResponse)
def apply_correction_route(req: S.CorrectionRequest) -> S.CorrectionResponse:
    """
    Apply a human correction.

    Recorded as a HUMAN-method observation, which outranks every automated
    source in golden-record arbitration -- so the correction survives
    re-ingestion of whichever document was wrong.
    """
    from product_intel.review import apply_correction
    from product_intel.validation import compute_quality, validate_product

    product = STATE.product(req.product_id)
    if product is None:
        raise HTTPException(404, f"No product matching '{req.product_id}'")

    schema = STATE.taxonomy.get(product.category_id)
    if schema.get(req.code) is None:
        raise HTTPException(400, f"'{req.code}' is not an attribute of {schema.name}")

    old_value = product.attributes.get(req.code)
    old = old_value.value if old_value else None

    av = apply_correction(product, req.code, req.value, schema, req.reviewer, req.note)
    if av.value is None:
        raise HTTPException(
            422,
            {"message": "Value rejected by the schema", "errors": av.validation_errors},
        )

    from product_intel.pipeline.golden import build_golden_record

    build_golden_record(product, schema)
    report = validate_product(product, schema, mirrors=STATE.store.load_all_mirrors())
    product.quality = compute_quality(product, schema, report, STATE.cfg)

    learned = STATE.engine.learned.learn_from_correction(
        product, req.code, old, av.value, req.reviewer
    )
    STATE.engine.learned.flush()

    STATE.store.save_product(product)

    queue = STATE.review_queue()
    if req.flag_id:
        queue.resolve(req.flag_id, f"corrected to '{av.value}'", req.reviewer)
        queue.flush()

    # Keep the analytics store in step so a dashboard query does not disagree
    # with the product page the reviewer is looking at.
    try:
        from product_intel.pipeline.db_ingest import CatalogDB

        CatalogDB(STATE.cfg).upsert([product])
    except Exception as exc:  # noqa: BLE001 - the correction itself already succeeded
        log.warning("could not update the analytics database: %s", exc)

    STATE.invalidate(graph=False, assets=False)

    return S.CorrectionResponse(
        ok=True,
        product_id=product.identity.product_id,
        code=req.code,
        applied_value=av.value,
        confidence=round(av.confidence, 4),
        validation_errors=av.validation_errors,
        learned_rule=learned,
        quality=quality_view(product.quality),
    )


@router.post("/review/{flag_id}/resolve")
def resolve_flag(flag_id: str, req: S.FlagResolveRequest) -> Dict[str, Any]:
    queue = STATE.review_queue()
    flag = queue.resolve(flag_id, req.resolution, req.reviewer)
    if flag is None:
        raise HTTPException(404, f"No flag '{flag_id}'")
    queue.flush()
    return {"ok": True, "flag_id": flag_id, "stats": queue.stats()}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get("/search", response_model=S.SearchResponse)
def search(
    q: str,
    limit: int = Query(20, le=100),
    category: Optional[str] = None,
    manufacturer: Optional[str] = None,
    ready_only: bool = False,
) -> S.SearchResponse:
    """
    Hybrid search: semantic when an index exists, attribute matching always.

    Attribute matching is not a fallback -- a trade buyer searching "63A 3 pole"
    wants exact spec matches, and embeddings are worse at that than a direct
    comparison. Semantic retrieval covers the descriptive half of the query.
    """
    products = STATE.products()
    taxonomy = STATE.taxonomy
    flags = STATE.open_flag_counts()

    def keep(p) -> bool:
        if category and p.category_id != category:
            return False
        if manufacturer and p.identity.manufacturer != manufacturer:
            return False
        if ready_only and not p.quality.channel_ready:
            return False
        return True

    scored: Dict[str, Dict[str, Any]] = {}

    # -- semantic ----------------------------------------------------------
    semantic_available = False
    try:
        from product_intel.pipeline.embedder import VectorIndex, embed_texts

        index = VectorIndex.load(STATE.cfg)
        if index.size() and STATE.cfg.embedding_enabled:
            vectors, _ = embed_texts([q], STATE.cfg)
            if vectors and vectors[0]:
                semantic_available = True
                for pid, score in index.search(vectors[0], k=limit * 3):
                    if pid in products and keep(products[pid]):
                        scored[pid] = {"score": float(score), "kind": "semantic", "matched": []}
    except Exception as exc:  # noqa: BLE001 - search must never 500
        log.info("semantic search unavailable: %s", exc)

    # -- attribute / identity ---------------------------------------------
    tokens = [t for t in re.split(r"[\s,]+", q.lower()) if len(t) > 1]
    numbers = re.findall(r"\d+(?:\.\d+)?", q)

    for pid, p in products.items():
        if not keep(p):
            continue
        matched: List[str] = []
        score = 0.0

        haystack = f"{p.identity.manufacturer} {p.identity.mpn} {p.display_name()}".lower()
        if q.lower().strip() in haystack:
            score += 3.0
            matched.append("identity")

        schema = taxonomy.get(p.category_id)
        for code, av in p.attributes.items():
            attr = schema.get(code)
            if attr is None or av.value in (None, "", []):
                continue
            rendered = render_value(av, attr).lower()
            label = attr.name.lower()
            for token in tokens:
                if token in rendered or token in label:
                    score += 0.6 if attr.variant_defining else 0.3
                    if attr.name not in matched:
                        matched.append(attr.name)
            for number in numbers:
                if attr.is_numeric and isinstance(av.value, (int, float)):
                    try:
                        if abs(float(av.value) - float(number)) < 0.01:
                            score += 1.2
                            if attr.name not in matched:
                                matched.append(attr.name)
                    except (TypeError, ValueError):
                        pass

        if score > 0:
            existing = scored.get(pid)
            if existing is None:
                scored[pid] = {
                    "score": min(score / 4.0, 1.0),
                    "kind": "identity" if "identity" in matched else "attribute",
                    "matched": matched,
                }
            else:
                # Blend: a product that matches both ways should outrank one
                # that matches only semantically.
                existing["score"] = min(1.0, existing["score"] + score / 8.0)
                existing["matched"] = matched

    ranked = sorted(scored.items(), key=lambda kv: kv[1]["score"], reverse=True)[:limit]
    hits = [
        S.SearchHit(
            product=product_summary(products[pid], taxonomy, flags.get(pid, 0)),
            score=round(meta["score"], 4),
            match_kind=meta["kind"],
            matched_on=meta["matched"][:6],
        )
        for pid, meta in ranked
    ]

    return S.SearchResponse(
        query=q,
        hits=hits,
        total=len(scored),
        semantic_available=semantic_available,
        filters_applied={
            k: v for k, v in
            {"category": category, "manufacturer": manufacturer, "ready_only": ready_only}.items()
            if v
        },
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@router.get("/schema", response_model=List[S.SchemaCategoryView])
def get_schema() -> List[S.SchemaCategoryView]:
    counts = Counter(p.category_id for p in STATE.products().values())
    return [
        S.SchemaCategoryView(
            id=cid,
            name=cat.name,
            vertical=cat.vertical,
            etim=cat.etim,
            unspsc=cat.unspsc,
            attribute_count=len(cat.attributes),
            required_core=len(cat.required_codes("core")),
            required_ecommerce=len(cat.required_codes("ecommerce")),
            rules=len(cat.rules),
            product_count=counts.get(cid, 0),
        )
        for cid, cat in STATE.taxonomy.categories.items()
    ]


@router.get("/schema/{category_id}")
def get_category_schema(category_id: str) -> Dict[str, Any]:
    taxonomy = STATE.taxonomy
    if category_id not in taxonomy.categories:
        raise HTTPException(404, f"No category '{category_id}'")
    cat = taxonomy.categories[category_id]
    return {
        "id": cat.id,
        "name": cat.name,
        "vertical": cat.vertical,
        "etim": cat.etim,
        "unspsc": cat.unspsc,
        "attributes": [
            {
                "code": code,
                "name": a.name,
                "datatype": a.datatype,
                "unit": a.canonical_unit,
                "allowed_values": a.allowed_values,
                "required_for": a.required_for,
                "variant_defining": a.variant_defining,
                "generated": a.generated,
                "aliases": a.aliases[:8],
            }
            for code, a in cat.attributes.items()
        ],
        "rules": [
            {"id": r.id, "type": r.type, "message": r.message} for r in cat.rules
        ],
    }


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------


def _llm_status(cfg) -> S.LLMStatus:
    import os

    from product_intel.llm.provider import (
        BEDROCK_SUGGESTED_MODELS,
        OLLAMA_SUGGESTED_MODELS,
        get_provider,
    )

    disabled = not cfg.llm_enabled or cfg.llm_provider == "null"
    provider = get_provider(cfg)
    available = False if disabled else provider.available

    if disabled:
        mode, detail, remediation = "off", "No model will be called. Deterministic extraction only.", []
    elif cfg.is_offline:
        mode = "offline"
        detail = "Running on this machine. No request leaves the building."
        remediation = [] if available else [
            "ollama serve",
            f"ollama pull {cfg.active_model}",
        ]
    else:
        mode = "cloud"
        detail = f"Calling AWS Bedrock in {cfg.aws_region}."
        remediation = [] if available else [
            f"{cfg.aws_access_key_id_env}=AKIA...",
            f"{cfg.aws_secret_access_key_env}=...",
            "...in the .env file at the project root, or run: aws configure",
        ]

    suggestions = (
        OLLAMA_SUGGESTED_MODELS if cfg.llm_provider == "ollama" else BEDROCK_SUGGESTED_MODELS
    )

    return S.LLMStatus(
        provider=cfg.llm_provider,
        mode=mode,
        model=None if disabled else cfg.active_model,
        enabled=cfg.llm_enabled,
        available=available,
        detail=detail,
        remediation=remediation,
        region=cfg.aws_region if cfg.llm_provider == "bedrock" else None,
        credential_source=cfg.aws_credential_source() if cfg.llm_provider == "bedrock" else None,
        endpoint=cfg.ollama_base_url if cfg.llm_provider == "ollama" else None,
        suggested_models=[{"id": n, "note": d} for n, d in suggestions],
        env_shadowing=[
            k for k in ("PI_LLM_PROVIDER", "PI_LLM_ENABLED", "PI_LLM_MODEL") if k in os.environ
        ],
    )


@router.get("/llm", response_model=S.LLMStatus)
def llm_status() -> S.LLMStatus:
    return _llm_status(STATE.refresh_settings())


@router.post("/llm", response_model=S.LLMStatus)
def llm_switch(req: S.LLMSwitchRequest) -> S.LLMStatus:
    from product_intel.config import save_settings

    updates: Dict[str, Any] = {}
    if req.provider == "off":
        updates["llm_enabled"] = False
        updates["llm_provider"] = "null"
    else:
        updates["llm_enabled"] = True
        updates["llm_provider"] = req.provider
        updates["llm_model"] = None
        if req.model:
            updates[f"{req.provider}_model"] = req.model
        if req.provider == "bedrock":
            if req.region:
                updates["aws_region"] = req.region
            if req.profile:
                updates["aws_profile"] = req.profile

    try:
        save_settings(updates)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not save settings: {exc}")

    return _llm_status(STATE.refresh_settings())


@router.post("/llm/test")
def llm_test() -> Dict[str, Any]:
    import time

    from product_intel.llm.provider import LLMUnavailable, get_provider

    cfg = STATE.refresh_settings()
    if not cfg.llm_enabled:
        return {"ok": False, "error": "LLM is disabled.", "elapsed_s": 0.0}

    started = time.time()
    try:
        result = get_provider(cfg).complete_json(
            'Reply with exactly this JSON and nothing else: {"ok": true}', expect="object"
        )
        return {"ok": True, "response": result, "elapsed_s": round(time.time() - started, 2)}
    except LLMUnavailable as exc:
        return {"ok": False, "error": str(exc)[:400], "elapsed_s": round(time.time() - started, 2)}


# ---------------------------------------------------------------------------
# Jobs: ingest and build
# ---------------------------------------------------------------------------


@router.post("/jobs/ingest", response_model=S.JobStatus)
def start_ingest(
    path: Optional[str] = None,
    force: bool = False,
    build_after: bool = True,
) -> S.JobStatus:
    target = Path(path) if path else STATE.cfg.sources_path
    if not target.exists():
        raise HTTPException(400, f"Path does not exist: {target}")

    def work(job) -> Dict[str, Any]:
        job.emit(f"scanning {target}", 0.05)
        engine = STATE.engine
        job.emit(f"LLM backend: {engine.provider.name} "
                 f"({'available' if engine.provider.available else 'unavailable, deterministic only'})", 0.1)

        report = engine.ingest([target], force=force)
        job.emit(
            f"ingested {report.sources_processed} sources "
            f"({report.sources_skipped} unchanged), {report.products_created} products created",
            0.55,
        )
        STATE.invalidate()

        result: Dict[str, Any] = {"ingest": report.as_dict()}
        if build_after:
            job.emit("enriching, validating and scoring", 0.6)
            out = engine.build()
            STATE.invalidate()
            result["build"] = out
            after = out.get("after", {})
            job.emit(
                f"built: {after.get('channel_ready', 0)} of {after.get('sellable', 0)} "
                f"SKUs channel-ready",
                0.98,
            )
        return result

    try:
        job = JOBS.submit("ingest", work)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return S.JobStatus(**job.to_dict())


@router.post("/jobs/build", response_model=S.JobStatus)
def start_build(no_enrich: bool = False, no_index: bool = False) -> S.JobStatus:
    def work(job) -> Dict[str, Any]:
        job.emit("loading catalog", 0.05)
        engine = STATE.engine
        job.emit("enriching, validating and scoring", 0.2)
        out = engine.build(enrich=not no_enrich, index=not no_index)
        STATE.invalidate()
        after = out.get("after", {})
        job.emit(
            f"{after.get('channel_ready', 0)} of {after.get('sellable', 0)} SKUs channel-ready",
            0.98,
        )
        return out

    try:
        job = JOBS.submit("build", work)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return S.JobStatus(**job.to_dict())


@router.get("/jobs/{job_id}", response_model=S.JobStatus)
def job_status(job_id: str) -> S.JobStatus:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"No job '{job_id}'")
    return S.JobStatus(**job.to_dict())


@router.get("/jobs", response_model=List[S.JobStatus])
def recent_jobs() -> List[S.JobStatus]:
    return [S.JobStatus(**j.to_dict()) for j in JOBS.recent()]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@router.post("/export")
def export(req: S.ExportRequest) -> Dict[str, Any]:
    products = list(STATE.products().values())
    if not products:
        raise HTTPException(400, "Catalog is empty.")

    exporter = EXPORTERS.get(req.fmt)
    if exporter is None:
        raise HTTPException(400, f"Unknown format '{req.fmt}'")

    out_dir = STATE.cfg.catalog_path / "exports"
    path = out_dir / f"catalog{DEFAULT_EXTENSIONS[req.fmt]}"
    result = exporter(products, STATE.taxonomy, path, ready_only=req.ready_only)
    result["download"] = f"/api/export/{req.fmt}/download"
    result["bytes"] = path.stat().st_size if path.exists() else 0
    return result


@router.get("/export/{fmt}/download")
def download_export(fmt: str) -> FileResponse:
    if fmt not in DEFAULT_EXTENSIONS:
        raise HTTPException(400, f"Unknown format '{fmt}'")
    path = STATE.cfg.catalog_path / "exports" / f"catalog{DEFAULT_EXTENSIONS[fmt]}"
    if not path.exists():
        raise HTTPException(404, "Nothing exported yet. Run the export first.")
    return FileResponse(
        path,
        filename=f"product-catalog{DEFAULT_EXTENSIONS[fmt]}",
        media_type="application/octet-stream",
    )


@router.get("/health")
def health() -> Dict[str, Any]:
    running = JOBS.running_job()
    return {
        "ok": True,
        "products": len(STATE.products()),
        "catalog_built": STATE.catalog_built(),
        "job_running": running.job_id if running and running.state == "running" else None,
    }
