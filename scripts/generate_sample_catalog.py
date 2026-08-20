#!/usr/bin/env python3
"""
Generate a synthetic industrial catalog for development and demos.

The data is deliberately realistic rather than clean. It contains the failure
modes that make product intelligence hard, so the pipeline is exercised against
the problems it exists to solve:

  * multi-column variant tables (the wrong-column trap)
  * mixed unit systems -- inches and mm, PSI and bar, CFM and m3/h, HP and W
  * inconsistent attribute labels across manufacturers
  * a scanned page with no text layer
  * cross-source conflicts (a datasheet and a web page that disagree)
  * gaps a variant can only fill from its family datasheet
  * enum surface forms that need mapping ('SS316', 'FNPT', 'full bore')

Usage:  python scripts/generate_sample_catalog.py [--out Sources]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

random.seed(20260820)

STYLES = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=STYLES["Heading1"], fontSize=17, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=STYLES["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("Body", parent=STYLES["BodyText"], fontSize=9, leading=12)
SMALL = ParagraphStyle("Small", parent=STYLES["BodyText"], fontSize=7.5, leading=9,
                       textColor=colors.HexColor("#555555"))

TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#98a2b3")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f9")]),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
])


# ===========================================================================
# Catalog definition
# ===========================================================================

CATALOG: Dict[str, Any] = {
    "electrical": {
        "manufacturer": "Voltaris Electric",
        "series": "VX-Series",
        "base_mpn": "VX100",
        "product": "Miniature Circuit Breaker",
        "blurb": (
            "The VX-Series miniature circuit breaker provides overload and short-circuit "
            "protection for commercial and light industrial distribution boards. DIN rail "
            "mounting, field-replaceable terminals, and a positive contact indicator are "
            "standard across the range."
        ),
        "shared": [
            ("Manufacturer", "Voltaris Electric"),
            ("Product Series", "VX-Series"),
            ("Rated Operational Voltage (Ue)", "240/415 V AC"),
            ("Mounting", "35mm DIN rail"),
            ("Terminal Type", "Screw terminal"),
            ("Enclosure Rating", "IP20"),
            ("Operating Temperature", "-25 to +60 C"),
            ("Certifications", "IEC 60947-2, CE, RoHS"),
            ("Country of Origin", "Germany"),
            ("Warranty", "24 months"),
            ("Unit of Measure", "Each"),
        ],
        "variant_header": ["Catalog Number", "Poles", "Rated Current", "Trip Curve",
                           "Interrupting Rating", "Width", "Weight"],
        "variants": [
            ["VX100-1P-C06", "1", "6 A",  "C", "10 kA", "17.5 mm", "0.11 kg"],
            ["VX100-1P-C16", "1", "16 A", "C", "10 kA", "17.5 mm", "0.11 kg"],
            ["VX100-2P-C20", "2", "20 A", "C", "10 kA", "35 mm",   "0.22 kg"],
            ["VX100-3P-C32", "3", "32 A", "C", "10 kA", "52.5 mm", "0.33 kg"],
            ["VX100-3P-D63", "3", "63 A", "D", "15 kA", "52.5 mm", "0.35 kg"],
        ],
        "prices": {"VX100-1P-C06": 18.40, "VX100-1P-C16": 18.90, "VX100-2P-C20": 34.75,
                   "VX100-3P-C32": 52.10, "VX100-3P-D63": 71.55},
        "gtins": {"VX100-1P-C06": "40123456789012", "VX100-1P-C16": "40123456789013",
                  "VX100-2P-C20": "40123456789029", "VX100-3P-C32": "40123456789036"},
        "category": "electrical.circuit_breaker",
        # Web page describes one SKU and CONTRADICTS the datasheet on interrupting rating.
        "web_sku": "VX100-2P-C20",
        "web_conflict": ("Interrupting Rating", "6 kA"),
    },
    "plumbing": {
        "manufacturer": "Ferrum Valve Works",
        "series": "FV-3000",
        "base_mpn": "FV3000",
        "product": "3-Piece Ball Valve",
        "blurb": (
            "FV-3000 three-piece ball valves are designed for process isolation service "
            "where in-line maintenance is required. The swing-out centre section allows "
            "seat and seal replacement without removing the end caps from the pipeline."
        ),
        "shared": [
            ("Manufacturer", "Ferrum Valve Works"),
            ("Series", "FV-3000"),
            ("Body Material", "SS316"),
            ("Seat Material", "RPTFE"),
            ("Port", "Full bore"),
            ("Body Construction", "3 piece"),
            ("End Connection", "FNPT"),
            ("Handle Type", "Locking lever"),
            ("Actuation", "Manual"),
            ("Working Pressure", "1000 PSI WOG"),
            ("Temperature Range", "-20 to 450 F"),
            ("Certifications", "API 608, MSS SP-110, NSF/ANSI 61"),
            ("Country of Origin", "Italy"),
            ("Warranty", "36 months"),
        ],
        "variant_header": ["Part Number", "Nominal Size", "Overall Length",
                           "Flow Coefficient (Cv)", "Weight"],
        "variants": [
            ['FV3000-050', '1/2"',  "3.35 in", "18",  "1.4 lbs"],
            ['FV3000-075', '3/4"',  "3.94 in", "38",  "2.1 lbs"],
            ['FV3000-100', '1"',    "4.53 in", "65",  "3.2 lbs"],
            ['FV3000-150', '1 1/2"', "5.71 in", "160", "6.4 lbs"],
            ['FV3000-200', '2"',    "6.50 in", "290", "9.8 lbs"],
        ],
        "prices": {"FV3000-050": 62.00, "FV3000-075": 78.50, "FV3000-100": 96.25,
                   "FV3000-150": 168.00, "FV3000-200": 241.75},
        "gtins": {"FV3000-050": "50987654321074", "FV3000-075": "50987654321081",
                  "FV3000-100": "50987654321098", "FV3000-150": "50987654321104"},
        "category": "plumbing.ball_valve",
        "web_sku": "FV3000-100",
        "web_conflict": ("Working Pressure", "800 PSI WOG"),
    },
    "hvac": {
        "manufacturer": "Aeroflow Systems",
        "series": "AF-CB",
        "base_mpn": "AFCB",
        "product": "Centrifugal Blower",
        "blurb": (
            "AF-CB forward-curved centrifugal blowers are used in air handling units, "
            "make-up air systems and light commercial ventilation. Galvanized steel "
            "housing with a permanently lubricated ball bearing motor."
        ),
        "shared": [
            ("Manufacturer", "Aeroflow Systems"),
            ("Series", "AF-CB"),
            ("Drive Type", "Direct drive"),
            ("Bearing Type", "Ball bearing"),
            ("Wheel Material", "Galvanized Steel"),
            ("Supply Voltage", "115 VAC"),
            ("Phase", "Single phase"),
            ("Frequency", "60 Hz"),
            ("Max Operating Temperature", "180 F"),
            ("Certifications", "UL 705, AMCA, ETL"),
            ("Country of Origin", "United States"),
            ("Warranty", "18 months"),
        ],
        "variant_header": ["Model", "Airflow", "Static Pressure", "Motor Power",
                           "Speed", "Wheel Diameter", "Sound Level", "Weight"],
        "variants": [
            ["AFCB-0630", "600 CFM",  "0.5 in. w.g.", "1/6 HP", "1050 RPM", "6.3 in",  "54 dBA", "18 lbs"],
            ["AFCB-0900", "900 CFM",  "0.75 in. w.g.", "1/4 HP", "1200 RPM", "9 in",   "58 dBA", "26 lbs"],
            ["AFCB-1200", "1200 CFM", "1.0 in. w.g.", "1/2 HP", "1450 RPM", "10.6 in", "63 dBA", "34 lbs"],
            ["AFCB-1800", "1800 CFM", "1.25 in. w.g.", "3/4 HP", "1600 RPM", "12.6 in", "68 dBA", "47 lbs"],
        ],
        "prices": {"AFCB-0630": 214.00, "AFCB-0900": 268.50, "AFCB-1200": 342.00, "AFCB-1800": 455.90},
        "gtins": {"AFCB-0630": "60111222333420", "AFCB-0900": "60111222333437",
                  "AFCB-1200": "60111222333444"},
        "category": "hvac.centrifugal_blower",
        "web_sku": "AFCB-1200",
        "web_conflict": ("Sound Level", "59 dBA"),
    },
}

#: A thermostat family whose variant sheet is deliberately thin, so gap filling
#: from the family datasheet is exercised.
THERMOSTAT = {
    "manufacturer": "Aeroflow Systems",
    "series": "AF-TS",
    "base_mpn": "AFTS",
    "product": "Commercial Thermostat",
    "category": "hvac.thermostat",
    "shared": [
        ("Manufacturer", "Aeroflow Systems"),
        ("Series", "AF-TS"),
        ("Supply Voltage", "24 VAC"),
        ("Display Type", "Backlit LCD"),
        ("Setpoint Range", "40 to 90 F"),
        ("Temperature Accuracy", "0.5 C"),
        ("Compatible Systems", "Conventional, Heat Pump, Dual Fuel"),
        ("Width", "4.5 in"),
        ("Height", "3.2 in"),
        ("Depth", "1.1 in"),
        ("Certifications", "UL 60730, FCC Part 15, ENERGY STAR"),
        ("Country of Origin", "Mexico"),
        ("Warranty", "60 months"),
        ("Weight", "0.6 lbs"),
    ],
    "variant_header": ["Model", "Control Type", "Heating Stages", "Cooling Stages", "Connectivity"],
    "variants": [
        ["AFTS-100", "Non-programmable", "1", "1", "None"],
        ["AFTS-200", "7 day programmable", "2", "2", "None"],
        ["AFTS-300", "Smart / Wi-Fi connected", "3", "2", "Wi-Fi, BACnet"],
    ],
    "prices": {"AFTS-100": 48.00, "AFTS-200": 92.50, "AFTS-300": 187.00},
    "gtins": {"AFTS-200": "60111222555111", "AFTS-300": "60111222555128"},
}


# ===========================================================================
# PDF builders
# ===========================================================================


def _kv_table(pairs: List[List[str]], widths=(2.3 * inch, 3.9 * inch)) -> Table:
    data = [["Specification", "Value"]] + [[k, v] for k, v in pairs]
    t = Table(data, colWidths=list(widths), repeatRows=1)
    t.setStyle(TABLE_STYLE)
    return t


def _matrix_table(header: List[str], rows: List[List[str]]) -> Table:
    """
    Variant matrix.

    Column widths are proportional to content width so long headers such as
    "Interrupting Rating" are not sheared across a column boundary by the PDF
    text extractor -- a real artifact, but one that belongs in a parser test
    rather than in every sample document.
    """
    width = 7.2 * inch
    weights = [
        max(len(str(header[i])), *(len(str(r[i])) for r in rows)) if rows else len(str(header[i]))
        for i in range(len(header))
    ]
    total = float(sum(weights))
    cols = [max(0.55 * inch, width * (w / total)) for w in weights]
    t = Table([header] + rows, colWidths=cols, repeatRows=1)
    t.setStyle(TABLE_STYLE)
    return t


def build_datasheet(spec: Dict[str, Any], out: Path) -> Path:
    """Family datasheet: shared spec table plus a multi-column variant matrix."""
    story: List[Any] = []
    story.append(Paragraph(spec["manufacturer"], H1))
    story.append(Paragraph(f"{spec['series']} {spec['product']} - Technical Datasheet", H2))
    story.append(Spacer(1, 6))
    story.append(Paragraph(spec["blurb"], BODY))
    story.append(Spacer(1, 10))

    story.append(Paragraph("General Specifications", H2))
    story.append(Paragraph(
        "The following specifications apply to all models in this series.", SMALL))
    story.append(Spacer(1, 4))
    story.append(_kv_table([[k, v] for k, v in spec["shared"]]))

    story.append(Spacer(1, 14))
    story.append(Paragraph("Model Selection Table", H2))
    story.append(Paragraph(
        "Select the catalog number corresponding to the required rating. "
        "Values in this table are specific to each model.", SMALL))
    story.append(Spacer(1, 4))
    story.append(_matrix_table(spec["variant_header"], spec["variants"]))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"Document reference: {spec['series']}-DS-2026 Rev C. "
        f"Specifications subject to change without notice.", SMALL))

    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"{spec['series']} Datasheet", author=spec["manufacturer"],
    ).build(story)
    return out


def build_variant_submittal(spec: Dict[str, Any], variant_idx: int, out: Path) -> Path:
    """
    A single-SKU submittal sheet that is deliberately incomplete.

    It states only the variant-defining attributes and the part number. Every
    shared attribute (materials, certifications, temperature range) is absent,
    which is exactly the 'limited product information' case the brief describes
    and which gap filling must recover from the family datasheet.
    """
    variant = spec["variants"][variant_idx]
    header = spec["variant_header"]
    mpn = variant[0]

    story: List[Any] = []
    story.append(Paragraph(spec["manufacturer"], H1))
    story.append(Paragraph(f"Submittal Sheet - {mpn}", H2))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Product: {spec['product']}<br/>"
        f"Part Number: {mpn}<br/>"
        f"Series: {spec['series']}", BODY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Rated Performance", H2))
    pairs = [[header[i], variant[i]] for i in range(1, len(header))]
    story.append(_kv_table(pairs))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "For materials of construction, approvals and environmental ratings, "
        f"refer to the {spec['series']} series datasheet.", SMALL))

    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"{mpn} Submittal", author=spec["manufacturer"],
    ).build(story)
    return out


def build_new_release_submittal(spec: Dict[str, Any], variant: List[str], out: Path) -> Path:
    """
    A newly-released SKU documented ONLY by a one-page submittal.

    It is absent from the family datasheet's selection table and from the price
    file. Its own document states the variant-defining ratings and nothing else
    -- no materials, no approvals, no temperature range, no dimensions.

    This is the "limited product information" case at the centre of the brief:
    everything else about this product has to be inherited from its family,
    and every inherited value has to be labelled as inferred rather than read.
    """
    header = spec["variant_header"]
    mpn = variant[0]

    story: List[Any] = []
    story.append(Paragraph(spec["manufacturer"], H1))
    story.append(Paragraph(f"New Product Release - {mpn}", H2))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Product: {spec['product']}<br/>"
        f"Part Number: {mpn}<br/>"
        f"Series: {spec['series']}<br/>"
        f"Status: Released - full datasheet revision pending", BODY))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Ratings", H2))
    story.append(_kv_table([[header[i], variant[i]] for i in range(1, len(header))]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "This model shares all materials of construction, approvals and "
        f"environmental ratings with the {spec['series']} series.", SMALL))

    out.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"{mpn} New Release", author=spec["manufacturer"],
    ).build(story)
    return out


def build_scanned_page(spec: Dict[str, Any], out: Path) -> Path:
    """
    A page whose content is an image, with no text layer.

    Exercises the OCR path, and -- when no OCR engine is installed -- proves the
    system reports the gap honestly instead of silently dropping the page.
    """
    from reportlab.graphics.shapes import Drawing, Rect, String
    from reportlab.platypus import Image as RLImage

    try:
        from PIL import Image as PILImage, ImageDraw
    except ImportError:
        return build_datasheet(spec, out)

    img = PILImage.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 60, 1640, 300], outline="black", width=4)
    draw.text((100, 130), f"{spec['manufacturer']}", fill="black")
    draw.text((100, 190), f"{spec['series']} INSTALLATION NOTES (SCANNED)", fill="black")

    y = 400
    for label, value in spec["shared"][:8]:
        draw.text((100, y), f"{label}: {value}", fill="black")
        draw.line([100, y + 40, 1600, y + 40], fill="#999999", width=2)
        y += 90

    # Slight rotation, as a real scan would have.
    img = img.rotate(0.4, expand=False, fillcolor="white")
    tmp = out.parent / f".{out.stem}_scan.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(tmp)

    story = [RLImage(str(tmp), width=7.0 * inch, height=9.0 * inch)]
    SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.4 * inch, bottomMargin=0.4 * inch,
        title=f"{spec['series']} Scanned Notes",
    ).build(story)
    tmp.unlink(missing_ok=True)
    return out


# ===========================================================================
# HTML and CSV builders
# ===========================================================================


def build_web_page(spec: Dict[str, Any], out: Path) -> Path:
    """A manufacturer product page: marketing prose, a spec table, and JSON-LD."""
    sku = spec["web_sku"]
    variant = next(v for v in spec["variants"] if v[0] == sku)
    header = spec["variant_header"]
    conflict_label, conflict_value = spec["web_conflict"]

    rows: List[tuple] = []
    for i in range(1, len(header)):
        rows.append((header[i], variant[i]))
    for label, value in spec["shared"]:
        if label in ("Manufacturer", "Series", "Product Series"):
            continue
        rows.append((label, value))
    # Inject the deliberate disagreement with the datasheet.
    rows = [(l, conflict_value if l == conflict_label else v) for l, v in rows]
    if conflict_label not in [l for l, _ in rows]:
        rows.append((conflict_label, conflict_value))

    spec_rows = "\n".join(
        f"      <tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows
    )
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": f"{spec['manufacturer']} {sku} {spec['product']}",
        "sku": sku,
        "mpn": sku,
        "brand": {"@type": "Brand", "name": spec["manufacturer"]},
        "gtin13": spec["gtins"].get(sku, ""),
        "offers": {
            "@type": "Offer",
            "price": str(spec["prices"][sku]),
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        "additionalProperty": [
            {"@type": "PropertyValue", "name": label, "value": value}
            for label, value in rows[:6]
        ],
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{spec['manufacturer']} {sku} | {spec['product']}</title>
  <script type="application/ld+json">
{json.dumps(jsonld, indent=2)}
  </script>
</head>
<body>
  <nav>Home / Products / {spec['product']}</nav>
  <main>
    <h1>{spec['manufacturer']} {sku}</h1>
    <p>{spec['blurb']}</p>
    <p>Manufacturer: {spec['manufacturer']}</p>
    <p>Part Number: {sku}</p>
    <p>List Price: ${spec['prices'][sku]:.2f} USD</p>

    <h2>Specifications</h2>
    <table>
      <caption>Product Specifications</caption>
      <tr><th>Specification</th><th>Value</th></tr>
{spec_rows}
    </table>

    <h2>Features</h2>
    <ul>
      <li>Series: {spec['series']}</li>
      <li>Stocked for next-day dispatch</li>
      <li>Full technical support available</li>
    </ul>
  </main>
  <footer>Prices exclude tax. Specifications subject to change.</footer>
</body>
</html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def build_price_file(out: Path) -> Path:
    """
    A distributor price/attribute feed covering every SKU.

    Uses different column names to the datasheets on purpose, so alias
    resolution has to do real work.
    """
    rows: List[Dict[str, str]] = []
    for spec in list(CATALOG.values()) + [THERMOSTAT]:
        for variant in spec["variants"]:
            mpn = variant[0]
            rows.append({
                "Item Number": mpn,
                "Brand": spec["manufacturer"],
                "Product Line": spec["series"],
                "Description": f"{spec['manufacturer']} {mpn} {spec['product']}",
                "List Price": f"{spec['prices'].get(mpn, 0):.2f}",
                "UPC": spec["gtins"].get(mpn, ""),
                "Sold As": "Each",
                "Standard Pack": "1",
                "Country of Origin": dict(spec["shared"]).get("Country of Origin", ""),
            })

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return out


def build_product_image(spec: Dict[str, Any], out: Path, compliant: bool = True) -> Path:
    """A synthetic product photo. One is deliberately non-compliant."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return out

    size = (1000, 1000) if compliant else (420, 300)
    background = "white" if compliant else "#4a5568"
    img = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.rectangle([w * 0.25, h * 0.2, w * 0.75, h * 0.8],
                   fill="#2b6cb0", outline="#1a365d", width=max(2, w // 200))
    draw.rectangle([w * 0.33, h * 0.3, w * 0.67, h * 0.45], fill="#e2e8f0")
    draw.text((w * 0.3, h * 0.85), spec["series"], fill="#1a202c" if compliant else "white")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


# ===========================================================================
# Entry point
# ===========================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="Sources", help="Output directory for the sample catalog")
    args = parser.parse_args()

    root = Path(args.out)
    created: List[Path] = []

    for vertical, spec in CATALOG.items():
        d = root / spec["manufacturer"].replace(" ", "_")
        created.append(build_datasheet(spec, d / f"{spec['series']}_datasheet.pdf"))
        created.append(build_variant_submittal(spec, len(spec["variants"]) - 1,
                                               d / f"{spec['variants'][-1][0]}_submittal.pdf"))
        created.append(build_web_page(spec, d / f"{spec['web_sku']}_web_page.html"))
        created.append(build_product_image(spec, d / "images" / f"{spec['series']}_hero.png",
                                           compliant=(vertical != "hvac")))

    # A newly-released SKU that exists only as a thin submittal: not in the
    # family selection table, not in the price file. Everything except its
    # ratings must be inherited from the VX-Series family.
    electrical = CATALOG["electrical"]
    created.append(build_new_release_submittal(
        electrical,
        ["VX100-4P-C40", "4", "40 A", "C", "10 kA", "70 mm", "0.44 kg"],
        root / "Voltaris_Electric" / "VX100-4P-C40_new_release.pdf",
    ))

    # Thermostat family: datasheet only, no web page -- tests a thinner source set.
    ts_dir = root / THERMOSTAT["manufacturer"].replace(" ", "_")
    ts_spec = dict(THERMOSTAT)
    ts_spec["blurb"] = (
        "AF-TS commercial thermostats provide staged control for conventional and "
        "heat pump systems in light commercial buildings."
    )
    created.append(build_datasheet(ts_spec, ts_dir / "AF-TS_datasheet.pdf"))
    created.append(build_scanned_page(CATALOG["plumbing"],
                                      root / "Ferrum_Valve_Works" / "FV-3000_scanned_notes.pdf"))
    created.append(build_price_file(root / "distributor_price_file.csv"))

    print(f"Generated {len(created)} source files under {root}/\n")
    for path in sorted(created):
        if path.exists():
            print(f"  {path.relative_to(root)}  ({path.stat().st_size:,} bytes)")

    total_skus = sum(len(s["variants"]) for s in CATALOG.values()) + len(THERMOSTAT["variants"])
    print(f"\nExpected products: {total_skus} SKUs across {len(CATALOG) + 1} families.")


if __name__ == "__main__":
    main()
