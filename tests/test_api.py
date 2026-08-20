"""
API tests.

Weighted towards the properties a client depends on and that are easy to break
silently:

  * every attribute view reports an origin, and inferred/generated values are
    never labelled as sourced
  * evidence survives the domain -> view mapping intact
  * a correction outranks the document it contradicts
  * an invalid correction is refused rather than stored
  * the SPA fallback serves the shell for deep links but cannot escape web/
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from product_intel.api.views import classify_origin, render_value
from product_intel.config import Settings
from product_intel.models import (
    AttributeValue,
    Evidence,
    ExtractionMethod,
    InferencePath,
    SourceKind,
)
from product_intel.schema.dictionary import load_taxonomy

TAX = load_taxonomy()
CB = TAX.get("electrical.circuit_breaker")


def make_client() -> TestClient:
    from product_intel.api.app import create_app

    return TestClient(create_app())


class TestOriginClassification(unittest.TestCase):
    """The single most important thing the UI reads. Getting it wrong lies to the user."""

    def test_sourced(self):
        av = AttributeValue(
            code="rated_current_a", value=63.0,
            evidence=Evidence(source_id="s1", source_kind=SourceKind.DATASHEET,
                              locator="p.1", quote="63 A", method=ExtractionMethod.NATIVE_TABLE),
        )
        self.assertEqual(classify_origin(av, CB.get("rated_current_a")), "sourced")

    def test_inherited_is_inferred_not_sourced(self):
        """An inherited value carries its donor's evidence. It must not read as sourced."""
        av = AttributeValue(
            code="country_of_origin", value="Germany",
            evidence=Evidence(source_id="s1", source_kind=SourceKind.DATASHEET,
                              locator="p.1", quote="Germany", method=ExtractionMethod.INHERITED),
            inference=InferencePath(strategy="family_inheritance", rationale="from the family sheet"),
        )
        self.assertEqual(classify_origin(av, CB.get("country_of_origin")), "inferred")

    def test_generated_wins_over_inference(self):
        av = AttributeValue(
            code="short_description", value="A breaker.",
            inference=InferencePath(strategy="generated_from_attributes", rationale="authored"),
        )
        self.assertEqual(classify_origin(av, CB.get("short_description")), "generated")

    def test_human_correction_outranks_everything(self):
        av = AttributeValue(
            code="body_material", value="Brass",
            evidence=Evidence(source_id="human:me", source_kind=SourceKind.USER,
                              locator="review queue", quote="corrected", method=ExtractionMethod.HUMAN),
            inference=InferencePath(strategy="family_inheritance", rationale="x"),
        )
        self.assertEqual(classify_origin(av, None), "human")

    def test_schema_default(self):
        av = AttributeValue(code="uom", value="EA",
                            normalization_notes=["schema default applied (EA)"])
        self.assertEqual(classify_origin(av, CB.get("uom")), "default")


class TestRendering(unittest.TestCase):
    def test_numeric_carries_its_unit(self):
        av = AttributeValue(code="rated_current_a", value=63.0, unit="A")
        self.assertEqual(render_value(av, CB.get("rated_current_a")), "63 A")

    def test_multi_valued_joins(self):
        av = AttributeValue(code="certifications", value=["UL 489", "CE"])
        self.assertEqual(render_value(av, CB.get("certifications")), "UL 489, CE")

    def test_none_renders_empty_not_the_string_none(self):
        self.assertEqual(render_value(AttributeValue(code="x", value=None), None), "")


class TestAPIReadPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = make_client()

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_overview_shape(self):
        body = self.client.get("/api/overview").json()
        for key in ("scorecard", "by_category", "by_manufacturer", "review", "attribute_coverage"):
            self.assertIn(key, body)

    def test_attribute_coverage_is_worst_first(self):
        """The point of the panel is to show what to chase; best-first would be useless."""
        rows = self.client.get("/api/overview").json()["attribute_coverage"]
        if len(rows) > 1:
            self.assertLessEqual(rows[0]["coverage"], rows[-1]["coverage"])

    def test_products_listing_and_filters(self):
        body = self.client.get("/api/products?limit=5").json()
        self.assertLessEqual(len(body["items"]), 5)
        self.assertIn("total", body)

        families = self.client.get("/api/products?families=true&limit=100").json()
        self.assertTrue(all(p["is_family"] for p in families["items"]))
        skus = self.client.get("/api/products?families=false&limit=100").json()
        self.assertTrue(all(not p["is_family"] for p in skus["items"]))

    def test_product_detail_carries_provenance(self):
        listing = self.client.get("/api/products?limit=1").json()
        if not listing["items"]:
            self.skipTest("catalog is empty")
        pid = listing["items"][0]["product_id"]
        detail = self.client.get(f"/api/products/{pid}").json()

        self.assertTrue(detail["attributes"])
        for a in detail["attributes"]:
            self.assertIn(a["origin"], ("sourced", "inferred", "generated", "human", "default"))
            if a["origin"] == "sourced":
                self.assertIsNotNone(a["evidence"], f"{a['code']} claims a source but has no evidence")
                self.assertTrue(a["evidence"]["quote"])
            if a["origin"] == "inferred":
                self.assertIsNotNone(a["inference"], f"{a['code']} is inferred but has no inference path")

    def test_lookup_by_mpn_not_just_id(self):
        listing = self.client.get("/api/products?limit=1").json()
        if not listing["items"]:
            self.skipTest("catalog is empty")
        mpn = listing["items"][0]["mpn"]
        r = self.client.get(f"/api/products/{mpn}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mpn"], mpn)

    def test_unknown_product_is_404(self):
        self.assertEqual(self.client.get("/api/products/NOPE-999").status_code, 404)

    def test_search_returns_scored_hits(self):
        body = self.client.get("/api/search?q=circuit+breaker&limit=5").json()
        self.assertIn("hits", body)
        scores = [h["score"] for h in body["hits"]]
        self.assertEqual(scores, sorted(scores, reverse=True), "hits must be ranked")

    def test_schema_endpoints(self):
        cats = self.client.get("/api/schema").json()
        self.assertTrue(cats)
        detail = self.client.get(f"/api/schema/{cats[0]['id']}").json()
        self.assertTrue(detail["attributes"])

    def test_mirror_highlight_offset_locates_the_quote(self):
        listing = self.client.get("/api/products?limit=20").json()
        for item in listing["items"]:
            detail = self.client.get(f"/api/products/{item['product_id']}").json()
            attr = next(
                (a for a in detail["attributes"]
                 if a["evidence"] and a["evidence"]["quote_verified"]),
                None,
            )
            if attr is None:
                continue
            ev = attr["evidence"]
            body = self.client.get(
                f"/api/sources/{ev['source_id']}/mirror", params={"highlight": ev["quote"]}
            ).json()
            self.assertIsNotNone(
                body["highlight_offset"],
                "a verified quote must be locatable in its own mirror",
            )
            return
        self.skipTest("no verified quotes in this catalog")


class TestAPIWritePaths(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def _first_enum_attribute(self):
        listing = self.client.get("/api/products?limit=40").json()
        for item in listing["items"]:
            detail = self.client.get(f"/api/products/{item['product_id']}").json()
            for a in detail["attributes"]:
                if a["datatype"] == "enum":
                    return item["product_id"], a
        return None, None

    def test_correction_becomes_human_sourced_and_outranks_documents(self):
        pid, attr = self._first_enum_attribute()
        if pid is None:
            self.skipTest("no enum attribute available")

        schema = self.client.get("/api/products/" + pid).json()
        allowed = self.client.get(
            f"/api/schema/{schema['category_id']}"
        ).json()
        options = next(a["allowed_values"] for a in allowed["attributes"] if a["code"] == attr["code"])
        target = next((v for v in options if v != attr["value"]), options[0])

        r = self.client.post("/api/review/correct", json={
            "product_id": pid, "code": attr["code"], "value": target, "reviewer": "unittest",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["applied_value"], target)

        after = self.client.get(f"/api/products/{pid}").json()
        corrected = next(a for a in after["attributes"] if a["code"] == attr["code"])
        self.assertEqual(corrected["origin"], "human")
        self.assertEqual(corrected["value"], target)
        self.assertEqual(corrected["evidence"]["method"], "human")

    def test_invalid_value_is_refused_not_stored(self):
        pid, attr = self._first_enum_attribute()
        if pid is None:
            self.skipTest("no enum attribute available")
        before = self.client.get(f"/api/products/{pid}").json()
        original = next(a for a in before["attributes"] if a["code"] == attr["code"])["value"]

        r = self.client.post("/api/review/correct", json={
            "product_id": pid, "code": attr["code"], "value": "definitely-not-a-legal-value",
        })
        self.assertEqual(r.status_code, 422)

        after = self.client.get(f"/api/products/{pid}").json()
        self.assertEqual(
            next(a for a in after["attributes"] if a["code"] == attr["code"])["value"],
            original,
            "a refused correction must leave the stored value untouched",
        )

    def test_a_second_correction_supersedes_the_first(self):
        """Re-correcting must stick. Keeping the earliest human value silently ignores the reviewer."""
        pid, attr = self._first_enum_attribute()
        if pid is None:
            self.skipTest("no enum attribute available")

        detail = self.client.get(f"/api/products/{pid}").json()
        options = next(
            a["allowed_values"]
            for a in self.client.get(f"/api/schema/{detail['category_id']}").json()["attributes"]
            if a["code"] == attr["code"]
        )
        if len(options) < 2:
            self.skipTest("attribute has only one legal value")

        first, second = options[0], options[1]
        self.client.post("/api/review/correct", json={
            "product_id": pid, "code": attr["code"], "value": first, "reviewer": "unittest",
        })
        self.client.post("/api/review/correct", json={
            "product_id": pid, "code": attr["code"], "value": second, "reviewer": "unittest",
        })

        after = self.client.get(f"/api/products/{pid}").json()
        current = next(a for a in after["attributes"] if a["code"] == attr["code"])
        self.assertEqual(current["value"], second, "the most recent correction must win")

    def test_correction_to_an_unknown_attribute_is_rejected(self):
        listing = self.client.get("/api/products?limit=1").json()
        if not listing["items"]:
            self.skipTest("catalog is empty")
        r = self.client.post("/api/review/correct", json={
            "product_id": listing["items"][0]["product_id"],
            "code": "not_a_real_attribute", "value": "x",
        })
        self.assertEqual(r.status_code, 400)


class TestLLMToggleAPI(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("PI_LLM_ENABLED", "PI_LLM_PROVIDER", "PI_LLM_MODEL")}
        self.client = make_client()

    def tearDown(self):
        self.client.post("/api/llm", json={"provider": "ollama"})
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_switching_changes_mode_and_model(self):
        offline = self.client.post("/api/llm", json={"provider": "ollama"}).json()
        self.assertEqual(offline["mode"], "offline")
        self.assertEqual(offline["model"], "qwen2.5:14b")

        cloud = self.client.post("/api/llm", json={"provider": "bedrock"}).json()
        self.assertEqual(cloud["mode"], "cloud")
        self.assertTrue(cloud["model"].startswith("us."))
        self.assertIsNotNone(cloud["region"])

        off = self.client.post("/api/llm", json={"provider": "off"}).json()
        self.assertEqual(off["mode"], "off")
        self.assertFalse(off["enabled"])
        self.assertIsNone(off["model"])

    def test_each_provider_remembers_its_own_model(self):
        self.client.post("/api/llm", json={"provider": "bedrock", "model": "us.amazon.nova-lite-v1:0"})
        self.client.post("/api/llm", json={"provider": "ollama"})
        back = self.client.post("/api/llm", json={"provider": "bedrock"}).json()
        self.assertEqual(back["model"], "us.amazon.nova-lite-v1:0")

    def test_unavailable_backend_offers_a_remedy(self):
        status = self.client.post("/api/llm", json={"provider": "ollama"}).json()
        if not status["available"]:
            self.assertTrue(status["remediation"], "an unreachable backend must say how to fix it")

    def test_invalid_provider_is_rejected(self):
        self.assertEqual(
            self.client.post("/api/llm", json={"provider": "chatgpt"}).status_code, 422
        )


class TestStaticServing(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_root_serves_the_shell(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Product Intelligence", r.text)

    def test_deep_link_falls_back_to_the_shell(self):
        """Client-side routing means /products/X must return index.html, not 404."""
        r = self.client.get("/products/VX100-2P-C20")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<div id=\"app\"", r.text)

    def test_path_traversal_cannot_escape_the_web_directory(self):
        r = self.client.get("/../settings.json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("llm_provider", r.text, "served a file outside web/")


if __name__ == "__main__":
    unittest.main()
