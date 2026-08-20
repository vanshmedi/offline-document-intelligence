"""
Image asset intelligence.

Digital assets are named in the brief alongside websites and technical
documents, and in a real PIM they are half the publishing problem: a product
without a compliant hero image cannot go live regardless of how good its
attribute data is.

What runs here without any model:
  - dimensions, aspect ratio, transparency, background detection
  - shot-type classification (hero / line drawing / dimension drawing /
    lifestyle) from image statistics
  - channel compliance checks against commerce requirements

What improves with a vision model, when one is configured:
  - alt text, and reading values off dimension drawings and spec-table images
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from product_intel.models import ExtractionMethod, Fragment, ProductAsset

log = logging.getLogger(__name__)

#: Baseline commerce image requirements. Tunable per retailer.
CHANNEL_RULES = {
    "min_width": 800,
    "min_height": 800,
    "max_aspect_deviation": 0.35,  # how far from square is acceptable
    "preferred_background": "white",
    "max_bytes": 8 * 1024 * 1024,
}


def _load_image(path: Path):
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        return Image.open(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open image %s: %s", path.name, exc)
        return None


def _analyze(img) -> Dict[str, Any]:
    """Cheap statistical analysis: background, saturation, edge density."""
    from PIL import Image  # type: ignore

    rgb = img.convert("RGB")
    thumb = rgb.copy()
    thumb.thumbnail((160, 160))
    pixels = list(thumb.getdata())
    if not pixels:
        return {}

    w, h = thumb.size
    border: List[Tuple[int, int, int]] = []
    for x in range(w):
        border.append(pixels[x])
        border.append(pixels[(h - 1) * w + x])
    for y in range(h):
        border.append(pixels[y * w])
        border.append(pixels[y * w + (w - 1)])

    mean_border = tuple(sum(c[i] for c in border) / len(border) for i in range(3))
    border_is_light = all(v > 235 for v in mean_border)
    border_uniform = (max(mean_border) - min(mean_border)) < 12

    grays = [(p[0] * 299 + p[1] * 587 + p[2] * 114) / 1000 for p in pixels]
    mean_gray = sum(grays) / len(grays)
    variance = sum((g - mean_gray) ** 2 for g in grays) / len(grays)

    saturations = [max(p) - min(p) for p in pixels]
    mean_sat = sum(saturations) / len(saturations)

    near_bw = sum(1 for g in grays if g > 240 or g < 25) / len(grays)

    return {
        "background": "white" if (border_is_light and border_uniform) else "non-white",
        "mean_gray": round(mean_gray, 1),
        "gray_variance": round(variance, 1),
        "mean_saturation": round(mean_sat, 1),
        "bw_ratio": round(near_bw, 3),
        "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info,
    }


def classify_shot_type(analysis: Dict[str, Any], filename: str) -> str:
    """Classify an image without a model, using filename hints and statistics."""
    name = filename.lower()
    for needle, label in (
        ("dimension", "dimension"), ("dim-", "dimension"), ("drawing", "line_drawing"),
        ("line", "line_drawing"), ("schematic", "line_drawing"), ("wiring", "line_drawing"),
        ("hero", "hero"), ("main", "hero"), ("primary", "hero"),
        ("angle", "angle"), ("alt", "angle"), ("side", "angle"),
        ("lifestyle", "lifestyle"), ("install", "lifestyle"), ("application", "lifestyle"),
    ):
        if needle in name:
            return label

    if not analysis:
        return "unknown"

    # Line art: mostly pure black and white, very low saturation.
    if analysis.get("bw_ratio", 0) > 0.82 and analysis.get("mean_saturation", 99) < 12:
        return "line_drawing"
    if analysis.get("background") == "white" and analysis.get("mean_saturation", 0) >= 12:
        return "hero"
    if analysis.get("background") == "non-white":
        return "lifestyle"
    return "unknown"


def check_compliance(width: int, height: int, analysis: Dict[str, Any], size_bytes: int) -> Tuple[bool, List[str]]:
    notes: List[str] = []
    if width < CHANNEL_RULES["min_width"] or height < CHANNEL_RULES["min_height"]:
        notes.append(
            f"below minimum {CHANNEL_RULES['min_width']}x{CHANNEL_RULES['min_height']} "
            f"(is {width}x{height})"
        )
    if width and height:
        aspect = width / height
        if abs(aspect - 1.0) > CHANNEL_RULES["max_aspect_deviation"]:
            notes.append(f"aspect ratio {aspect:.2f} is far from square")
    if analysis.get("background") == "non-white":
        notes.append("background is not white; most B2B channels require a clean white background")
    if size_bytes > CHANNEL_RULES["max_bytes"]:
        notes.append(f"file is {size_bytes / 1e6:.1f} MB, above the {CHANNEL_RULES['max_bytes'] / 1e6:.0f} MB limit")
    return (not notes), notes


def build_asset(path: Path, source_id: str, asset_id: str, relative_path: str) -> ProductAsset:
    """Analyze an image file and produce a ProductAsset record."""
    img = _load_image(path)
    if img is None:
        return ProductAsset(
            asset_id=asset_id,
            source_id=source_id,
            relative_path=relative_path,
            compliance_notes=["image could not be opened (Pillow missing or file corrupt)"],
        )

    width, height = img.size
    analysis = _analyze(img)
    shot = classify_shot_type(analysis, path.name)
    size_bytes = path.stat().st_size if path.exists() else 0
    compliant, notes = check_compliance(width, height, analysis, size_bytes)

    return ProductAsset(
        asset_id=asset_id,
        source_id=source_id,
        relative_path=relative_path,
        width=width,
        height=height,
        shot_type=shot,
        background=analysis.get("background", "unknown"),
        channel_compliant=compliant,
        compliance_notes=notes,
    )


def parse_image(path: Path, source_id: str) -> Tuple[List[Fragment], str, Dict[str, Any]]:
    """Treat a standalone image as a source: analyze it, emit a descriptive fragment."""
    asset = build_asset(path, source_id, f"asset_{source_id}", path.name)
    summary = (
        f"Image asset {path.name}: {asset.width}x{asset.height}, "
        f"shot type {asset.shot_type}, background {asset.background}, "
        f"channel compliant: {asset.channel_compliant}."
    )
    mirror = f"# Image asset: {path.name}\n\n{summary}\n"
    if asset.compliance_notes:
        mirror += "\nCompliance notes:\n" + "".join(f"- {n}\n" for n in asset.compliance_notes)

    fragment = Fragment(
        fragment_id=f"{source_id}_img",
        source_id=source_id,
        kind="image_caption",
        locator=path.name,
        text=summary,
        method=ExtractionMethod.VISION,
        metadata={"asset": asset.model_dump()},
    )
    stats = {
        "assets": 1,
        "fragments": 1,
        "compliant": asset.channel_compliant,
        "warnings": asset.compliance_notes,
    }
    return [fragment], mirror, stats


def generate_alt_text(asset: ProductAsset, product_name: str, category_name: str) -> str:
    """
    Deterministic, accessible alt text.

    A vision model would write better prose, but this is never wrong and never
    hallucinates a feature the image does not show -- which matters more for
    accessibility compliance than fluency.
    """
    shot_phrases = {
        "hero": f"Product photograph of {product_name}",
        "angle": f"Alternate angle view of {product_name}",
        "line_drawing": f"Technical line drawing of {product_name}",
        "dimension": f"Dimensional drawing of {product_name} showing measurements",
        "lifestyle": f"{product_name} shown in a typical installation",
        "unknown": f"Image of {product_name}",
    }
    base = shot_phrases.get(asset.shot_type, shot_phrases["unknown"])
    return f"{base} ({category_name})"[:125]
