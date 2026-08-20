"""
Schema-directed attribute extraction.

This is the core behavioural change from the predecessor pipeline. The old
extractor asked a language model "find every financial figure on this page",
which produced free-form attribute names, wrong-column values, and one LLM call
per page whether or not the page had anything in it.

Here, extraction is directed by the attribute dictionary and runs in two tiers:

  Tier 1 -- deterministic. Spec tables, key/value blocks and labelled patterns
  are resolved against the dictionary's aliases with no model at all. On real
  datasheets this recovers the large majority of attributes, at zero inference
  cost, with exact provenance and a verifiable quote.

  Tier 2 -- LLM gap-fill. Only the attributes Tier 1 could not find, and only
  over the fragments plausibly containing them, are sent to a model. The prompt
  names the exact attributes, their datatypes and their legal values, and
  demands a verbatim quote for each answer.

A value whose quote cannot be found in the source is discarded rather than
"healed". The predecessor snapped a wrong number to the nearest number on the
same line, which quietly produced confident, wrong data -- exactly the failure
mode this system exists to prevent.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from product_intel.config import Settings, settings as global_settings
from product_intel.llm.provider import LLMProvider, LLMUnavailable
from product_intel.models import (
    AttributeValue,
    Evidence,
    ExtractionMethod,
    Fragment,
    SourceKind,
)
from product_intel.pipeline.normalizer import normalize_value, parse_number
from product_intel.schema.dictionary import AttributeDef, CategorySchema

log = logging.getLogger(__name__)

#: Column headers in a variant table that identify the row, not a spec value.
_KEY_COLUMN_HINTS = (
    "part", "catalog", "cat no", "model", "order", "mpn", "sku", "item", "type", "code", "reference"
)


def _norm_label(text: str) -> str:
    """Normalize a table row label for alias matching."""
    text = re.sub(r"\(.*?\)", " ", str(text or ""))          # drop parenthetical units
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def match_attribute(label: str, schema: CategorySchema) -> Optional[AttributeDef]:
    """
    Resolve a document's row label to a canonical attribute.

    Longest alias wins, so 'rated operational voltage' beats 'voltage' and
    'min operating temperature' beats 'operating temperature'.
    """
    norm = _norm_label(label)
    if not norm:
        return None

    best: Optional[AttributeDef] = None
    best_len = 0
    for attr in schema.attributes.values():
        if attr.generated or attr.identity:
            continue
        for alias in attr.alias_patterns():
            a = _norm_label(alias)
            if not a:
                continue
            if norm == a:
                if len(a) > best_len:
                    best, best_len = attr, len(a) + 100  # exact match outranks partial
            elif re.search(rf"(?<!\w){re.escape(a)}(?!\w)", norm) and len(a) > best_len:
                best, best_len = attr, len(a)
    return best


def _split_range(text: str) -> Optional[Tuple[float, float]]:
    """Parse '-20 to +70 C' / '-4...158 F' / '-20 ~ 70' into (low, high)."""
    if not text:
        return None
    s = str(text)
    m = re.search(
        r"([-+−]?\s*\d+(?:[.,]\d+)?)\s*(?:to|\.\.\.|\.\.|~|–|—|/|through)\s*([-+−]?\s*\d+(?:[.,]\d+)?)",
        s,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    lo, hi = parse_number(m.group(1)), parse_number(m.group(2))
    if lo is None or hi is None:
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def _range_partner(attr: AttributeDef, schema: CategorySchema) -> Optional[Tuple[AttributeDef, AttributeDef]]:
    """
    If `attr` is one half of a min/max pair, return (min_attr, max_attr).

    Datasheets almost always write 'Operating Temperature: -20 to 70 C' as a
    single row, but the schema stores two attributes. This bridges the two.
    """
    for lo_suffix, hi_suffix in (("_min_c", "_max_c"), ("_min", "_max")):
        if attr.code.endswith(lo_suffix):
            partner = schema.get(attr.code[: -len(lo_suffix)] + hi_suffix)
            if partner:
                return attr, partner
        if attr.code.endswith(hi_suffix):
            partner = schema.get(attr.code[: -len(hi_suffix)] + lo_suffix)
            if partner:
                return partner, attr
    return None


class ExtractionResult:
    """Attribute observations pulled from one source for one product."""

    def __init__(self) -> None:
        self.values: Dict[str, List[AttributeValue]] = {}
        self.llm_calls = 0
        self.deterministic_hits = 0
        self.llm_hits = 0
        self.rejected_quotes = 0

    def add(self, av: AttributeValue) -> None:
        if av.value is None:
            return
        self.values.setdefault(av.code, []).append(av)

    def codes(self) -> List[str]:
        return list(self.values.keys())

    def flat(self) -> List[AttributeValue]:
        return [v for vs in self.values.values() for v in vs]


class SchemaDirectedExtractor:
    """Extracts a specific, known set of attributes from a set of fragments."""

    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        cfg: Optional[Settings] = None,
    ) -> None:
        self.cfg = cfg or global_settings
        self.provider = provider

    # -- public API ---------------------------------------------------------

    def extract(
        self,
        fragments: Sequence[Fragment],
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        mirror: str = "",
        mpn_hint: Optional[str] = None,
    ) -> ExtractionResult:
        result = ExtractionResult()

        if self.cfg.deterministic_first:
            # Tables first, across every fragment, then prose -- never
            # interleaved. A variant matrix also appears in the page's raw text,
            # where it reads as an undifferentiated run of numbers; letting the
            # text pass run before the structured pass lets one variant's weight
            # attach to another variant's record. Structure wins, prose fills
            # what structure left behind.
            for frag in fragments:
                if frag.kind in ("table", "keyvalue") and frag.table:
                    self._from_table(frag, schema, source_id, source_kind, result, mpn_hint)
            for frag in fragments:
                if frag.kind in ("text", "heading", "image_caption"):
                    self._from_text(frag, schema, source_id, source_kind, result)
            result.deterministic_hits = len(result.values)

        if self.cfg.llm_gap_fill and self.provider is not None and self.provider.available:
            missing = [
                c for c in schema.extractable_codes()
                if c not in result.values and not schema.attributes[c].generated
            ]
            if missing:
                before = len(result.values)
                self._llm_gap_fill(fragments, schema, source_id, source_kind, missing, result)
                result.llm_hits = len(result.values) - before

        # Verify every quote against the canonical mirror. Unverifiable
        # evidence is downgraded, never silently accepted.
        if mirror:
            self._verify_quotes(result, mirror)

        return result

    # -- tier 1: deterministic ---------------------------------------------

    def _from_table(
        self,
        frag: Fragment,
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        result: ExtractionResult,
        mpn_hint: Optional[str],
    ) -> None:
        rows = frag.table or []
        if not rows:
            return

        orientation = self._table_orientation(rows)
        if orientation == "variant_matrix":
            self._from_variant_matrix(frag, schema, source_id, source_kind, result, mpn_hint)
        else:
            self._from_label_value_rows(frag, schema, source_id, source_kind, result)

    def _table_orientation(self, rows: List[List[str]]) -> str:
        """
        Decide whether a table is label/value pairs or a variant matrix.

        A variant matrix has a header row whose first cell names a key column
        ("Part Number", "Model") and whose remaining cells are attribute names.
        Getting this wrong is what caused the predecessor to read the wrong
        column, so the test is explicit rather than incidental.
        """
        if not rows or len(rows) < 2:
            return "label_value"
        header = [str(c or "").strip().lower() for c in rows[0]]
        if len(header) < 3:
            return "label_value"
        first = header[0]
        if any(h in first for h in _KEY_COLUMN_HINTS):
            return "variant_matrix"
        # If most header cells resolve to attribute names, it is a matrix.
        return "label_value"

    def _from_label_value_rows(
        self,
        frag: Fragment,
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        result: ExtractionResult,
    ) -> None:
        for r_idx, row in enumerate(frag.table or []):
            cells = [str(c or "").strip() for c in row]
            if len(cells) < 2 or not cells[0]:
                continue
            label = cells[0]
            value = next((c for c in cells[1:] if c), "")
            if not value:
                continue

            attr = match_attribute(label, schema)
            if attr is None:
                continue

            locator = f"{frag.locator} / row {r_idx + 1}"
            quote = " | ".join(c for c in cells if c)
            self._emit(
                attr, value, schema, frag, source_id, source_kind, locator, quote,
                ExtractionMethod.STRUCTURED_FEED if frag.method == ExtractionMethod.STRUCTURED_FEED
                else ExtractionMethod.NATIVE_TABLE,
                result,
            )

    def _from_variant_matrix(
        self,
        frag: Fragment,
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        result: ExtractionResult,
        mpn_hint: Optional[str],
    ) -> None:
        """
        Read a variant table, taking values from the row matching this product.

        This is the direct fix for the wrong-column bug. Rather than guessing a
        column position, the row is selected by matching the key column against
        the product's own part number. If no row matches, nothing is extracted --
        an honest gap beats a confident wrong value.
        """
        from product_intel.pipeline.identity import normalize_mpn

        rows = frag.table or []
        header = [str(c or "").strip() for c in rows[0]]
        if not mpn_hint:
            return

        target = normalize_mpn(mpn_hint)
        matched_row: Optional[Tuple[int, List[str]]] = None
        for r_idx, row in enumerate(rows[1:], start=1):
            if not row:
                continue
            if normalize_mpn(str(row[0] or "")) == target:
                matched_row = (r_idx, [str(c or "").strip() for c in row])
                break

        if matched_row is None:
            return

        r_idx, cells = matched_row
        for c_idx, head in enumerate(header):
            if c_idx == 0 or c_idx >= len(cells):
                continue
            value = cells[c_idx]
            if not value:
                continue
            attr = match_attribute(head, schema)
            if attr is None:
                continue
            locator = f"{frag.locator} / row {r_idx + 1} / column '{head}'"
            quote = " | ".join(c for c in cells if c)
            self._emit(
                attr, value, schema, frag, source_id, source_kind, locator, quote,
                ExtractionMethod.NATIVE_TABLE, result,
            )

    def _from_text(
        self,
        frag: Fragment,
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        result: ExtractionResult,
    ) -> None:
        """Find 'Label: value' patterns in prose, one attribute at a time."""
        text = frag.text or ""
        if not text:
            return

        for attr in schema.attributes.values():
            if attr.generated or attr.identity or attr.code in result.values:
                continue
            for alias in attr.alias_patterns():
                # Single- and double-letter aliases ('w', 'h', 'd', 'in') are
                # meaningful as a table row label but catastrophic in prose,
                # where 'd' matches the 'D' of a trip curve and yields a depth
                # of 15 mm from "D 15 kA". Short aliases stay table-only.
                if len(alias) < 4:
                    continue

                # Two accepted shapes, both requiring the value to be clearly
                # delimited from the label:
                #   strong separator -- a colon, an en/em dash, a spaced hyphen
                #     not starting a number, or a run of two or more spaces;
                #   single space -- only when the value begins like a value,
                #     i.e. with a digit or a sign. This is what lets
                #     "Operating Temperature -25 to +60 C" parse (without the
                #     '-' being mistaken for the separator and dropping the
                #     sign) while rejecting prose such as
                #     "...approvals and environmental ratings...".
                strong = (
                    rf"(?<![\w]){re.escape(alias)}\s*"
                    rf"(?::|\s[–—]\s|\s-\s(?!\d)|\s{{2,}})\s*"
                    rf"([^\n;|]{{1,90}})"
                )
                loose = rf"(?<![\w]){re.escape(alias)}\s+((?=[-+−]?\d)[^\n;|]{{1,90}})"

                m = re.search(strong, text, flags=re.IGNORECASE) or re.search(
                    loose, text, flags=re.IGNORECASE
                )
                if not m:
                    continue
                raw = m.group(1).strip().rstrip(".,;")
                if not raw:
                    continue
                line_start = text.rfind("\n", 0, m.start()) + 1
                line_end = text.find("\n", m.end())
                quote = text[line_start : line_end if line_end != -1 else len(text)].strip()
                self._emit(
                    attr, raw, schema, frag, source_id, source_kind,
                    frag.locator, quote, ExtractionMethod.NATIVE_TEXT, result,
                )
                break

    def _emit(
        self,
        attr: AttributeDef,
        raw: str,
        schema: CategorySchema,
        frag: Fragment,
        source_id: str,
        source_kind: SourceKind,
        locator: str,
        quote: str,
        method: ExtractionMethod,
        result: ExtractionResult,
    ) -> None:
        """Normalize a raw observation and attach its evidence."""
        # A combined range row feeds two attributes at once.
        pair = _range_partner(attr, schema)
        if pair and attr.is_numeric:
            rng = _split_range(raw)
            if rng:
                lo_attr, hi_attr = pair
                for target, val in ((lo_attr, rng[0]), (hi_attr, rng[1])):
                    unit_hint = re.sub(r"[-+−\d.,\s]", "", str(raw))[:12]
                    av = normalize_value(f"{val} {unit_hint}".strip(), target)
                    if av.value is None:
                        continue
                    av.raw_value = str(raw)
                    av.evidence = Evidence(
                        source_id=source_id, source_kind=source_kind, locator=locator,
                        page=frag.page, quote=quote, method=method,
                    )
                    av.normalization_notes.append(f"split from range '{raw}'")
                    result.add(av)
                return

        av = normalize_value(raw, attr)
        if av.value is None:
            return
        av.evidence = Evidence(
            source_id=source_id, source_kind=source_kind, locator=locator,
            page=frag.page, quote=quote, method=method,
        )
        result.add(av)

    # -- tier 2: LLM gap-fill ----------------------------------------------

    def _llm_gap_fill(
        self,
        fragments: Sequence[Fragment],
        schema: CategorySchema,
        source_id: str,
        source_kind: SourceKind,
        missing: List[str],
        result: ExtractionResult,
    ) -> None:
        candidates = self._rank_fragments(fragments, missing, schema)
        if not candidates:
            return

        budget = self.cfg.max_llm_fragments_per_product
        context_blocks: List[str] = []
        frag_by_id: Dict[str, Fragment] = {}
        for frag in candidates[:budget]:
            frag_by_id[frag.fragment_id] = frag
            body = frag.text[:1800]
            context_blocks.append(f"[FRAGMENT {frag.fragment_id} @ {frag.locator}]\n{body}")

        spec_lines = []
        for code in missing:
            attr = schema.attributes[code]
            bits = [f"- {code} ({attr.name}): {attr.datatype}"]
            if attr.canonical_unit:
                bits.append(f"expressed in {attr.canonical_unit} (convert if the source uses another unit)")
            if attr.allowed_values:
                bits.append(f"one of [{', '.join(attr.allowed_values)}]")
            if attr.cardinality == "multi":
                bits.append("may hold multiple values")
            spec_lines.append("; ".join(bits))

        prompt = (
            "You are extracting product attributes from industrial product documentation.\n\n"
            "Find ONLY the attributes listed below. For each one you find, return the value "
            "and the exact verbatim text from the fragment that supports it.\n\n"
            "RULES:\n"
            "1. Every value MUST be supported by a quote copied character-for-character from a fragment. "
            "If you cannot quote it, do not report it.\n"
            "2. Do NOT infer, calculate or assume values. Report only what is written.\n"
            "3. If an attribute is not present, omit it entirely. Omission is the correct answer "
            "for anything absent -- do not guess.\n"
            "4. Report the value as written in the source, including its unit. Do not convert.\n\n"
            f"ATTRIBUTES TO FIND:\n" + "\n".join(spec_lines) + "\n\n"
            f"DOCUMENT FRAGMENTS:\n" + "\n\n".join(context_blocks) + "\n\n"
            'Return JSON: {"attributes": [{"code": "...", "value": "...", '
            '"quote": "...", "fragment_id": "..."}]}'
        )

        try:
            payload = self.provider.complete_json(prompt, expect="object")
            result.llm_calls += 1
        except LLMUnavailable as exc:
            log.warning("LLM gap-fill unavailable: %s", exc)
            return

        items = payload.get("attributes", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "")).strip()
            attr = schema.get(code)
            if attr is None or attr.generated or attr.identity or code in result.values:
                continue
            raw = item.get("value")
            quote = str(item.get("quote", "")).strip()
            if raw is None or not quote:
                continue

            frag = frag_by_id.get(str(item.get("fragment_id", "")))
            if frag is None:
                frag = next((f for f in frag_by_id.values() if quote[:40] in f.text), None)
            if frag is None:
                result.rejected_quotes += 1
                continue

            # The quote must actually exist in the fragment it claims to come from.
            if not _loose_contains(frag.text, quote):
                result.rejected_quotes += 1
                log.debug("rejected unverifiable LLM quote for %s: %r", code, quote[:60])
                continue

            av = normalize_value(raw, attr)
            if av.value is None:
                continue
            av.evidence = Evidence(
                source_id=source_id, source_kind=source_kind, locator=frag.locator,
                page=frag.page, quote=quote, method=ExtractionMethod.LLM,
            )
            result.add(av)

    def _rank_fragments(
        self,
        fragments: Sequence[Fragment],
        missing: List[str],
        schema: CategorySchema,
    ) -> List[Fragment]:
        """Order fragments by how likely they are to contain the missing attributes."""
        aliases: List[str] = []
        for code in missing:
            aliases.extend(_norm_label(a) for a in schema.attributes[code].alias_patterns())
        aliases = [a for a in aliases if len(a) > 2]

        scored: List[Tuple[float, Fragment]] = []
        for frag in fragments:
            body = _norm_label(frag.text)
            if not body:
                continue
            score = sum(1.0 for a in set(aliases) if a in body)
            if frag.kind in ("table", "keyvalue"):
                score *= 1.4
            if score > 0:
                scored.append((score, frag))
        scored.sort(key=lambda sf: sf[0], reverse=True)
        return [f for _, f in scored]

    # -- quote verification -------------------------------------------------

    def _verify_quotes(self, result: ExtractionResult, mirror: str) -> None:
        """
        Confirm each quote exists in the canonical mirror.

        This is what makes "traceable output" a checkable property rather than a
        claim. A value whose quote cannot be located keeps its evidence pointer
        but is marked unverified, which costs it confidence downstream.
        """
        norm_mirror = _normalize_for_match(mirror)
        for av in result.flat():
            if av.evidence is None or not av.evidence.quote:
                continue
            av.evidence.quote_verified = _loose_contains(
                mirror, av.evidence.quote, prenormalized_haystack=norm_mirror
            )


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _loose_contains(
    haystack: str,
    needle: str,
    prenormalized_haystack: Optional[str] = None,
) -> bool:
    """
    Whitespace- and punctuation-insensitive containment.

    Tolerates line-wrapping and punctuation drift introduced by PDF extraction,
    but still requires the substance of the quote to be present. Very short
    quotes are matched strictly to avoid coincidental hits.
    """
    if not needle:
        return False
    h = prenormalized_haystack if prenormalized_haystack is not None else _normalize_for_match(haystack)
    n = _normalize_for_match(needle)
    if not n:
        return False
    if len(n) < 8:
        return n in h
    if n in h:
        return True
    # Allow a truncated or slightly-extended quote: require an 85% contiguous run.
    window = int(len(n) * 0.85)
    if window < 10:
        return False
    step = max(1, window // 8)
    return any(n[i : i + window] in h for i in range(0, len(n) - window + 1, step))
