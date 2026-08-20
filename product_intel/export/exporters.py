"""
Commerce-ready export.

"Commerce-ready" is only true if the data can leave the system in a format a
PIM or storefront actually ingests. Four targets:

  json    -- full fidelity, every value with its evidence. The audit artifact.
  csv     -- flat channel feed (Akeneo / Salsify / inRiver / Shopify shaped).
  bmecat  -- BMEcat 2005 with ETIM classification. The B2B interchange standard
             in electrical, plumbing and HVAC distribution.
  gdsn    -- GS1-shaped item payload for retail/wholesale sync.

Every exporter can filter to channel-ready products only, because publishing an
incomplete record is worse than publishing nothing.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from xml.etree import ElementTree as ET

from product_intel.models import Product
from product_intel.schema.dictionary import CategorySchema, Taxonomy

log = logging.getLogger(__name__)


def _render(av, attr) -> str:
    if av.value is None:
        return ""
    if isinstance(av.value, list):
        return ", ".join(str(v) for v in av.value)
    if attr is not None and attr.is_numeric:
        return f"{float(av.value):g}"
    return str(av.value)


def _filter(products: Sequence[Product], ready_only: bool) -> List[Product]:
    if not ready_only:
        return list(products)
    return [p for p in products if p.quality.channel_ready]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def export_json(
    products: Sequence[Product],
    taxonomy: Taxonomy,
    path: Path,
    ready_only: bool = False,
    include_evidence: bool = True,
) -> Dict[str, Any]:
    """Full-fidelity export. With evidence included this is the audit artifact."""
    selected = _filter(products, ready_only)
    items: List[Dict[str, Any]] = []

    for product in selected:
        schema = taxonomy.get(product.category_id)
        attributes: Dict[str, Any] = {}
        for code, av in product.attributes.items():
            attr = schema.get(code)
            entry: Dict[str, Any] = {"value": av.value, "unit": av.unit, "confidence": av.confidence}
            if attr is not None:
                entry["name"] = attr.name
                entry["generated"] = attr.generated
            if include_evidence:
                if av.evidence is not None:
                    entry["evidence"] = {
                        "source_id": av.evidence.source_id,
                        "source_kind": av.evidence.source_kind.value,
                        "locator": av.evidence.locator,
                        "page": av.evidence.page,
                        "quote": av.evidence.quote,
                        "method": av.evidence.method.value,
                        "quote_verified": av.evidence.quote_verified,
                    }
                if av.inference is not None:
                    entry["inference"] = av.inference.model_dump(mode="json")
            attributes[code] = entry

        items.append(
            {
                "product_id": product.identity.product_id,
                "manufacturer": product.identity.manufacturer,
                "mpn": product.identity.mpn,
                "gtin": product.identity.gtin,
                "series": product.identity.series,
                "base_product_id": product.identity.base_product_id,
                "category": {
                    "id": product.category_id,
                    "name": schema.name,
                    "etim": schema.etim,
                    "unspsc": schema.unspsc,
                    "vertical": schema.vertical,
                },
                "attributes": attributes,
                "quality": product.quality.model_dump(mode="json"),
                "conflicts": [c.model_dump(mode="json") for c in product.conflicts],
                "source_ids": product.source_ids,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": taxonomy.version,
        "product_count": len(items),
        "products": items,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return {"format": "json", "products": len(items), "path": str(path)}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def export_csv(
    products: Sequence[Product],
    taxonomy: Taxonomy,
    path: Path,
    ready_only: bool = False,
) -> Dict[str, Any]:
    """
    Flat channel feed.

    Columns are the union of attributes present across the selected products,
    with identity and quality columns first, so the file is usable as a
    drop-in import for most PIM tools.
    """
    selected = _filter(products, ready_only)
    if not selected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return {"format": "csv", "products": 0, "path": str(path)}

    attribute_codes: List[str] = []
    for product in selected:
        for code in product.attributes:
            if code not in attribute_codes:
                attribute_codes.append(code)

    lead = ["product_id", "manufacturer", "mpn", "gtin", "series",
            "category_id", "category_name", "etim_class", "unspsc"]
    tail = ["quality_overall", "completeness_ecommerce", "accuracy",
            "channel_ready", "conflict_count", "mean_confidence"]
    ordered_attrs = sorted(attribute_codes)
    header = lead + ordered_attrs + tail

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for product in selected:
            schema = taxonomy.get(product.category_id)
            confidences = [av.confidence for av in product.attributes.values()]
            row = [
                product.identity.product_id, product.identity.manufacturer,
                product.identity.mpn, product.identity.gtin or "",
                product.identity.series or "", product.category_id, schema.name,
                schema.etim or "", schema.unspsc or "",
            ]
            for code in ordered_attrs:
                av = product.attributes.get(code)
                row.append(_render(av, schema.get(code)) if av else "")
            row += [
                f"{product.quality.overall:.4f}",
                f"{product.quality.completeness_ecommerce:.4f}",
                f"{product.quality.accuracy:.4f}",
                "Y" if product.quality.channel_ready else "N",
                len(product.conflicts),
                f"{sum(confidences) / len(confidences):.4f}" if confidences else "0",
            ]
            writer.writerow(row)

    return {
        "format": "csv",
        "products": len(selected),
        "columns": len(header),
        "path": str(path),
    }


# ---------------------------------------------------------------------------
# BMEcat 2005 + ETIM
# ---------------------------------------------------------------------------


def export_bmecat(
    products: Sequence[Product],
    taxonomy: Taxonomy,
    path: Path,
    catalog_id: str = "PI_CATALOG",
    catalog_name: str = "Product Intelligence Export",
    ready_only: bool = True,
) -> Dict[str, Any]:
    """
    BMEcat 2005 T_NEW_CATALOG with ETIM feature groups.

    This is the format electrical and plumbing distributors actually exchange,
    which is why ETIM class codes are carried on the taxonomy rather than being
    an afterthought.
    """
    selected = _filter(products, ready_only)

    root = ET.Element("BMECAT", {"version": "2005"})
    header = ET.SubElement(root, "HEADER")
    catalog = ET.SubElement(header, "CATALOG")
    ET.SubElement(catalog, "LANGUAGE").text = "eng"
    ET.SubElement(catalog, "CATALOG_ID").text = catalog_id
    ET.SubElement(catalog, "CATALOG_VERSION").text = "1.0"
    ET.SubElement(catalog, "CATALOG_NAME").text = catalog_name
    ET.SubElement(catalog, "GENERATION_DATE").text = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    new_catalog = ET.SubElement(root, "T_NEW_CATALOG")

    for product in selected:
        schema = taxonomy.get(product.category_id)
        article = ET.SubElement(new_catalog, "ARTICLE")
        ET.SubElement(article, "SUPPLIER_AID").text = product.identity.mpn

        details = ET.SubElement(article, "ARTICLE_DETAILS")
        ET.SubElement(details, "DESCRIPTION_SHORT").text = (
            str(product.get("product_name") or f"{product.identity.manufacturer} {product.identity.mpn}")[:150]
        )
        long_desc = product.get("long_description") or product.get("short_description")
        if long_desc:
            ET.SubElement(details, "DESCRIPTION_LONG").text = str(long_desc)
        if product.identity.gtin:
            ET.SubElement(details, "EAN").text = product.identity.gtin
        ET.SubElement(details, "MANUFACTURER_NAME").text = product.identity.manufacturer
        ET.SubElement(details, "MANUFACTURER_AID").text = product.identity.mpn

        keywords = product.get("search_keywords")
        if isinstance(keywords, list):
            for kw in keywords[:10]:
                ET.SubElement(details, "KEYWORD").text = str(kw)

        order = ET.SubElement(article, "ARTICLE_ORDER_DETAILS")
        ET.SubElement(order, "ORDER_UNIT").text = str(product.get("uom") or "EA")
        ET.SubElement(order, "CONTENT_UNIT").text = str(product.get("uom") or "EA")
        ET.SubElement(order, "PRICE_QUANTITY").text = "1"
        ET.SubElement(order, "QUANTITY_MIN").text = "1"

        price_value = product.get("list_price_usd")
        if price_value is not None:
            price_details = ET.SubElement(article, "ARTICLE_PRICE_DETAILS")
            price = ET.SubElement(price_details, "ARTICLE_PRICE", {"price_type": "net_list"})
            ET.SubElement(price, "PRICE_AMOUNT").text = f"{float(price_value):.2f}"
            ET.SubElement(price, "PRICE_CURRENCY").text = "USD"

        # ETIM feature group
        if schema.etim:
            features = ET.SubElement(article, "ARTICLE_FEATURES")
            system = ET.SubElement(features, "REFERENCE_FEATURE_SYSTEM_NAME")
            system.text = "ETIM-8.0"
            ET.SubElement(features, "REFERENCE_FEATURE_GROUP_ID").text = schema.etim

            for code, av in sorted(product.attributes.items()):
                attr = schema.get(code)
                if attr is None or attr.generated or av.value in (None, "", []):
                    continue
                feature = ET.SubElement(features, "FEATURE")
                ET.SubElement(feature, "FNAME").text = attr.name
                ET.SubElement(feature, "FVALUE").text = _render(av, attr)
                if av.unit:
                    ET.SubElement(feature, "FUNIT").text = av.unit
                # Non-standard but harmless: carries our confidence through the feed.
                ET.SubElement(feature, "FDESCR").text = f"confidence={av.confidence:.2f}"

        ET.SubElement(article, "ARTICLE_REFERENCE", {"type": "base_product"}).text = (
            product.identity.base_product_id or ""
        )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return {"format": "bmecat", "products": len(selected), "path": str(path)}


# ---------------------------------------------------------------------------
# GDSN-shaped
# ---------------------------------------------------------------------------


def export_gdsn(
    products: Sequence[Product],
    taxonomy: Taxonomy,
    path: Path,
    gln: str = "0000000000000",
    ready_only: bool = True,
) -> Dict[str, Any]:
    """GS1-shaped trade item payload. JSON rather than the full GDSN XML envelope."""
    selected = _filter(products, ready_only)
    items = []

    for product in selected:
        schema = taxonomy.get(product.category_id)
        trade_item: Dict[str, Any] = {
            "gtin": product.identity.gtin or "",
            "informationProviderGLN": gln,
            "targetMarketCountryCode": "840",
            "gpcCategoryCode": schema.unspsc or "",
            "tradeItemDescription": str(product.get("product_name") or ""),
            "brandName": product.identity.manufacturer,
            "manufacturerPartNumber": product.identity.mpn,
            "additionalTradeItemIdentification": product.identity.alternate_mpns,
            "tradeItemMeasurements": {},
            "referencedFileDetail": [],
            "tradeItemClassification": {
                "etimClassCode": schema.etim,
                "unspscCode": schema.unspsc,
                "internalCategoryId": product.category_id,
            },
            "additionalTradeItemAttributes": [],
        }

        for code, unit_key in (("weight_kg", "netWeight"), ("width_mm", "width"),
                               ("height_mm", "height"), ("depth_mm", "depth"),
                               ("length_mm", "length")):
            av = product.attributes.get(code)
            if av is not None and isinstance(av.value, (int, float)):
                trade_item["tradeItemMeasurements"][unit_key] = {
                    "value": float(av.value),
                    "measurementUnitCode": av.unit,
                }

        for code, av in sorted(product.attributes.items()):
            attr = schema.get(code)
            if attr is None or av.value in (None, "", []):
                continue
            trade_item["additionalTradeItemAttributes"].append(
                {
                    "attributeName": attr.name,
                    "attributeCode": code,
                    "attributeValue": _render(av, attr),
                    "measurementUnitCode": av.unit,
                    "dataConfidence": round(av.confidence, 3),
                }
            )

        items.append(trade_item)

    payload = {
        "catalogueItemNotification": {
            "creationDateTime": datetime.now(timezone.utc).isoformat(),
            "documentStatusCode": "ORIGINAL",
            "tradeItemCount": len(items),
            "tradeItems": items,
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return {"format": "gdsn", "products": len(items), "path": str(path)}


EXPORTERS = {
    "json": export_json,
    "csv": export_csv,
    "bmecat": export_bmecat,
    "gdsn": export_gdsn,
}

DEFAULT_EXTENSIONS = {"json": ".json", "csv": ".csv", "bmecat": ".xml", "gdsn": ".json"}
