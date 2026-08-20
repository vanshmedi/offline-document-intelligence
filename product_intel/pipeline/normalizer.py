"""
Value normalization.

Turns the surface forms found in real industrial documents into one canonical
vocabulary:

    '1/2"'          -> 12.7 mm
    '150 PSI WOG'   -> 10.34 bar
    '1200 CFM'      -> 2038.8 m3/h
    '-4 F'          -> -20 degC
    '3/4 HP'        -> 559.3 W
    'SS316'         -> 'Stainless Steel 316'
    'ptfe'          -> 'PTFE'

Nothing is dropped in the process: the raw surface form and raw unit are kept
on the AttributeValue, so the original text is always recoverable and the
conversion is auditable.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, List, Optional, Tuple

from product_intel.models import AttributeValue
from product_intel.schema.dictionary import AttributeDef, load_units

# --- fraction and number parsing -------------------------------------------

_FRACTION_MAP = {
    "¼": 0.25, "½": 0.5, "¾": 0.75, "⅛": 0.125, "⅜": 0.375,
    "⅝": 0.625, "⅞": 0.875, "⅓": 1 / 3, "⅔": 2 / 3,
}

_NUM_RE = re.compile(
    r"""(?P<sign>[-+−]?)\s*
        (?:
          (?P<whole>\d[\d,]*)\s+(?P<fnum>\d+)\s*/\s*(?P<fden>\d+)   # 1 1/2
          |(?P<fnum2>\d+)\s*/\s*(?P<fden2>\d+)                       # 3/4
          |(?P<dec>\d[\d,]*(?:\.\d+)?)                               # 12.7 / 1,200
        )""",
    re.VERBOSE,
)

#: Enum synonym table. Maps the messy forms found in datasheets and web copy
#: onto the controlled vocabulary declared in the attribute dictionary.
ENUM_SYNONYMS = {
    # materials
    "ss316": "Stainless Steel 316", "316ss": "Stainless Steel 316",
    "316 ss": "Stainless Steel 316", "316": "Stainless Steel 316",
    "316l": "Stainless Steel 316", "sst 316": "Stainless Steel 316",
    "stainless steel (316)": "Stainless Steel 316", "stainless 316": "Stainless Steel 316",
    "ss304": "Stainless Steel 304", "304ss": "Stainless Steel 304",
    "304 ss": "Stainless Steel 304", "304": "Stainless Steel 304",
    "stainless 304": "Stainless Steel 304",
    "stainless steel": "Stainless Steel 316", "stainless": "Stainless Steel 316",
    "cs": "Carbon Steel", "carbon stl": "Carbon Steel", "a105": "Carbon Steel",
    "forged brass": "Brass", "lead-free brass": "Brass", "lead free brass": "Brass",
    "cw617n": "Brass", "c46500": "Brass", "naval brass": "Brass",
    "di": "Ductile Iron", "ductile": "Ductile Iron",
    "mi": "Malleable Iron", "malleable": "Malleable Iron",
    "u-pvc": "PVC", "upvc": "PVC", "pvc-u": "PVC",
    # seats / seals
    "r-ptfe": "RPTFE", "rptfe": "RPTFE", "reinforced ptfe": "RPTFE",
    "glass filled ptfe": "RPTFE", "teflon": "PTFE", "p.t.f.e.": "PTFE",
    "tfm 1600": "TFM", "tfm1600": "TFM",
    # end connections
    "npt": "NPT Threaded", "fnpt": "NPT Threaded", "mnpt": "NPT Threaded",
    "npt threaded": "NPT Threaded", "threaded npt": "NPT Threaded",
    "female npt": "NPT Threaded", "male npt": "NPT Threaded",
    "bsp": "BSP Threaded", "bspt": "BSP Threaded", "bspp": "BSP Threaded",
    "sw": "Socket Weld", "socket-weld": "Socket Weld", "socketweld": "Socket Weld",
    "bw": "Butt Weld", "butt-weld": "Butt Weld",
    "flange": "Flanged", "flanged ends": "Flanged", "ansi flanged": "Flanged",
    "sweat": "Solder", "solder joint": "Solder", "c x c": "Solder",
    "press fit": "Press", "press-fit": "Press",
    # ports
    "full bore": "Full Port", "fb": "Full Port", "full-port": "Full Port",
    "standard bore": "Standard Port", "std port": "Standard Port",
    "reduced bore": "Reduced Port", "rb": "Reduced Port", "reduced-port": "Reduced Port",
    # mounting
    "din": "DIN Rail", "din-rail": "DIN Rail", "din rail mount": "DIN Rail",
    "35mm din rail": "DIN Rail", "panel": "Panel Mount", "surface mount": "Panel Mount",
    "plug in": "Plug-In", "plugin": "Plug-In", "bolt on": "Bolt-On",
    # handles / actuation
    "lever handle": "Lever", "lever operated": "Lever", "locking handle": "Locking Lever",
    "tee": "Tee Handle", "t-handle": "Tee Handle", "gear": "Gear Operated",
    "hand operated": "Manual", "manual operation": "Manual",
    "pneumatically actuated": "Pneumatic", "electrically actuated": "Electric",
    # drives / bearings
    "direct": "Direct Drive", "direct-drive": "Direct Drive", "dd": "Direct Drive",
    "belt": "Belt Drive", "belt-drive": "Belt Drive",
    "ball": "Ball Bearing", "ball brg": "Ball Bearing",
    "sleeve": "Sleeve Bearing", "sleeve brg": "Sleeve Bearing",
    # displays
    "backlit lcd display": "Backlit LCD", "lcd display": "LCD",
    "touch screen": "Touchscreen", "touch-screen": "Touchscreen",
    # control types
    "7 day programmable": "7-Day Programmable", "7day": "7-Day Programmable",
    "5-1-1": "5-1-1 Programmable", "5/1/1": "5-1-1 Programmable",
    "non programmable": "Non-Programmable", "manual thermostat": "Non-Programmable",
    "wifi": "Smart / Wi-Fi Connected", "wi-fi": "Smart / Wi-Fi Connected",
    "smart": "Smart / Wi-Fi Connected", "connected": "Smart / Wi-Fi Connected",
    # fittings
    "90 deg elbow": "90 Elbow", "90° elbow": "90 Elbow", "elbow 90": "90 Elbow",
    "45 deg elbow": "45 Elbow", "45° elbow": "45 Elbow", "elbow 45": "45 Elbow",
    "street elbow": "90 Elbow", "hex nipple": "Nipple", "hex bushing": "Bushing",
    # schedules
    "sch40": "SCH 40", "sch 40": "SCH 40", "schedule 40": "SCH 40",
    "sch80": "SCH 80", "sch 80": "SCH 80", "schedule 80": "SCH 80",
    "sch160": "SCH 160", "schedule 160": "SCH 160",
    "extra strong": "XS", "double extra strong": "XXS",
    # finishes
    "galv": "Galvanized", "hot dip galvanized": "Galvanized", "hdg": "Galvanized",
    "black iron": "Black", "self colour": "Black", "self color": "Black",
    "cp": "Chrome Plated", "np": "Nickel Plated",
    # trip curves
    "curve b": "B", "curve c": "C", "curve d": "D", "type b": "B",
    "type c": "C", "type d": "D",
    # uom
    "each": "EA", "ea.": "EA", "box": "BX", "case": "CS", "pack": "PK",
    "foot": "FT", "feet": "FT", "roll": "RL",
    # phases
    "single phase": "1", "1-phase": "1", "1 ph": "1", "single-phase": "1",
    "three phase": "3", "3-phase": "3", "3 ph": "3", "three-phase": "3",
    # terminals
    "screw terminal": "Screw", "box lugs": "Box Lug", "spring cage": "Spring Clamp",
    # body construction
    "one piece": "1-Piece", "1 piece": "1-Piece", "two piece": "2-Piece",
    "2 piece": "2-Piece", "three piece": "3-Piece", "3 piece": "3-Piece",
}

#: Certification surface forms -> canonical labels.
CERT_CANONICAL = {
    "ul489": "UL 489", "ul 489": "UL 489", "ul1077": "UL 1077", "ul 1077": "UL 1077",
    "ul705": "UL 705", "ul 705": "UL 705", "ul60730": "UL 60730", "ul 60730": "UL 60730",
    "ul": "UL Listed", "ul listed": "UL Listed", "c-ul": "cULus", "cul": "cULus",
    "culus": "cULus", "csa": "CSA", "ce": "CE", "ce marked": "CE",
    "rohs": "RoHS", "rohs compliant": "RoHS", "reach": "REACH",
    "iec60947": "IEC 60947-2", "iec 60947-2": "IEC 60947-2", "iec 60947": "IEC 60947-2",
    "nsf61": "NSF/ANSI 61", "nsf/61": "NSF/ANSI 61", "nsf 61": "NSF/ANSI 61",
    "nsf/ansi 61": "NSF/ANSI 61", "nsf372": "NSF/ANSI 372", "nsf/ansi 372": "NSF/ANSI 372",
    "nsf": "NSF/ANSI 61", "wras": "WRAS", "api608": "API 608", "api 608": "API 608",
    "mss sp-110": "MSS SP-110", "mss-sp-110": "MSS SP-110",
    "amca": "AMCA Certified", "etl": "ETL Listed", "fcc": "FCC Part 15",
    "energy star": "ENERGY STAR", "energystar": "ENERGY STAR",
    "iso9001": "ISO 9001", "iso 9001": "ISO 9001",
    "lead free": "Lead-Free Compliant", "lead-free": "Lead-Free Compliant",
}


def _to_ascii(text: str) -> str:
    """Normalize unicode fractions, dashes and degree signs to ASCII-ish forms."""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _FRACTION_MAP:
            out.append(f" {_FRACTION_MAP[ch]:g}")
        elif ch in "‐‑‒–—−":
            out.append("-")
        elif ch in "“”":
            out.append('"')
        elif ch in "‘’":
            out.append("'")
        else:
            out.append(ch)
    text = "".join(out)
    return unicodedata.normalize("NFKC", text)


def parse_number(text: str) -> Optional[float]:
    """Parse the first number in a string, handling fractions like '1 1/2' and '3/4'."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    s = _to_ascii(str(text)).strip()
    if not s:
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    sign = -1.0 if m.group("sign") in ("-", "−") else 1.0
    try:
        if m.group("whole") is not None:
            whole = float(m.group("whole").replace(",", ""))
            num, den = float(m.group("fnum")), float(m.group("fden"))
            if not _is_real_fraction(num, den):
                return sign * whole
            val = whole + num / den
        elif m.group("fnum2") is not None:
            num, den = float(m.group("fnum2")), float(m.group("fden2"))
            if not _is_real_fraction(num, den):
                # Not a fraction -- a dual rating such as '240/415 V AC' or a
                # date. Take the leading number; dividing it would produce a
                # confident, absurd value (240/415 -> 0.578 V).
                return sign * num
            val = num / den
    except (ValueError, ZeroDivisionError):
        return None
    else:
        if m.group("whole") is None and m.group("fnum2") is None:
            try:
                val = float(m.group("dec").replace(",", ""))
            except ValueError:
                return None
    return sign * val


#: Denominators that appear in real industrial fractional sizes (inches).
_FRACTION_DENOMINATORS = {2, 3, 4, 5, 6, 8, 10, 12, 16, 32, 64}


def _is_real_fraction(numerator: float, denominator: float) -> bool:
    """
    Distinguish '3/4' (a size) from '240/415' (a dual voltage rating).

    A genuine fractional dimension has a proper numerator over a binary-ish
    denominator. Anything else that happens to contain a slash is two separate
    numbers, and treating it as division silently fabricates a value.
    """
    if denominator == 0 or numerator <= 0:
        return False
    if not float(denominator).is_integer() or not float(numerator).is_integer():
        return False
    return int(denominator) in _FRACTION_DENOMINATORS and numerator < denominator


def _unit_lookup(raw_unit: str, family: Optional[str]) -> Optional[Tuple[str, str, dict]]:
    """Resolve a surface unit string to (family, unit_code, unit_spec)."""
    if not raw_unit:
        return None
    token = _to_ascii(raw_unit).strip().lower().strip(".,;:()[]")
    if not token:
        return None
    units = load_units()["families"]
    families = [family] if family and family in units else list(units.keys())
    for fam in families:
        for code, spec in units[fam]["units"].items():
            if token == code.lower() or token in [a.lower() for a in spec["aliases"]]:
                return fam, code, spec
    return None


def detect_unit(text: str, family: Optional[str]) -> Optional[Tuple[str, str, dict]]:
    """Find a unit token anywhere in a string, preferring the longest alias match."""
    if not text:
        return None
    lowered = _to_ascii(str(text)).lower()
    units = load_units()["families"]
    families = [family] if family and family in units else list(units.keys())

    best: Optional[Tuple[str, str, dict]] = None
    best_len = 0
    for fam in families:
        for code, spec in units[fam]["units"].items():
            for alias in [code] + spec["aliases"]:
                a = alias.lower()
                # Alphanumeric aliases need word boundaries; symbols like " or ° do not.
                # A digit may precede a unit ('22kA', '63A', '35mm'), so only a
                # preceding *letter* blocks the match.
                if a and a[0].isalnum():
                    hit = re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z0-9])", lowered)
                else:
                    hit = re.search(re.escape(a), lowered)
                if hit and len(a) > best_len:
                    best, best_len = (fam, code, spec), len(a)
    return best


def convert_to_canonical(value: float, family: str, unit_code: str) -> float:
    """Convert a value in `unit_code` to the family's canonical unit."""
    units = load_units()["families"][family]
    spec = units["units"][unit_code]
    style = spec.get("offset_style")
    if style == "fahrenheit":
        return (value - 32.0) * 5.0 / 9.0
    if style == "kelvin":
        return value - 273.15
    return value * float(spec["factor"])


def convert_units(value: float, family: str, from_unit: str, to_unit: str) -> float:
    """
    Convert between any two units of the same family.

    Needed because an attribute's canonical unit is not always its family's
    canonical unit -- interrupting rating is declared in kA while the current
    family normalizes to A, so a naive one-step conversion inflates it 1000x.
    """
    if from_unit == to_unit:
        return value
    base = convert_to_canonical(value, family, from_unit)
    units = load_units()["families"][family]["units"]
    target = units.get(to_unit)
    if target is None:
        return base
    style = target.get("offset_style")
    if style == "fahrenheit":
        return base * 9.0 / 5.0 + 32.0
    if style == "kelvin":
        return base + 273.15
    return base / float(target["factor"])


#: Synonyms promoted from human corrections, keyed by attribute code. Populated
#: at startup from Catalog/learned_rules.json. This is the mechanism that makes
#: one reviewer correction fix the next five hundred products rather than one.
_LEARNED_SYNONYMS: Dict[str, Dict[str, str]] = {}


def register_learned_synonyms(by_code: Dict[str, Dict[str, str]]) -> None:
    """Install reviewer-promoted enum mappings. Called once when the engine loads."""
    _LEARNED_SYNONYMS.clear()
    for code, mapping in (by_code or {}).items():
        _LEARNED_SYNONYMS[code] = {k.strip().lower(): v for k, v in mapping.items()}


def learned_synonym_count() -> int:
    return sum(len(v) for v in _LEARNED_SYNONYMS.values())


def normalize_enum(
    raw: str,
    allowed: List[str],
    code: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Map a surface form onto the controlled vocabulary. Returns (value, note)."""
    if raw is None:
        return None, None
    token = _to_ascii(str(raw)).strip()
    if not token:
        return None, None
    lowered = token.lower().strip(".,;:")

    for a in allowed:  # exact, case-insensitive
        if lowered == a.lower():
            return a, None

    # A reviewer's decision outranks the built-in synonym table.
    if code:
        learned = _LEARNED_SYNONYMS.get(code, {}).get(lowered)
        if learned and learned in allowed:
            return learned, f"applied a learned mapping: '{token}' -> '{learned}'"

    syn = ENUM_SYNONYMS.get(lowered)
    if syn and syn in allowed:
        return syn, f"mapped '{token}' -> '{syn}'"

    for a in allowed:  # substring containment, longest candidate wins
        if a.lower() in lowered or lowered in a.lower():
            return a, f"matched '{token}' -> '{a}'"

    hits = [(len(k), v) for k, v in ENUM_SYNONYMS.items() if k in lowered and v in allowed]
    if hits:
        best = max(hits)[1]
        return best, f"inferred '{token}' -> '{best}'"

    return None, f"'{token}' is not a legal value (allowed: {', '.join(allowed[:6])}...)"


def normalize_certifications(raw: Any) -> List[str]:
    """Split a certifications blob into canonical, deduplicated labels."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[;,/•\n]|\s{2,}", str(raw))
    out: List[str] = []
    for item in items:
        token = _to_ascii(str(item)).strip().strip(".;:")
        if not token or len(token) > 60:
            continue
        canon = CERT_CANONICAL.get(token.lower())
        if not canon:
            hits = [(len(k), v) for k, v in CERT_CANONICAL.items()
                    if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", token.lower())]
            canon = max(hits)[1] if hits else token
        if canon not in out:
            out.append(canon)
    return out


def normalize_value(raw: Any, attr: AttributeDef) -> AttributeValue:
    """
    Normalize one raw observation against its attribute definition.

    Always returns an AttributeValue: failures are recorded in
    `validation_errors` rather than raised, so one bad cell never aborts a
    document.
    """
    av = AttributeValue(code=attr.code, value=None, raw_value=None if raw is None else str(raw))

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        av.validation_errors.append("empty value")
        return av

    # -- multi-valued -------------------------------------------------------
    if attr.cardinality == "multi":
        if attr.code == "certifications":
            av.value = normalize_certifications(raw)
        else:
            items = raw if isinstance(raw, list) else re.split(r"[;,•\n]", str(raw))
            cleaned = [_to_ascii(str(i)).strip() for i in items]
            av.value = [c for c in cleaned if c]
        if not av.value:
            av.validation_errors.append("no values parsed")
        return av

    # -- numeric ------------------------------------------------------------
    if attr.datatype == "number":
        num = parse_number(raw)
        if num is None:
            av.validation_errors.append(f"could not parse a number from '{raw}'")
            return av

        detected = detect_unit(str(raw), attr.unit_family)
        if detected and attr.unit_family:
            fam, code, _spec = detected
            av.raw_unit = code
            if fam == attr.unit_family:
                target = attr.canonical_unit or load_units()["families"][fam]["canonical"]
                converted = convert_units(num, fam, code, target)
                if code != target:
                    av.normalization_notes.append(f"{num:g} {code} -> {converted:g} {target}")
                num = converted
            else:
                av.normalization_notes.append(
                    f"unit '{code}' belongs to '{fam}', expected '{attr.unit_family}'; value taken as-is"
                )
        elif attr.unit_family and attr.canonical_unit:
            # No unit written. Assume the canonical unit but say so, because a
            # silent assumption here is exactly how bad catalog data is born.
            av.normalization_notes.append(f"no unit found; assumed {attr.canonical_unit}")
            av.confidence_factors["unit_assumed"] = 0.85

        if math.isnan(num) or math.isinf(num):
            av.validation_errors.append("non-finite number")
            return av

        av.value = round(num, 6)
        av.unit = attr.canonical_unit
        if attr.min is not None and av.value < attr.min:
            av.validation_errors.append(f"{av.value:g} is below the plausible minimum {attr.min:g}")
        if attr.max is not None and av.value > attr.max:
            av.validation_errors.append(f"{av.value:g} exceeds the plausible maximum {attr.max:g}")
        return av

    # -- enum ---------------------------------------------------------------
    if attr.datatype == "enum" and attr.allowed_values:
        val, note = normalize_enum(str(raw), attr.allowed_values, code=attr.code)
        if val is None:
            av.validation_errors.append(note or "value not in allowed set")
            av.value = None
        else:
            av.value = val
            if note:
                av.normalization_notes.append(note)
        return av

    # -- boolean ------------------------------------------------------------
    if attr.datatype == "boolean":
        token = str(raw).strip().lower()
        if token in ("yes", "true", "y", "1", "included", "standard"):
            av.value = True
        elif token in ("no", "false", "n", "0", "not included", "optional"):
            av.value = False
        else:
            av.validation_errors.append(f"'{raw}' is not a boolean")
        return av

    # -- string / text ------------------------------------------------------
    text = _to_ascii(str(raw)).strip()
    text = re.sub(r"\s+", " ", text)
    if attr.max_length and len(text) > attr.max_length:
        text = text[: attr.max_length].rstrip() + "..."
        av.normalization_notes.append(f"truncated to {attr.max_length} characters")
    if attr.pattern and not re.match(attr.pattern, text):
        av.validation_errors.append(f"'{text}' does not match the required pattern {attr.pattern}")
    av.value = text
    return av
