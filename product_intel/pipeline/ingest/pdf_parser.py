"""
PDF parsing into provenance-tagged fragments.

Two things this does that the predecessor did not:

1. Tables keep their shape. A spec table is emitted as a `table` Fragment with
   its rows intact, so the extractor can reason about which *column* a value
   belongs to. Flattening tables into prose is what produced the wrong-column
   bug in the old pipeline -- a variant table with three columns would silently
   yield the wrong variant's number.

2. Pages with no text layer are not silently dropped. They are routed to OCR
   when an engine is available, and when it is not they are recorded as an
   explicit gap in the audit log rather than vanishing.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from product_intel.models import ExtractionMethod, Fragment

log = logging.getLogger(__name__)


def _frag_id(source_id: str, page: Optional[int], seq: int) -> str:
    return f"{source_id}_p{page or 0}_f{seq}"


def heal_split_numbers(text: str) -> str:
    """
    Repair digit-splitting artifacts introduced by PDF text extraction.

    '2 15,719' -> '215,719'   '8 ,927' -> '8,927'   '1 ,200' -> '1,200'

    Deliberately conservative: it only rejoins digits around a comma group.
    The predecessor also rejoined bare digit pairs ('9 93' -> '993'), which
    corrupted legitimately separate table cells such as '9  93' meaning two
    columns. That rule is gone.
    """
    if not text:
        return text
    text = re.sub(r"(?<![\d.,])(\d{1,3})\s+(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?![\d])", r"\1\2", text)
    text = re.sub(r"(\d)\s+,\s*(\d{3})\b", r"\1,\2", text)
    text = re.sub(r"(\d),\s+(\d{3})\b", r"\1,\2", text)
    return text


def _clean_cell(cell: Any) -> str:
    if cell is None:
        return ""
    return heal_split_numbers(re.sub(r"\s+", " ", str(cell)).strip())


def _table_to_markdown(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(padded[0]) + " |", "|" + "---|" * width]
    for r in padded[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _try_ocr(page: Any, page_num: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt OCR on a page with no text layer.

    Returns (text, engine_name). Engines are tried in order of quality and the
    absence of all of them is reported honestly rather than papered over.
    """
    try:
        image = page.to_image(resolution=300).original
    except Exception as exc:  # noqa: BLE001
        return None, f"page rasterization failed: {exc}"

    try:
        import pytesseract  # type: ignore

        text = pytesseract.image_to_string(image)
        if text and text.strip():
            return text, "tesseract"
        return None, "tesseract returned no text"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return None, f"tesseract failed: {exc}"

    try:
        import easyocr  # type: ignore
        import numpy as np

        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        lines = reader.readtext(np.array(image), detail=0)
        if lines:
            return "\n".join(lines), "easyocr"
        return None, "easyocr returned no text"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return None, f"easyocr failed: {exc}"

    return None, "no OCR engine installed (pip install pytesseract or easyocr)"


def parse_pdf(
    path: Path,
    source_id: str,
    enable_ocr: bool = True,
) -> Tuple[List[Fragment], str, Dict[str, Any]]:
    """
    Parse a PDF into fragments plus a canonical Markdown mirror.

    Returns (fragments, markdown_mirror, stats). The mirror is the
    human-readable artifact that survives the AI: every quote stored anywhere
    in the system must be locatable inside it.
    """
    import pdfplumber

    fragments: List[Fragment] = []
    mirror: List[str] = []
    stats: Dict[str, Any] = {
        "pages": 0,
        "tables": 0,
        "text_pages": 0,
        "ocr_pages": 0,
        "empty_pages": 0,
        "ocr_engine": None,
        "warnings": [],
    }
    seq = 0

    with pdfplumber.open(path) as pdf:
        stats["pages"] = len(pdf.pages)
        if not pdf.pages:
            raise ValueError(f"{path.name}: PDF contains no pages")

        for idx, page in enumerate(pdf.pages):
            page_num = idx + 1
            mirror.append(f"\n<!-- PAGE_START: {page_num} -->\n")
            mirror.append(f"## Page {page_num}\n")

            raw_text = page.extract_text() or ""
            method = ExtractionMethod.NATIVE_TEXT

            if not raw_text.strip() and enable_ocr:
                ocr_text, engine = _try_ocr(page, page_num)
                if ocr_text:
                    raw_text = ocr_text
                    method = ExtractionMethod.OCR
                    stats["ocr_pages"] += 1
                    stats["ocr_engine"] = engine
                    mirror.append(f"\n> _[Recovered by OCR ({engine}); treat values as lower confidence.]_\n")
                else:
                    stats["empty_pages"] += 1
                    stats["warnings"].append(f"page {page_num}: no text layer and {engine}")
                    mirror.append(
                        f"\n> _[NO TEXT LAYER on page {page_num}. {engine}. "
                        f"This page's content is NOT represented in the knowledge layer.]_\n"
                    )
            elif raw_text.strip():
                stats["text_pages"] += 1
            else:
                stats["empty_pages"] += 1
                stats["warnings"].append(f"page {page_num}: no text layer (OCR disabled)")

            text = heal_split_numbers(raw_text)
            if text.strip():
                mirror.append("\n" + text + "\n")

                # The first line of page 1 of a datasheet or submittal is
                # essentially always the manufacturer's name. Emitting it as a
                # heading gives identity resolution the same anchor that HTML
                # gets from <h1>.
                if page_num == 1:
                    first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
                    if 2 < len(first_line) < 80:
                        seq += 1
                        fragments.append(
                            Fragment(
                                fragment_id=_frag_id(source_id, page_num, seq),
                                source_id=source_id,
                                kind="heading",
                                page=page_num,
                                locator=f"p.{page_num} / title",
                                text=first_line,
                                method=method,
                            )
                        )

                for block in re.split(r"\n\s*\n", text):
                    block = block.strip()
                    if len(block) < 25:
                        continue
                    seq += 1
                    fragments.append(
                        Fragment(
                            fragment_id=_frag_id(source_id, page_num, seq),
                            source_id=source_id,
                            kind="text",
                            page=page_num,
                            locator=f"p.{page_num}",
                            text=block,
                            method=method,
                        )
                    )

            # -- tables, shape preserved ------------------------------------
            try:
                tables = page.extract_tables()
            except Exception as exc:  # noqa: BLE001
                tables = []
                stats["warnings"].append(f"page {page_num}: table extraction failed ({exc})")

            for t_idx, table in enumerate(tables):
                rows = [[_clean_cell(c) for c in row] for row in table if row]
                rows = [r for r in rows if any(c for c in r)]
                if len(rows) < 2:
                    continue
                stats["tables"] += 1
                seq += 1
                md = _table_to_markdown(rows)
                mirror.append(f"\n### Table {t_idx + 1} (Page {page_num})\n\n{md}\n")
                fragments.append(
                    Fragment(
                        fragment_id=_frag_id(source_id, page_num, seq),
                        source_id=source_id,
                        kind="table",
                        page=page_num,
                        locator=f"p.{page_num} / Table {t_idx + 1}",
                        text=md,
                        table=rows,
                        method=ExtractionMethod.NATIVE_TABLE,
                        metadata={"table_index": t_idx + 1, "rows": len(rows), "cols": len(rows[0])},
                    )
                )

            mirror.append(f"\n<!-- PAGE_END: {page_num} -->\n")

    markdown = "".join(mirror)
    if len(re.sub(r"\s+", "", markdown)) < 50:
        raise ValueError(f"{path.name}: parsed mirror is effectively empty")

    stats["fragments"] = len(fragments)
    return fragments, markdown, stats


def mirror_checksum(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
