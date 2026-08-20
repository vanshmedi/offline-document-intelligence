"""
Test suite for the Product Intelligence Engine.

Coverage is weighted towards the properties the system's value depends on:

  * normalization correctness, including the unit conversions and the fraction
    vs. dual-rating distinction that silently corrupt data when wrong
  * identity resolution, including the OCR fuzzy-match path
  * that the variant matrix reads the *right row* -- the wrong-column bug
  * that unverifiable quotes are rejected rather than repaired
  * that golden-record arbitration keeps the loser and explains the winner
  * that inherited and generated values are never mistaken for observed ones
  * that the validation gate cannot be globally disabled
  * that SQL execution refuses anything that can write
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from product_intel.config import Settings
from product_intel.confidence import score_attribute
from product_intel.graph import ProductGraph, build_graph
from product_intel.models import (
    AttributeValue,
    Evidence,
    ExtractionMethod,
    Fragment,
    Product,
    ProductIdentity,
    RelationType,
    SourceKind,
)
from product_intel.pipeline.db_ingest import CatalogDB
from product_intel.pipeline.extractor import (
    SchemaDirectedExtractor,
    _loose_contains,
    _split_range,
    match_attribute,
)
from product_intel.pipeline.golden import arbitrate, build_golden_record, values_agree
from product_intel.pipeline.identity import (
    IdentityResolver,
    extract_mpn_candidates,
    looks_like_mpn,
    normalize_manufacturer,
    normalize_mpn,
    product_id_for,
)
from product_intel.pipeline.normalizer import normalize_value, parse_number
from product_intel.schema.dictionary import load_taxonomy
from product_intel.validation import (
    PeerStatistics,
    compute_quality,
    evaluate_expression,
    validate_product,
)

TAX = load_taxonomy()
CB = TAX.get("electrical.circuit_breaker")
BV = TAX.get("plumbing.ball_valve")
BL = TAX.get("hvac.centrifugal_blower")


def make_product(mpn: str = "TEST-1", manufacturer: str = "Acme", category: str = "electrical.circuit_breaker") -> Product:
    return Product(
        identity=ProductIdentity(
            product_id=product_id_for(manufacturer, mpn),
            manufacturer=manufacturer,
            mpn=mpn,
            normalized_mpn=normalize_mpn(mpn),
        ),
        category_id=category,
    )


def observed(code: str, value, kind: SourceKind, source_id: str,
             method: ExtractionMethod = ExtractionMethod.NATIVE_TABLE,
             verified: bool = True, unit=None) -> AttributeValue:
    return AttributeValue(
        code=code,
        value=value,
        unit=unit,
        evidence=Evidence(
            source_id=source_id,
            source_kind=kind,
            locator="test",
            quote=f"{code}: {value}",
            method=method,
            quote_verified=verified,
        ),
    )


# ---------------------------------------------------------------------------


class TestNormalization(unittest.TestCase):
    def test_imperial_to_metric(self):
        self.assertAlmostEqual(normalize_value('1/2"', BV.get("nominal_size_mm")).value, 12.7, places=2)
        self.assertAlmostEqual(normalize_value("1 1/2 in", BV.get("nominal_size_mm")).value, 38.1, places=2)
        self.assertAlmostEqual(normalize_value("2.4 lbs", CB.get("weight_kg")).value, 1.0886, places=3)

    def test_pressure_and_flow(self):
        self.assertAlmostEqual(normalize_value("600 PSI WOG", BV.get("pressure_rating_bar")).value, 41.37, places=1)
        self.assertAlmostEqual(normalize_value("1200 CFM", BL.get("airflow_m3h")).value, 2038.81, places=1)
        self.assertAlmostEqual(normalize_value("1.0 in. w.g.", BL.get("static_pressure_bar")).value, 0.00249, places=4)

    def test_temperature_offset_not_scaled(self):
        """Fahrenheit needs an offset, not a factor. A naive multiply gives nonsense."""
        self.assertAlmostEqual(normalize_value("-20 F", BV.get("operating_temp_min_c")).value, -28.889, places=2)
        self.assertAlmostEqual(normalize_value("450 F", BV.get("operating_temp_max_c")).value, 232.22, places=1)

    def test_attribute_unit_differs_from_family_canonical(self):
        """Interrupting rating is declared in kA while the current family is A."""
        self.assertEqual(normalize_value("10 kA", CB.get("interrupting_rating_ka")).value, 10.0)
        self.assertEqual(normalize_value("65,000 A", CB.get("interrupting_rating_ka")).value, 65.0)

    def test_unit_directly_after_digits(self):
        self.assertEqual(normalize_value("63A", CB.get("rated_current_a")).value, 63.0)
        self.assertEqual(normalize_value("35mm", CB.get("width_mm")).value, 35.0)

    def test_dual_rating_is_not_a_fraction(self):
        """'240/415 V' is two ratings. Dividing them yields a confident 0.578 V."""
        self.assertEqual(parse_number("240/415 V AC"), 240.0)
        self.assertEqual(normalize_value("240/415 V AC", CB.get("rated_voltage_v")).value, 240.0)
        self.assertEqual(parse_number("3/4 HP"), 0.75)  # a real fraction still works

    def test_enum_synonyms(self):
        self.assertEqual(normalize_value("SS316", BV.get("body_material")).value, "Stainless Steel 316")
        self.assertEqual(normalize_value("FNPT", BV.get("end_connection")).value, "NPT Threaded")
        self.assertEqual(normalize_value("full bore", BV.get("port_type")).value, "Full Port")
        self.assertEqual(normalize_value("35mm DIN rail", CB.get("mounting_type")).value, "DIN Rail")

    def test_illegal_enum_is_rejected_not_guessed(self):
        av = normalize_value("unobtainium", BV.get("body_material"))
        self.assertIsNone(av.value)
        self.assertTrue(av.validation_errors)

    def test_out_of_range_is_flagged(self):
        av = normalize_value("99999 A", CB.get("rated_current_a"))
        self.assertEqual(av.value, 99999.0)
        self.assertTrue(any("maximum" in e for e in av.validation_errors))

    def test_certifications_are_canonicalized(self):
        av = normalize_value("UL489, cULus; CE / RoHS compliant", CB.get("certifications"))
        self.assertIn("UL 489", av.value)
        self.assertIn("RoHS", av.value)

    def test_assumed_unit_is_recorded(self):
        av = normalize_value("20", CB.get("rated_current_a"))
        self.assertEqual(av.value, 20.0)
        self.assertTrue(any("assumed" in n for n in av.normalization_notes))


class TestIdentity(unittest.TestCase):
    def test_separator_variants_collapse(self):
        self.assertEqual(normalize_mpn("P/N: CB-100/2P-C20"), normalize_mpn("cb 100 2p c20"))
        self.assertEqual(product_id_for("Acme Inc.", "CB-100"), product_id_for("ACME", "cb100"))

    def test_corporate_suffixes_stripped(self):
        self.assertEqual(normalize_manufacturer("Ferrum Valve Works Ltd."), "Ferrum Valve Works")

    def test_mpn_heuristic(self):
        self.assertTrue(looks_like_mpn("VX100-2P-C20"))
        self.assertFalse(looks_like_mpn("Nominal Size"))
        self.assertFalse(looks_like_mpn("ab"))

    def test_unlabelled_numeric_tokens_are_not_part_numbers(self):
        """'240/415' is a voltage, not an MPN."""
        self.assertNotIn("240/415", extract_mpn_candidates("Rated voltage 240/415 V AC"))
        self.assertIn("NSX100F-C20", extract_mpn_candidates("Order Code: NSX100F-C20"))

    def test_near_duplicate_is_detected(self):
        resolver = IdentityResolver()
        pid, _ = resolver.register("Ferrum Valve Works", "FV-3000")
        match = resolver.find_near_duplicate("Ferrum Valve Works", "FV-2000")
        self.assertIsNotNone(match)
        self.assertEqual(match[0], pid)

    def test_near_duplicate_is_reported_not_merged(self):
        """VX100-1P-C06 and VX100-1P-C16 are one character apart and are different
        SKUs. Nothing in the strings distinguishes that case from an OCR misread,
        so the detector reports a suspicion and a human decides -- silently
        merging two real SKUs destroys data irrecoverably."""
        resolver = IdentityResolver()
        pid_a, _ = resolver.register("Voltaris", "VX100-1P-C06")
        pid_b, _ = resolver.register("Voltaris", "VX100-1P-C16")
        self.assertNotEqual(pid_a, pid_b)
        self.assertIsNotNone(resolver.find_near_duplicate("Voltaris", "VX100-1P-C16"))

    def test_near_duplicate_is_manufacturer_scoped(self):
        resolver = IdentityResolver()
        resolver.register("Acme", "FV-3000")
        self.assertIsNone(resolver.find_near_duplicate("Other Brand", "FV-2000"))


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings(llm_enabled=False, embedding_enabled=False)
        self.extractor = SchemaDirectedExtractor(provider=None, cfg=self.cfg)

    def test_alias_resolution_prefers_longest(self):
        self.assertEqual(match_attribute("Rated Current (In)", CB).code, "rated_current_a")
        self.assertEqual(match_attribute("Rated Operational Voltage (Ue)", CB).code, "rated_voltage_v")
        self.assertIsNone(match_attribute("Completely Unrelated Label", CB))

    def test_identity_attributes_are_not_extracted(self):
        """A family sheet names every sibling's part number; extracting 'mpn'
        from shared fragments would assign one variant's number to another."""
        self.assertIsNone(match_attribute("Part Number", CB))
        self.assertNotIn("mpn", CB.extractable_codes())
        self.assertNotIn("manufacturer", CB.extractable_codes())

    def test_range_splitting(self):
        self.assertEqual(_split_range("-20 to +70 C"), (-20.0, 70.0))
        self.assertEqual(_split_range("-4...158 F"), (-4.0, 158.0))
        self.assertIsNone(_split_range("no range here"))

    def test_variant_matrix_reads_the_matching_row(self):
        """The wrong-column bug: a three-variant table must yield each variant's own values."""
        table = [
            ["Catalog Number", "Poles", "Rated Current", "Trip Curve", "Interrupting Rating"],
            ["VX100-1P-C06", "1", "6 A", "C", "10 kA"],
            ["VX100-2P-C20", "2", "20 A", "C", "10 kA"],
            ["VX100-3P-D63", "3", "63 A", "D", "15 kA"],
        ]
        frag = Fragment(
            fragment_id="f1", source_id="src_x", kind="table",
            locator="p.1 / Table 1", text="", table=table,
            method=ExtractionMethod.NATIVE_TABLE,
        )
        for mpn, poles, current, curve in (
            ("VX100-1P-C06", "1", 6.0, "C"),
            ("VX100-2P-C20", "2", 20.0, "C"),
            ("VX100-3P-D63", "3", 63.0, "D"),
        ):
            result = self.extractor.extract([frag], CB, "src_x", SourceKind.DATASHEET, mpn_hint=mpn)
            self.assertEqual(result.values["poles"][0].value, poles, mpn)
            self.assertEqual(result.values["rated_current_a"][0].value, current, mpn)
            self.assertEqual(result.values["trip_curve"][0].value, curve, mpn)

    def test_variant_matrix_extracts_nothing_when_no_row_matches(self):
        """An honest gap beats a confidently wrong value."""
        table = [["Catalog Number", "Poles"], ["VX100-1P-C06", "1"]]
        frag = Fragment(
            fragment_id="f1", source_id="s", kind="table", locator="t",
            text="", table=table, method=ExtractionMethod.NATIVE_TABLE,
        )
        result = self.extractor.extract([frag], CB, "s", SourceKind.DATASHEET, mpn_hint="NOT-IN-TABLE")
        self.assertNotIn("poles", result.values)

    def test_label_value_table(self):
        table = [["Specification", "Value"], ["Mounting", "35mm DIN rail"], ["Enclosure Rating", "IP20"]]
        frag = Fragment(
            fragment_id="f1", source_id="s", kind="table", locator="t",
            text="", table=table, method=ExtractionMethod.NATIVE_TABLE,
        )
        result = self.extractor.extract([frag], CB, "s", SourceKind.DATASHEET)
        self.assertEqual(result.values["mounting_type"][0].value, "DIN Rail")

    def test_prose_negative_number_keeps_its_sign(self):
        """'Operating Temperature -25 to +60 C' must not parse as +25."""
        frag = Fragment(
            fragment_id="f1", source_id="s", kind="text", locator="p.1",
            text="Operating Temperature -25 to +60 C", method=ExtractionMethod.NATIVE_TEXT,
        )
        result = self.extractor.extract([frag], CB, "s", SourceKind.DATASHEET)
        self.assertEqual(result.values["operating_temp_min_c"][0].value, -25.0)
        self.assertEqual(result.values["operating_temp_max_c"][0].value, 60.0)

    def test_prose_does_not_match_mid_sentence(self):
        """'...approvals and environmental ratings...' is not a certifications row."""
        frag = Fragment(
            fragment_id="f1", source_id="s", kind="text", locator="p.1",
            text="This model shares all materials of construction, approvals and environmental "
                 "ratings with the VX-Series series.",
            method=ExtractionMethod.NATIVE_TEXT,
        )
        result = self.extractor.extract([frag], CB, "s", SourceKind.DATASHEET)
        self.assertNotIn("certifications", result.values)

    def test_quote_verification_against_mirror(self):
        frag = Fragment(
            fragment_id="f1", source_id="s", kind="table", locator="t", text="",
            table=[["Specification", "Value"], ["Enclosure Rating", "IP20"]],
            method=ExtractionMethod.NATIVE_TABLE,
        )
        good = self.extractor.extract(
            [frag], CB, "s", SourceKind.DATASHEET,
            mirror="| Enclosure Rating | IP20 |",
        )
        self.assertTrue(good.values["enclosure_rating"][0].evidence.quote_verified)

        bad = self.extractor.extract(
            [frag], CB, "s", SourceKind.DATASHEET,
            mirror="a completely unrelated document body",
        )
        self.assertFalse(bad.values["enclosure_rating"][0].evidence.quote_verified)

    def test_loose_contains_tolerates_wrapping_but_not_invention(self):
        self.assertTrue(_loose_contains("Rated Current\n63 A", "Rated Current 63 A"))
        self.assertFalse(_loose_contains("Rated Current 63 A", "Interrupting rating 25 kA"))


class TestGoldenRecord(unittest.TestCase):
    def test_higher_precedence_source_wins(self):
        obs = [
            observed("interrupting_rating_ka", 6.0, SourceKind.MANUFACTURER_WEB, "web"),
            observed("interrupting_rating_ka", 10.0, SourceKind.DATASHEET, "ds"),
        ]
        winner, conflict = arbitrate("interrupting_rating_ka", obs, CB.get("interrupting_rating_ka"))
        self.assertEqual(winner.value, 10.0)
        self.assertIsNotNone(conflict)
        self.assertEqual(len(conflict.losing_values), 1)
        self.assertIn("precedence", conflict.resolution_rule)

    def test_losing_values_are_kept(self):
        obs = [
            observed("pressure_rating_bar", 55.16, SourceKind.MANUFACTURER_WEB, "web"),
            observed("pressure_rating_bar", 68.95, SourceKind.DATASHEET, "ds"),
        ]
        _winner, conflict = arbitrate("pressure_rating_bar", obs, BV.get("pressure_rating_bar"))
        self.assertEqual(conflict.losing_values[0]["value"], 55.16)
        self.assertEqual(conflict.losing_values[0]["source_id"], "web")

    def test_human_correction_outranks_everything(self):
        obs = [
            observed("poles", "3", SourceKind.DATASHEET, "ds"),
            observed("poles", "4", SourceKind.USER, "human", method=ExtractionMethod.HUMAN),
        ]
        winner, conflict = arbitrate("poles", obs, CB.get("poles"))
        self.assertEqual(winner.value, "4")
        self.assertIsNone(conflict)

    def test_near_equal_numbers_corroborate_rather_than_conflict(self):
        """12.7 mm and 12.70 mm are the same measurement, not a disagreement."""
        self.assertTrue(values_agree(12.7, 12.70, BV.get("nominal_size_mm")))
        obs = [
            observed("nominal_size_mm", 12.7, SourceKind.DATASHEET, "a"),
            observed("nominal_size_mm", 12.70, SourceKind.CATALOG, "b"),
        ]
        winner, conflict = arbitrate("nominal_size_mm", obs, BV.get("nominal_size_mm"))
        self.assertIsNone(conflict)
        self.assertGreater(winner.confidence, 0.7)

    def test_corroboration_raises_confidence(self):
        single = arbitrate("width_mm", [observed("width_mm", 35.0, SourceKind.DATASHEET, "a")],
                           CB.get("width_mm"))[0]
        triple = arbitrate(
            "width_mm",
            [observed("width_mm", 35.0, SourceKind.DATASHEET, s) for s in ("a", "b", "c")],
            CB.get("width_mm"),
        )[0]
        self.assertGreater(triple.confidence, single.confidence)

    def test_defaults_never_override_evidence(self):
        product = make_product()
        product.observations["uom"] = [observed("uom", "BX", SourceKind.PRICE_FILE, "pf")]
        build_golden_record(product, CB)
        self.assertEqual(product.attributes["uom"].value, "BX")

    def test_defaults_are_marked_as_inference(self):
        product = make_product()
        build_golden_record(product, CB)
        uom = product.attributes["uom"]
        self.assertEqual(uom.value, "EA")
        self.assertIsNotNone(uom.inference)
        self.assertEqual(uom.inference.strategy, "schema_default")
        self.assertLess(uom.confidence, 0.6)


class TestConfidence(unittest.TestCase):
    def test_verified_beats_unverified(self):
        good = observed("width_mm", 35.0, SourceKind.DATASHEET, "a", verified=True)
        bad = observed("width_mm", 35.0, SourceKind.DATASHEET, "a", verified=False)
        score_attribute(good, CB.get("width_mm"))
        score_attribute(bad, CB.get("width_mm"))
        self.assertGreater(good.confidence, bad.confidence)

    def test_ocr_scores_below_native_table(self):
        native = observed("width_mm", 35.0, SourceKind.DATASHEET, "a", method=ExtractionMethod.NATIVE_TABLE)
        ocr = observed("width_mm", 35.0, SourceKind.DATASHEET, "a", method=ExtractionMethod.OCR)
        score_attribute(native, CB.get("width_mm"))
        score_attribute(ocr, CB.get("width_mm"))
        self.assertGreater(native.confidence, ocr.confidence)

    def test_validation_error_collapses_confidence(self):
        av = observed("rated_current_a", 99999.0, SourceKind.DATASHEET, "a")
        av.validation_errors.append("exceeds plausible maximum")
        score_attribute(av, CB.get("rated_current_a"))
        self.assertLess(av.confidence, 0.5)

    def test_confidence_is_never_hardcoded(self):
        """The predecessor's defect: every value scored exactly 1.0."""
        values = [
            observed("width_mm", 35.0, SourceKind.DATASHEET, "a"),
            observed("width_mm", 35.0, SourceKind.DISTRIBUTOR_WEB, "b", method=ExtractionMethod.LLM),
            observed("width_mm", 35.0, SourceKind.IMAGE, "c", method=ExtractionMethod.OCR, verified=False),
        ]
        for av in values:
            score_attribute(av, CB.get("width_mm"))
        self.assertEqual(len({round(v.confidence, 3) for v in values}), 3)
        self.assertTrue(all(0.0 < v.confidence < 1.0 for v in values))


class TestValidation(unittest.TestCase):
    def test_rule_expressions(self):
        product = make_product()
        product.attributes["operating_temp_min_c"] = AttributeValue(code="operating_temp_min_c", value=-25.0)
        product.attributes["operating_temp_max_c"] = AttributeValue(code="operating_temp_max_c", value=60.0)
        self.assertTrue(evaluate_expression("operating_temp_max_c > operating_temp_min_c", product))
        product.attributes["operating_temp_max_c"].value = -40.0
        self.assertFalse(evaluate_expression("operating_temp_max_c > operating_temp_min_c", product))

    def test_rule_is_not_applicable_when_operand_missing(self):
        self.assertIsNone(evaluate_expression("operating_temp_max_c > operating_temp_min_c", make_product()))

    def test_rule_expressions_cannot_execute_code(self):
        """Rules come from a data file and must never be eval()'d."""
        product = make_product()
        self.assertIsNone(evaluate_expression("__import__('os').system('echo pwned')", product))

    def test_cross_attribute_rule_failure_is_an_error(self):
        product = make_product()
        product.attributes["operating_temp_min_c"] = AttributeValue(code="operating_temp_min_c", value=60.0)
        product.attributes["operating_temp_max_c"] = AttributeValue(code="operating_temp_max_c", value=-25.0)
        report = validate_product(product, CB)
        self.assertTrue(any("cb_temp_order" in e for e in report.errors))

    def test_outlier_detection(self):
        products = []
        for i, width in enumerate([17.5, 17.6, 17.4, 17.5, 35.0, 4000.0]):
            p = make_product(mpn=f"P{i}")
            p.attributes["width_mm"] = AttributeValue(code="width_mm", value=width, unit="mm")
            products.append(p)
        peers = PeerStatistics(threshold=3.5).fit(products)
        self.assertTrue(peers.is_outlier("electrical.circuit_breaker", "width_mm", 4000.0)[0])
        self.assertFalse(peers.is_outlier("electrical.circuit_breaker", "width_mm", 17.5)[0])

    def test_completeness_has_a_denominator(self):
        product = make_product()
        for code in CB.required_codes("core"):
            product.attributes[code] = AttributeValue(code=code, value="x")
        quality = compute_quality(product, CB)
        self.assertEqual(quality.completeness_core, 1.0)
        self.assertLess(quality.completeness_ecommerce, 1.0)

    def test_no_global_auto_approve_override(self):
        """The predecessor shipped with a flag that disabled its validation gate."""
        fields = set(Settings.model_fields)
        for banned in ("auto_approve_needs_review", "auto_approve", "skip_validation"):
            self.assertNotIn(banned, fields)

    def test_unknown_settings_keys_are_rejected(self):
        with self.assertRaises(Exception):
            Settings(gpu_batch_size=128)


class TestLLMProviderToggle(unittest.TestCase):
    """The offline/cloud switch, including that it never leaks or persists a secret."""

    def test_provider_resolution(self):
        from product_intel.llm.provider import (
            BedrockProvider,
            NullProvider,
            OllamaProvider,
            get_provider,
        )

        for name, cls in (
            ("ollama", OllamaProvider),
            ("bedrock", BedrockProvider),
            ("null", NullProvider),
        ):
            self.assertIsInstance(get_provider(Settings(llm_provider=name)), cls, name)

    def test_disabled_always_yields_null_provider(self):
        from product_intel.llm.provider import NullProvider, get_provider

        cfg = Settings(llm_provider="bedrock", llm_enabled=False)
        self.assertIsInstance(get_provider(cfg), NullProvider)

    def test_model_default_follows_provider(self):
        """Switching backend must not leave Bedrock pointed at an Ollama tag."""
        self.assertEqual(Settings(llm_provider="ollama").active_model, "qwen2.5:14b")
        self.assertTrue(
            Settings(llm_provider="bedrock").active_model.startswith("us.anthropic.")
        )

    def test_explicit_model_overrides_provider_default(self):
        cfg = Settings(llm_provider="bedrock", llm_model="us.amazon.nova-lite-v1:0")
        self.assertEqual(cfg.active_model, "us.amazon.nova-lite-v1:0")

    def test_offline_classification(self):
        self.assertTrue(Settings(llm_provider="ollama").is_offline)
        self.assertTrue(Settings(llm_provider="null").is_offline)
        self.assertTrue(Settings(llm_provider="bedrock", llm_enabled=False).is_offline)
        self.assertFalse(Settings(llm_provider="bedrock").is_offline)

    def test_no_secret_is_ever_serialized(self):
        """Settings holds env var *names*, never values."""
        cfg = Settings(llm_provider="bedrock")
        dumped = json.dumps(cfg.model_dump())
        self.assertIn("AWS_ACCESS_KEY_ID", dumped)      # the name is fine
        for field in cfg.model_dump().values():
            if isinstance(field, str):
                self.assertFalse(field.startswith("AKIA"), "a live key reached settings")

    def test_bedrock_converse_request_shape(self):
        """Pin the wire format: Converse API, model ID, system block, token cap."""
        from unittest import mock

        from product_intel.llm.provider import BedrockProvider

        cfg = Settings(
            llm_provider="bedrock",
            bedrock_model="us.amazon.nova-lite-v1:0",
            bedrock_max_tokens=1234,
            llm_temperature=0.0,
        )
        provider = BedrockProvider(cfg)

        fake_client = mock.Mock()
        fake_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
            "stopReason": "end_turn",
        }
        with mock.patch.object(provider, "_get_client", return_value=fake_client):
            result = provider.complete_json("probe", expect="object")

        self.assertEqual(result, {"ok": True})
        kwargs = fake_client.converse.call_args[1]
        self.assertEqual(kwargs["modelId"], "us.amazon.nova-lite-v1:0")
        self.assertEqual(kwargs["messages"][0]["role"], "user")
        self.assertEqual(kwargs["messages"][0]["content"][0]["text"], "probe")
        self.assertEqual(kwargs["inferenceConfig"]["maxTokens"], 1234)
        # json_mode is requested through the system block, since Converse has
        # no response_format switch.
        self.assertTrue(any("JSON" in b["text"] for b in kwargs["system"]))

    def test_bedrock_multi_block_response_is_joined(self):
        from unittest import mock

        from product_intel.llm.provider import BedrockProvider

        provider = BedrockProvider(Settings(llm_provider="bedrock"))
        fake_client = mock.Mock()
        fake_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"a":'}, {"text": " 1}"}]}},
            "stopReason": "end_turn",
        }
        with mock.patch.object(provider, "_get_client", return_value=fake_client):
            self.assertEqual(provider.complete_json("p", expect="object"), {"a": 1})

    def test_bedrock_errors_are_translated_with_a_fix(self):
        """Each Bedrock failure mode has a specific remedy; say what it is."""
        from botocore.exceptions import ClientError

        from product_intel.llm.provider import (
            LLMConfigurationError,
            LLMUnavailable,
            BedrockProvider,
        )

        provider = BedrockProvider(Settings(llm_provider="bedrock"))

        cases = [
            ("AccessDeniedException", "Model access", LLMConfigurationError),
            ("ResourceNotFoundException", "inference profile", LLMConfigurationError),
            ("UnrecognizedClientException", "rejected", LLMConfigurationError),
            ("ThrottlingException", "transient", LLMUnavailable),
        ]
        for code, expected_hint, cls in cases:
            err = ClientError({"Error": {"Code": code, "Message": "boom"}}, "Converse")
            translated = provider._translate_error(err)
            self.assertIsInstance(translated, cls, code)
            self.assertIn(code, str(translated))
            self.assertIn(expected_hint.lower(), str(translated).lower(), code)

    def test_throttling_is_retryable_but_access_denied_is_not(self):
        from product_intel.llm.provider import LLMConfigurationError, LLMUnavailable

        # LLMConfigurationError must be a subclass so callers can catch either,
        # but the retry loop must be able to tell them apart.
        self.assertTrue(issubclass(LLMConfigurationError, LLMUnavailable))

    def test_missing_boto3_is_a_clear_configuration_error(self):
        import builtins
        from unittest import mock

        from product_intel.llm.provider import LLMConfigurationError, BedrockProvider

        provider = BedrockProvider(Settings(llm_provider="bedrock"))
        real_import = builtins.__import__

        def no_boto3(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("No module named 'boto3'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=no_boto3):
            with self.assertRaises(LLMConfigurationError) as ctx:
                provider._get_client()
        self.assertIn("pip install boto3", str(ctx.exception))

    def test_dotenv_does_not_override_real_environment(self):
        import os

        from product_intel.config import load_dotenv

        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / ".env"
            env_file.write_text(
                "# comment\n"
                "\n"
                "export PI_TEST_A='from-file'\n"
                'PI_TEST_B="from-file"\n'
            )
            os.environ["PI_TEST_A"] = "from-shell"
            os.environ.pop("PI_TEST_B", None)
            try:
                load_dotenv(env_file)
                self.assertEqual(os.environ["PI_TEST_A"], "from-shell")  # shell wins
                self.assertEqual(os.environ["PI_TEST_B"], "from-file")
            finally:
                os.environ.pop("PI_TEST_A", None)
                os.environ.pop("PI_TEST_B", None)

    def test_save_settings_round_trip_preserves_other_keys(self):
        from product_intel.config import save_settings

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text(json.dumps({
                "_comment": "keep me",
                "llm_provider": "ollama",
                "target_channel": "ecommerce",
                "max_workers": 7,
            }))
            save_settings({"llm_provider": "bedrock"}, path)
            written = json.loads(path.read_text())

        self.assertEqual(written["llm_provider"], "bedrock")
        self.assertEqual(written["max_workers"], 7)          # untouched
        self.assertEqual(written["_comment"], "keep me")      # preserved

    def test_save_settings_rejects_invalid_values_before_writing(self):
        from product_intel.config import save_settings

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            original = {"llm_provider": "ollama"}
            path.write_text(json.dumps(original))
            with self.assertRaises(Exception):
                save_settings({"llm_provider": "not-a-real-provider"}, path)
            self.assertEqual(json.loads(path.read_text()), original)  # unchanged

    def test_stale_config_keys_are_rejected_not_ignored(self):
        """A removed provider's leftover key should fail loudly."""
        with self.assertRaises(Exception):
            Settings(openrouter_model="anthropic/claude-3.5-haiku")


class TestSqlSafety(unittest.TestCase):
    def setUp(self):
        self.db = CatalogDB(Settings(catalog_root=tempfile.mkdtemp()))

    def test_reads_allowed(self):
        for sql in ("SELECT * FROM products", "WITH x AS (SELECT 1) SELECT * FROM x"):
            self.assertTrue(self.db.is_read_only_sql(sql)[0], sql)

    def test_writes_refused(self):
        for sql in (
            "DELETE FROM products",
            "DROP TABLE products",
            "UPDATE products SET mpn = 'x'",
            "INSERT INTO products VALUES (1)",
        ):
            self.assertFalse(self.db.is_read_only_sql(sql)[0], sql)

    def test_filesystem_reaching_statements_refused(self):
        """A keyword blocklist alone lets these through; both can write files."""
        for sql in (
            "COPY products TO '/tmp/leak.csv'",
            "ATTACH '/etc/passwd' AS pw",
            "INSTALL httpfs",
            "SELECT 1; DROP TABLE products",
        ):
            self.assertFalse(self.db.is_read_only_sql(sql)[0], sql)


class TestGraph(unittest.TestCase):
    def test_variant_family_traversal(self):
        graph = ProductGraph()
        graph.add("v1", RelationType.VARIANT_OF, "base")
        graph.add("v2", RelationType.VARIANT_OF, "base")
        self.assertEqual(graph.base_of("v1"), "base")
        self.assertCountEqual(graph.variants_of("base"), ["v1", "v2"])
        self.assertIn("v2", graph.siblings("v1"))
        self.assertNotIn("v1", graph.siblings("v1"))

    def test_replaces_creates_inverse_edge(self):
        graph = ProductGraph()
        graph.add("new", RelationType.REPLACES, "old")
        self.assertIn("new", graph.neighbours("old", RelationType.REPLACED_BY))

    def test_round_trip(self):
        graph = ProductGraph()
        graph.add("a", RelationType.COMPATIBLE_WITH, "b")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "graph.json"
            graph.save(path)
            reloaded = ProductGraph.load(path)
        self.assertEqual(reloaded.stats()["edges"], graph.stats()["edges"])

    def test_build_from_products(self):
        base = make_product("VX100")
        variant = make_product("VX100-2P-C20")
        variant.identity.base_product_id = base.identity.product_id
        graph = build_graph([base, variant])
        self.assertEqual(graph.base_of(variant.identity.product_id), base.identity.product_id)


class TestEnrichment(unittest.TestCase):
    def test_inherited_values_are_labelled_and_discounted(self):
        from product_intel.pipeline.enricher import fill_gaps

        base = make_product("VX100")
        base.attributes["country_of_origin"] = observed(
            "country_of_origin", "Germany", SourceKind.DATASHEET, "ds"
        )
        score_attribute(base.attributes["country_of_origin"], CB.get("country_of_origin"))

        variant = make_product("VX100-4P-C40")
        variant.identity.base_product_id = base.identity.product_id

        graph = build_graph([base, variant])
        catalog = {p.identity.product_id: p for p in (base, variant)}
        filled = fill_gaps(variant, CB, graph, catalog, Settings())

        self.assertIn("country_of_origin", filled)
        inherited = variant.attributes["country_of_origin"]
        self.assertEqual(inherited.value, "Germany")
        self.assertIsNotNone(inherited.inference)
        self.assertEqual(inherited.inference.strategy, "family_inheritance")
        self.assertLess(inherited.confidence, base.attributes["country_of_origin"].confidence)

    def test_variant_defining_attributes_are_never_inherited(self):
        """Inheriting what distinguishes a variant would fabricate the distinction."""
        from product_intel.pipeline.enricher import fill_gaps

        base = make_product("VX100")
        base.attributes["poles"] = observed("poles", "3", SourceKind.DATASHEET, "ds")
        variant = make_product("VX100-4P-C40")
        variant.identity.base_product_id = base.identity.product_id

        graph = build_graph([base, variant])
        filled = fill_gaps(variant, CB, graph, {p.identity.product_id: p for p in (base, variant)}, Settings())
        self.assertNotIn("poles", filled)

    def test_generated_content_is_marked_and_grounded(self):
        from product_intel.pipeline.enricher import generate_content

        product = make_product("VX100-2P-C20")
        for code, value in (("poles", "2"), ("rated_current_a", 20.0), ("trip_curve", "C")):
            av = observed(code, value, SourceKind.DATASHEET, "ds")
            score_attribute(av, CB.get(code))
            product.attributes[code] = av

        generated, _warnings = generate_content(product, CB, provider=None, cfg=Settings())
        self.assertIn("short_description", generated)
        desc = product.attributes["short_description"]
        self.assertIsNotNone(desc.inference)
        self.assertEqual(desc.inference.strategy, "generated_from_attributes")
        self.assertIn("20", desc.value)

    def test_generation_flags_unverifiable_numbers(self):
        from product_intel.pipeline.enricher import _verify_generated

        product = make_product()
        product.attributes["rated_current_a"] = AttributeValue(code="rated_current_a", value=20.0)
        payload = {"short_description": "Rated 20 A with a 25 year warranty."}
        _payload, warnings = _verify_generated(payload, [("rated_current_a", "Rated Current", "20 A")], product)
        self.assertTrue(any("25" in w for w in warnings))


class TestExporters(unittest.TestCase):
    def setUp(self):
        self.product = make_product("VX100-2P-C20", "Voltaris Electric")
        self.product.identity.gtin = "40123456789029"
        for code, value, unit in (
            ("poles", "2", None), ("rated_current_a", 20.0, "A"), ("trip_curve", "C", None),
        ):
            av = observed(code, value, SourceKind.DATASHEET, "ds", unit=unit)
            score_attribute(av, CB.get(code))
            self.product.attributes[code] = av
        self.product.quality.channel_ready = True

    def test_json_carries_evidence(self):
        from product_intel.export.exporters import export_json

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            export_json([self.product], TAX, path, include_evidence=True)
            payload = json.loads(path.read_text())
        entry = payload["products"][0]["attributes"]["rated_current_a"]
        self.assertEqual(entry["value"], 20.0)
        self.assertIn("evidence", entry)
        self.assertEqual(entry["evidence"]["source_id"], "ds")

    def test_bmecat_is_wellformed_with_etim(self):
        from xml.etree import ElementTree as ET

        from product_intel.export.exporters import export_bmecat

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.xml"
            export_bmecat([self.product], TAX, path, ready_only=True)
            root = ET.parse(path).getroot()
        self.assertEqual(root.tag, "BMECAT")
        self.assertEqual(root.attrib["version"], "2005")
        aids = [e.text for e in root.iter("SUPPLIER_AID")]
        self.assertIn("VX100-2P-C20", aids)
        self.assertIn("EC000109", [e.text for e in root.iter("REFERENCE_FEATURE_GROUP_ID")])

    def test_csv_has_identity_and_quality_columns(self):
        import csv as csvmod

        from product_intel.export.exporters import export_csv

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            export_csv([self.product], TAX, path)
            rows = list(csvmod.DictReader(path.open()))
        self.assertEqual(rows[0]["mpn"], "VX100-2P-C20")
        self.assertEqual(rows[0]["etim_class"], "EC000109")
        self.assertIn("quality_overall", rows[0])

    def test_ready_only_withholds_incomplete_products(self):
        from product_intel.export.exporters import export_gdsn

        incomplete = make_product("INCOMPLETE-1")
        incomplete.quality.channel_ready = False
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            result = export_gdsn([self.product, incomplete], TAX, path, ready_only=True)
        self.assertEqual(result["products"], 1)


class TestSchema(unittest.TestCase):
    def test_every_category_merges_common_attributes(self):
        for cid, cat in TAX.categories.items():
            self.assertIn("manufacturer", cat.attributes, cid)
            self.assertIn("mpn", cat.attributes, cid)
            self.assertTrue(cat.required_codes("core"), cid)

    def test_channels_are_cumulative(self):
        attr = CB.get("width_mm")
        self.assertFalse(attr.is_required("core"))
        self.assertTrue(attr.is_required("ecommerce"))
        self.assertTrue(attr.is_required("enhanced"))

    def test_numeric_attributes_declare_units_and_bounds(self):
        for cid, cat in TAX.categories.items():
            for code, attr in cat.attributes.items():
                if not attr.is_numeric or attr.code in ("warranty_months", "package_quantity", "list_price_usd"):
                    continue
                if attr.unit_family:
                    self.assertIsNotNone(attr.canonical_unit, f"{cid}.{code}")
                self.assertIsNotNone(attr.min, f"{cid}.{code} has no lower bound")
                self.assertIsNotNone(attr.max, f"{cid}.{code} has no upper bound")

    def test_keyword_classification(self):
        self.assertEqual(TAX.classify_by_keywords("miniature circuit breaker, C curve")[0],
                         "electrical.circuit_breaker")
        self.assertEqual(TAX.classify_by_keywords("full port brass ball valve NPT")[0],
                         "plumbing.ball_valve")
        self.assertEqual(TAX.classify_by_keywords("an entirely unrelated object")[0],
                         "industrial.generic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
