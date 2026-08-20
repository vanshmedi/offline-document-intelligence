"""
HTML / product-page parsing.

Manufacturer web pages are the second-biggest source of product data after
datasheets, and the messiest: specs live in <table>, in <dl>, in bulleted
"key: value" lists, and in prose. All four shapes are handled and each emits a
fragment with a CSS-ish locator so a value can be traced back to the element
it came from.

Embedded schema.org / JSON-LD Product blocks are treated as a structured feed
rather than scraped text, because they are machine-authored and deserve a
higher reliability weight.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from product_intel.models import ExtractionMethod, Fragment

log = logging.getLogger(__name__)

_DROP_TAGS = ("script", "style", "nav", "footer", "header", "noscript", "svg", "form")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_html(
    path_or_html: Any,
    source_id: str,
    is_path: bool = True,
) -> Tuple[List[Fragment], str, Dict[str, Any]]:
    """Parse an HTML file (or raw string) into fragments plus a Markdown mirror."""
    from bs4 import BeautifulSoup

    if is_path:
        html = Path(path_or_html).read_text(encoding="utf-8", errors="replace")
    else:
        html = str(path_or_html)

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()

    fragments: List[Fragment] = []
    mirror: List[str] = []
    stats: Dict[str, Any] = {"tables": 0, "keyvalues": 0, "jsonld": 0, "text_blocks": 0, "warnings": []}
    seq = 0

    title = _clean(soup.title.get_text()) if soup.title else ""
    h1 = _clean(soup.h1.get_text()) if soup.h1 else ""
    page_title = h1 or title
    mirror.append(f"# {page_title}\n\n" if page_title else "")

    if page_title:
        seq += 1
        fragments.append(
            Fragment(
                fragment_id=f"{source_id}_f{seq}",
                source_id=source_id,
                kind="heading",
                locator="h1" if h1 else "title",
                text=page_title,
                method=ExtractionMethod.NATIVE_TEXT,
            )
        )

    # -- JSON-LD (schema.org Product) ---------------------------------------
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            stats["warnings"].append("malformed JSON-LD block skipped")
            continue
        blocks = payload if isinstance(payload, list) else [payload]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if "Product" not in str(block.get("@type", "")):
                continue
            stats["jsonld"] += 1
            seq += 1
            pairs = _flatten_jsonld(block)
            mirror.append("\n### Structured Product Data (JSON-LD)\n\n")
            mirror.extend(f"- **{k}**: {v}\n" for k, v in pairs)
            fragments.append(
                Fragment(
                    fragment_id=f"{source_id}_f{seq}",
                    source_id=source_id,
                    kind="keyvalue",
                    locator="script[type=application/ld+json]",
                    text="\n".join(f"{k}: {v}" for k, v in pairs),
                    table=[[k, v] for k, v in pairs],
                    method=ExtractionMethod.STRUCTURED_FEED,
                    metadata={"jsonld": True},
                )
            )

    # -- spec tables --------------------------------------------------------
    for t_idx, table in enumerate(soup.find_all("table")):
        rows: List[List[str]] = []
        for tr in table.find_all("tr"):
            cells = [_clean(td.get_text()) for td in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if len(rows) < 2:
            continue
        stats["tables"] += 1
        seq += 1
        md = _rows_to_md(rows)
        caption = _clean(table.caption.get_text()) if table.caption else f"Table {t_idx + 1}"
        mirror.append(f"\n### {caption}\n\n{md}\n")
        fragments.append(
            Fragment(
                fragment_id=f"{source_id}_f{seq}",
                source_id=source_id,
                kind="table",
                locator=f"table:nth-of-type({t_idx + 1})",
                text=md,
                table=rows,
                method=ExtractionMethod.NATIVE_TABLE,
                metadata={"caption": caption},
            )
        )

    # -- definition lists ---------------------------------------------------
    for dl_idx, dl in enumerate(soup.find_all("dl")):
        pairs: List[List[str]] = []
        terms = dl.find_all("dt")
        for dt in terms:
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                key, val = _clean(dt.get_text()), _clean(dd.get_text())
                if key and val:
                    pairs.append([key, val])
        if not pairs:
            continue
        stats["keyvalues"] += 1
        seq += 1
        mirror.append("\n### Specifications\n\n")
        mirror.extend(f"- **{k}**: {v}\n" for k, v in pairs)
        fragments.append(
            Fragment(
                fragment_id=f"{source_id}_f{seq}",
                source_id=source_id,
                kind="keyvalue",
                locator=f"dl:nth-of-type({dl_idx + 1})",
                text="\n".join(f"{k}: {v}" for k, v in pairs),
                table=pairs,
                method=ExtractionMethod.NATIVE_TEXT,
            )
        )

    # -- "Key: value" bullet lists ------------------------------------------
    for ul_idx, ul in enumerate(soup.find_all(["ul", "ol"])):
        pairs = []
        bullets = []
        for li in ul.find_all("li", recursive=False):
            text = _clean(li.get_text())
            if not text:
                continue
            bullets.append(text)
            m = re.match(r"^([A-Za-z][\w\s/&().\-]{2,45}?)\s*[:–—-]\s+(.{1,160})$", text)
            if m:
                pairs.append([_clean(m.group(1)), _clean(m.group(2))])
        if not bullets:
            continue
        seq += 1
        if len(pairs) >= 2:
            stats["keyvalues"] += 1
            mirror.extend(f"- **{k}**: {v}\n" for k, v in pairs)
            fragments.append(
                Fragment(
                    fragment_id=f"{source_id}_f{seq}",
                    source_id=source_id,
                    kind="keyvalue",
                    locator=f"ul:nth-of-type({ul_idx + 1})",
                    text="\n".join(f"{k}: {v}" for k, v in pairs),
                    table=pairs,
                    method=ExtractionMethod.NATIVE_TEXT,
                )
            )
        else:
            mirror.extend(f"- {b}\n" for b in bullets)
            fragments.append(
                Fragment(
                    fragment_id=f"{source_id}_f{seq}",
                    source_id=source_id,
                    kind="text",
                    locator=f"ul:nth-of-type({ul_idx + 1})",
                    text="\n".join(bullets),
                    method=ExtractionMethod.NATIVE_TEXT,
                )
            )

    # -- prose, and short "Key: value" paragraphs ---------------------------
    # Short paragraphs matter: "Manufacturer: Acme" and "Part Number: X-1" are
    # often the only place a product page states its identity, and a naive
    # minimum-length filter drops exactly those lines.
    short_pairs: List[List[str]] = []
    for p_idx, para in enumerate(soup.find_all("p")):
        text = _clean(para.get_text())
        if not text:
            continue

        if len(text) < 40:
            m = re.match(r"^([A-Za-z][\w\s/&().\-]{2,40}?)\s*[:–—]\s*(.{1,80})$", text)
            if m:
                short_pairs.append([_clean(m.group(1)), _clean(m.group(2))])
            continue

        stats["text_blocks"] += 1
        seq += 1
        mirror.append(f"\n{text}\n")
        fragments.append(
            Fragment(
                fragment_id=f"{source_id}_f{seq}",
                source_id=source_id,
                kind="text",
                locator=f"p:nth-of-type({p_idx + 1})",
                text=text,
                method=ExtractionMethod.NATIVE_TEXT,
            )
        )

    if short_pairs:
        stats["keyvalues"] += 1
        seq += 1
        mirror.append("\n### Page Details\n\n")
        mirror.extend(f"- **{k}**: {v}\n" for k, v in short_pairs)
        fragments.append(
            Fragment(
                fragment_id=f"{source_id}_f{seq}",
                source_id=source_id,
                kind="keyvalue",
                locator="p (inline key/value)",
                text="\n".join(f"{k}: {v}" for k, v in short_pairs),
                table=short_pairs,
                method=ExtractionMethod.NATIVE_TEXT,
            )
        )

    stats["fragments"] = len(fragments)
    return fragments, "".join(mirror), stats


def _rows_to_md(rows: List[List[str]]) -> str:
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(padded[0]) + " |", "|" + "---|" * width]
    out.extend("| " + " | ".join(r) + " |" for r in padded[1:])
    return "\n".join(out)


def _flatten_jsonld(block: Dict[str, Any], prefix: str = "") -> List[Tuple[str, str]]:
    """Flatten a schema.org Product block into key/value pairs."""
    pairs: List[Tuple[str, str]] = []
    for key, val in block.items():
        if key.startswith("@"):
            continue
        label = f"{prefix}{key}"
        if isinstance(val, dict):
            # PropertyValue pairs carry their own name/value.
            if "name" in val and "value" in val:
                pairs.append((str(val["name"]), str(val["value"])))
            else:
                pairs.extend(_flatten_jsonld(val, prefix=f"{label}."))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    pairs.append((str(item["name"]), str(item["value"])))
                elif isinstance(item, dict):
                    pairs.extend(_flatten_jsonld(item, prefix=f"{label}."))
                else:
                    pairs.append((label, str(item)))
        elif val is not None:
            pairs.append((label, str(val)))
    return pairs
