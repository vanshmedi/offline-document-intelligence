"""
Source-type dispatch.

Adding a new input format means adding one entry here and one parser function.
Nothing in the rest of the pipeline knows or cares what a source was born as --
everything downstream consumes Fragments.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from product_intel.models import Fragment, SourceKind

log = logging.getLogger(__name__)

ParserFn = Callable[..., Tuple[List[Fragment], str, Dict[str, Any]]]

EXTENSION_MAP: Dict[str, str] = {
    ".pdf": "pdf",
    ".html": "html",
    ".htm": "html",
    ".csv": "tabular",
    ".tsv": "tabular",
    ".xlsx": "tabular",
    ".xlsm": "tabular",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
}

#: Filename hints that suggest what kind of source this is, and therefore how
#: much to trust it during golden-record arbitration.
KIND_HINTS: List[Tuple[Tuple[str, ...], SourceKind]] = [
    (("datasheet", "data-sheet", "spec", "technical", "submittal"), SourceKind.DATASHEET),
    (("catalog", "catalogue", "brochure"), SourceKind.CATALOG),
    (("price", "pricelist", "price-list", "pricing"), SourceKind.PRICE_FILE),
    (("erp", "export", "extract", "feed"), SourceKind.ERP_EXPORT),
    (("web", "page", "product-page", "www"), SourceKind.MANUFACTURER_WEB),
    (("distributor", "reseller"), SourceKind.DISTRIBUTOR_WEB),
]


def content_type_for(path: Path) -> str:
    ct = EXTENSION_MAP.get(path.suffix.lower())
    if ct is None:
        raise ValueError(f"Unsupported file type '{path.suffix}' for {path.name}")
    return ct


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in EXTENSION_MAP


def infer_kind(path: Path, default: SourceKind = SourceKind.DATASHEET) -> SourceKind:
    """Guess the source kind from the filename and its parent folders."""
    haystack = "/".join(p.lower() for p in path.parts[-3:])
    for needles, kind in KIND_HINTS:
        if any(n in haystack for n in needles):
            return kind
    ct = EXTENSION_MAP.get(path.suffix.lower())
    if ct == "html":
        return SourceKind.MANUFACTURER_WEB
    if ct == "tabular":
        return SourceKind.PRICE_FILE
    if ct == "image":
        return SourceKind.IMAGE
    return default


def checksum_file(path: Path) -> str:
    """SHA-256, streamed. Deterministic source IDs derive from this."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def source_id_for(checksum: str) -> str:
    return f"src_{checksum[:16]}"


def parse_source(
    path: Path,
    source_id: str,
    enable_ocr: bool = True,
) -> Tuple[List[Fragment], str, Dict[str, Any]]:
    """Dispatch to the right parser and return (fragments, mirror, stats)."""
    ct = content_type_for(path)

    if ct == "pdf":
        from product_intel.pipeline.ingest.pdf_parser import parse_pdf

        return parse_pdf(path, source_id, enable_ocr=enable_ocr)

    if ct == "html":
        from product_intel.pipeline.ingest.web_parser import parse_html

        return parse_html(path, source_id, is_path=True)

    if ct == "tabular":
        from product_intel.pipeline.ingest.tabular_parser import parse_tabular

        return parse_tabular(path, source_id)

    if ct == "image":
        from product_intel.pipeline.ingest.image_parser import parse_image

        return parse_image(path, source_id)

    raise ValueError(f"No parser registered for content type '{ct}'")
