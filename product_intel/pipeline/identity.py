"""
Product identity resolution.

The predecessor's guiding principle was "documents never lose identity". This
module supplies its dual, which is what product intelligence actually needs:
**products have one identity across documents**.

A product mentioned in a datasheet, a web page and a price file must resolve to
one product_id, or the golden record never forms and every downstream count is
wrong.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Tokens that appear in part numbers as noise rather than signal.
_NOISE_PREFIXES = ("p/n", "pn", "part no", "part number", "part#", "part #", "cat no", "cat.", "model", "mpn", "sku", "item")

_MPN_RE = re.compile(r"\b([A-Z0-9]{2,}(?:[-/][A-Z0-9]+){1,5})\b")


def normalize_mpn(raw: str) -> str:
    """
    Canonicalize a part number for matching.

    'CB-100/2P-C20' , 'cb 100 2p c20' and 'P/N: CB100-2PC20' all collapse to
    'CB10025PC20'-style keys so the same physical part matches across sources.
    Separators are dropped rather than unified, because manufacturers are not
    consistent about which separator they use between systems.
    """
    if not raw:
        return ""
    text = str(raw).strip().lower()
    for prefix in _NOISE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip(" :.#")
    return re.sub(r"[^a-z0-9]", "", text).upper()


def normalize_manufacturer(raw: str) -> str:
    """Strip corporate suffixes so 'Acme Inc.' and 'ACME' are one manufacturer."""
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", str(raw)).strip()
    text = re.sub(
        r"[,\s]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|gmbh|s\.a\.|sa|plc|co|co\.|company|group|holdings)\.?$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip(" .,-")


def product_id_for(manufacturer: str, mpn: str) -> str:
    """Deterministic ID. Same manufacturer + part number always yields the same ID."""
    key = f"{normalize_manufacturer(manufacturer).upper()}|{normalize_mpn(mpn)}"
    return f"prod_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def looks_like_mpn(token: str) -> bool:
    """
    Heuristic test for whether a token is a part number.

    Requires a digit and a letter, or an explicit separator pattern -- which
    excludes plain words and bare numbers without excluding real part numbers
    like '2CDS251001R0204' or 'NSX100F'.
    """
    if not token:
        return False
    t = token.strip()
    if len(t) < 3 or len(t) > 40:
        return False
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9\-/._]*$", t):
        return False
    has_digit = any(c.isdigit() for c in t)
    has_alpha = any(c.isalpha() for c in t)
    if has_digit and has_alpha:
        return True
    # Digits plus a separator, e.g. '100-2P'
    return has_digit and bool(re.search(r"[-/._]", t))


def extract_mpn_candidates(text: str, limit: int = 20) -> List[str]:
    """Pull plausible part numbers out of free text, best candidates first."""
    if not text:
        return []
    found: List[str] = []

    # Labelled forms are the most reliable: "Part Number: ABC-123".
    for m in re.finditer(
        r"(?:part\s*(?:no\.?|number|#)|catalog(?:ue)?\s*(?:no\.?|number)|model|mpn|order\s*(?:no\.?|code)|sku|item\s*(?:no\.?|number))"
        r"\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/._]{2,39})",
        text,
        flags=re.IGNORECASE,
    ):
        cand = m.group(1).strip(" .,;:")
        if looks_like_mpn(cand) and cand not in found:
            found.append(cand)

    # Unlabelled candidates must contain a letter. Without this, tokens like
    # '240/415' (a voltage) and '5-1-1' (a schedule) read as part numbers --
    # pure digit-and-separator tokens are far more often measurements.
    for m in _MPN_RE.finditer(text.upper()):
        cand = m.group(1)
        if not any(c.isalpha() for c in cand):
            continue
        if looks_like_mpn(cand) and cand not in found:
            found.append(cand)
        if len(found) >= limit:
            break

    return found[:limit]


def variant_base_id(manufacturer: str, mpn: str, series: Optional[str]) -> Optional[str]:
    """
    Derive the base (family) product a variant belongs to.

    Prefers an explicit series when the document declares one, because that is
    authoritative. Otherwise it strips a trailing variant suffix -- 'CB-100-2P-C20'
    -> 'CB-100' -- which is how most industrial part numbering actually works.
    """
    if series:
        return product_id_for(manufacturer, series)

    parts = re.split(r"[-/]", str(mpn or "").strip())
    if len(parts) < 3:
        return None
    base = "-".join(parts[:-2]) if len(parts) > 3 else parts[0]
    if not base or normalize_mpn(base) == normalize_mpn(mpn):
        return None
    return product_id_for(manufacturer, base)


class IdentityResolver:
    """
    Maintains the mapping from every alias seen to a canonical product_id.

    Alias forms accumulate as more sources are ingested, so a price file that
    writes 'CB100-2P-C20' still resolves to the product first seen in a
    datasheet as 'CB-100/2P-C20'.
    """

    def __init__(self) -> None:
        self._by_key: Dict[str, str] = {}
        self._aliases: Dict[str, List[str]] = {}
        self._gtin: Dict[str, str] = {}

    def register(
        self,
        manufacturer: str,
        mpn: str,
        gtin: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """Register a sighting. Returns (product_id, is_new)."""
        pid = product_id_for(manufacturer, mpn)
        key = f"{normalize_manufacturer(manufacturer).upper()}|{normalize_mpn(mpn)}"
        is_new = key not in self._by_key
        self._by_key[key] = pid
        aliases = self._aliases.setdefault(pid, [])
        if mpn and mpn not in aliases:
            aliases.append(mpn)
        if gtin:
            self._gtin[re.sub(r"\D", "", gtin)] = pid
        return pid, is_new

    def resolve(
        self,
        manufacturer: str,
        mpn: str,
        gtin: Optional[str] = None,
    ) -> Optional[str]:
        """Find an existing product_id. GTIN wins when present -- it is a global key."""
        if gtin:
            hit = self._gtin.get(re.sub(r"\D", "", gtin))
            if hit:
                return hit
        key = f"{normalize_manufacturer(manufacturer).upper()}|{normalize_mpn(mpn)}"
        return self._by_key.get(key)

    def find_near_duplicate(
        self,
        manufacturer: str,
        mpn: str,
        max_distance: int = 2,
    ) -> Optional[Tuple[str, str, int]]:
        """
        Detect a probable duplicate of an existing part number.

        Scanned catalogs are everywhere in this industry and OCR reliably
        mangles part numbers -- 'FV-3000' reads as 'FV-2000', '0' as 'O', '5'
        as 'S'. But 'VX100-1P-C06' and 'VX100-1P-C16' are also one character
        apart and are genuinely different products.

        There is no reliable way to tell those two cases apart from the strings
        alone, so this method does NOT merge. It reports a suspicion, the
        product is created normally, and a human decides. Silently merging two
        real SKUs destroys catalog data in a way that is very hard to detect
        later; carrying one extra record until someone confirms it is cheap.

        Returns (product_id, matched_mpn, distance) or None.
        """
        target = normalize_mpn(mpn)
        if len(target) < 4:
            return None  # too short for edit distance to carry meaning

        mfr_key = normalize_manufacturer(manufacturer).upper()
        best: Optional[Tuple[str, str, int]] = None

        for key, pid in self._by_key.items():
            known_mfr, _, known_mpn = key.partition("|")
            if known_mfr != mfr_key or known_mpn == target:
                continue
            if abs(len(known_mpn) - len(target)) > max_distance:
                continue
            distance = _edit_distance(target, known_mpn, max_distance)
            if distance is None or distance > max_distance:
                continue
            if distance / max(len(target), len(known_mpn)) > 0.2:
                continue
            if best is None or distance < best[2]:
                aliases = self._aliases.get(pid, [])
                best = (pid, aliases[0] if aliases else known_mpn, distance)

        return best

    def aliases_for(self, product_id: str) -> List[str]:
        return self._aliases.get(product_id, [])

    def add_alias(self, product_id: str, manufacturer: str, mpn: str) -> None:
        """Bind an additional surface form to an existing product."""
        key = f"{normalize_manufacturer(manufacturer).upper()}|{normalize_mpn(mpn)}"
        self._by_key[key] = product_id
        aliases = self._aliases.setdefault(product_id, [])
        if mpn and mpn not in aliases:
            aliases.append(mpn)

    def known_count(self) -> int:
        return len(set(self._by_key.values()))


def _edit_distance(a: str, b: str, cutoff: int) -> Optional[int]:
    """Levenshtein distance with early exit once `cutoff` is exceeded."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if len(a) - len(b) > cutoff:
        return None

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        current = [i + 1]
        for j, cb in enumerate(b):
            current.append(min(previous[j + 1] + 1, current[j] + 1, previous[j] + (ca != cb)))
        if min(current) > cutoff:
            return None
        previous = current
    return previous[-1]
