# Product Requirements Document
## Product Intelligence Engine for Industrial Commerce
**Version 1.0** · Unilog challenge

---

## 1. Vision

Industrial manufacturers and distributors hold thousands to millions of complex
SKUs whose information is scattered across datasheets, catalogs, websites, price
files and ERP extracts. Turning that into accurate, structured, commerce-ready
product data is slow, manual and error-prone — and it is the thing standing
between a distributor and a working B2B storefront.

This project builds the engine that does it. Not a chatbot over documents: a
**catalog**, in which every product record is assembled from every source that
mentions it, normalized to one vocabulary, validated against a declared schema,
scored, and exportable to the formats a PIM or storefront actually ingests.

**The governing principle: no fact without provenance.** Every attribute value
carries the source document, the location inside it and the verbatim text that
supports it — or, if it was inferred rather than read, an explicit inference
path. There is no third state, and the data model has no field for one.

Where a technical convenience and this principle conflict, this principle wins.

---

## 2. Why this is hard

The difficulty is not "read a PDF". It is:

- **The same product appears in five documents under four spellings.** A
  datasheet, a submittal, a web page, a price file and an ERP extract each name
  it differently. Without identity resolution there is no golden record.
- **The same attribute is named six ways.** `Operating Temp`, `Operating
  Temperature Range`, `Temp. (°C)`, `Ambient`. Free-form extraction produces six
  attributes where there should be one.
- **The same value is stated in four unit systems.** `1/2"`, `DN15`, `150 PSI`,
  `10.3 bar`, `1200 CFM`, `3/4 HP`. A catalog that mixes them cannot be filtered.
- **Variant tables are traps.** A selection table lists five SKUs across seven
  columns. Reading the wrong row silently attaches one variant's rating to
  another — the single most common industrial-datasheet extraction failure.
- **Sources disagree.** The datasheet says 10 kA, the web page says 6 kA. Both
  must be retained and the choice must be explainable.
- **The information is genuinely incomplete.** A newly released SKU may have
  nothing but a one-page submittal. What it shares with its family has to be
  recovered from the family, and clearly marked as inherited.
- **Nobody can act on an unexplained number.** A product record that cannot be
  traced to a source cannot be signed off by a product manager.

---

## 3. Guiding principles

1. **No fact without provenance.** Evidence or a declared inference. Never neither.
2. **An honest gap beats a confident wrong value.** Where the system cannot
   determine something, it reports the gap.
3. **Deterministic before probabilistic.** Rules and structure first; the model
   fills what they could not, and only that.
4. **Nothing is discarded.** Losing values in a conflict, raw surface forms, and
   the original unit are all retained.
5. **Inference is always labelled.** An inherited or generated value is never
   presented as though it were read from the product's own datasheet.
6. **The catalog outlives the AI.** Product JSON and source mirrors are durable
   and human-readable; the database, graph and vector index are derived and
   disposable.
7. **Validation cannot be switched off.** There is no global approve-anyway flag.
8. **Growth without disruption.** Ingesting one source never rewrites another
   product's data.

---

## 4. Scope

**In scope.** PDF datasheets, submittals and catalogs (digital and scanned);
manufacturer and distributor HTML product pages; CSV/XLSX price files and ERP
extracts; product images. Electrical, plumbing/PVF, HVAC and general industrial
verticals. Attribute extraction, normalization, identity resolution, golden
record assembly, enrichment, content generation, validation, quality scoring,
human review, and export.

**Out of scope for v1.** Customer-specific pricing and contract logic; real-time
ERP synchronisation; multi-tenant authentication; storefront rendering;
translation and market localization beyond the generation hooks.

---

## 5. Architecture

Four layers. Each consumes only the artifacts of the one above it, so any layer
can be replaced without touching the others.

### L1 — Ingest and perceive
Source files become **provenance-tagged Fragments** plus a canonical Markdown
mirror. Tables retain their shape as row/column structures, because flattening
a variant matrix into prose is what destroys the ability to know which column a
value came from. Pages with no text layer are routed to OCR; when no engine is
available the gap is recorded in the audit log rather than the page silently
vanishing.

### L2 — Resolve and structure
- **Identity resolution.** Deterministic product IDs from normalized
  manufacturer + part number. Variant/family relationships derived from series
  declarations and part-number structure. Near-duplicates (OCR misreads) are
  flagged for a human, never merged automatically.
- **Classification.** Keyword scoring against the taxonomy first; a model only
  breaks ties. Classification is sticky once confident.
- **Schema-directed extraction.** The extractor is told exactly which attributes
  to find, with what datatype, unit family and legal values. Tier 1 resolves
  spec tables and key/value blocks deterministically; Tier 2 sends only the
  remaining attributes, over only the plausible fragments, to a model that must
  return a verbatim quote. Unverifiable quotes are discarded.
- **Normalization.** Units to one canonical scale, enum surface forms to a
  controlled vocabulary. Raw forms retained.
- **Golden record.** Multi-source arbitration by precedence, then extraction
  method, then confidence. Near-equal numbers corroborate rather than conflict.
  Losers and the deciding rule are retained.

### L3 — Enrich and generate
- **Knowledge graph.** `variant_of`, `compatible_with`, `certified_by`,
  `belongs_to`, `documented_in`, `replaces`/`replaced_by`. Edges carry provenance.
- **Gap filling.** Missing attributes recovered from the family record or from
  unanimous sibling consensus, tagged `inferred` with the inheritance path and
  discounted in confidence. Variant-defining attributes are never inherited.
- **Content generation.** Descriptions, feature bullets, SEO metadata and search
  keywords, authored only from attributes that themselves carry evidence.
  Generated numeric claims are checked against the source facts and flagged when
  they do not correspond.

### L4 — Validate and govern
- **Confidence**, from six independent factors, replacing any hardcoded score.
- **Validation**: schema conformance, cross-attribute rules from the data file,
  peer-group outlier detection using median absolute deviation, and
  re-verification of every quote against the mirror on disk.
- **Quality scorecard** on four axes: completeness per channel, accuracy,
  consistency, distinctiveness — reported before and after enrichment.
- **Review queue**, prioritized by reason weight × severity × (1 − confidence).
- **Learning loop**: corrections are promoted to enum synonyms and manufacturer
  defaults applied to every subsequent ingest.
- **Export**: JSON with full evidence, channel CSV, BMEcat 2005 with ETIM, GDSN.

---

## 6. The attribute dictionary

The contract that makes extraction directed rather than open-ended. Per
attribute: canonical code and name, datatype, unit family and canonical unit,
allowed values, plausibility bounds, cardinality, identity and variant-defining
flags, the surface aliases found in real documents, and which sales channel
requires it.

Categories carry **ETIM** and **UNSPSC** class codes so exports are
standards-compliant. Adding a vertical is a data change, not a code change.

Channels are cumulative: `core` (PIM minimum) ⊂ `ecommerce` (publishable) ⊂
`enhanced` (drives conversion). Completeness is measured against each.

---

## 7. Functional requirements

| # | Requirement |
|---|---|
| F1 | Ingest PDF, HTML, CSV, XLSX and image sources from a directory tree. |
| F2 | Skip sources whose checksum is unchanged; resume a partially processed source from its last completed step. |
| F3 | Resolve every mention of a product across sources to one product record. |
| F4 | Classify each product into a category carrying ETIM and UNSPSC codes. |
| F5 | Extract only attributes declared for that category, with datatype, unit and legal-value conformance. |
| F6 | Read variant tables by matching the key column to the product's own part number; extract nothing when no row matches. |
| F7 | Normalize every value to the attribute's canonical unit and controlled vocabulary, retaining the raw form. |
| F8 | Arbitrate multi-source disagreement by declared precedence and retain the losing values with the deciding rule. |
| F9 | Attach to every value either an Evidence pointer or an InferencePath. |
| F10 | Re-verify every quote against the canonical mirror and mark unverifiable ones. |
| F11 | Compute a per-value confidence from observable signals; never assign a constant. |
| F12 | Fill gaps from the product family, labelled as inferred and confidence-discounted. |
| F13 | Generate commerce copy grounded in evidenced attributes, flagging unverifiable claims. |
| F14 | Validate against schema, cross-attribute rules and peer distributions. |
| F15 | Score quality on four axes per product and across the catalog, before and after enrichment. |
| F16 | Route failures and low-confidence values to a prioritized human review queue. |
| F17 | Apply human corrections at the highest precedence and promote them to reusable rules. |
| F18 | Export JSON, CSV, BMEcat 2005 + ETIM, and GDSN, optionally restricted to channel-ready products. |
| F19 | Answer attribute queries via parameterised SQL and descriptive queries via semantic search, always with citations. |
| F20 | Analyze product images for shot type and channel compliance, and generate alt text. |

---

## 8. Non-functional requirements

- **Offline-capable.** Default configuration makes no external network call.
  The LLM provider is pluggable; the whole pipeline runs with none configured.
- **Graceful degradation.** Missing model, missing OCR engine or missing
  embedding library reduces coverage visibly in the scorecard; it never crashes
  and never fabricates.
- **Deterministic.** Same inputs and same versions produce the same product IDs
  and the same derived artifacts, independent of worker count.
- **Incremental.** Re-running over an unchanged corpus performs no work.
- **Auditable.** Per-source, per-step audit logs; per-query plans; every
  arbitration explained.
- **Safe by construction.** Rule expressions are parsed, not evaluated. SQL is
  allowlisted by leading keyword and denied filesystem-reaching verbs. Config
  rejects unknown keys.
- **Typed.** Pydantic models for all structured data.

---

## 9. Quality model

| Axis | Definition |
|---|---|
| **Completeness** | Filled required attributes ÷ required attributes, per channel. Family records are exempt from variant-defining requirements. |
| **Accuracy** | Of values claiming to be read from a source, the share whose quote is still locatable in the mirror. Declared inferences are excluded and counted separately. |
| **Consistency** | Share of applicable schema and cross-attribute rules passed, less a penalty for schema errors. |
| **Distinctiveness** | Inverse of the peer-group outlier rate among numeric attributes. |

Overall = 0.40 × completeness(target channel) + 0.25 × accuracy + 0.25 ×
consistency + 0.10 × distinctiveness.

Confidence is reported separately for sourced and generated attributes, because
blending them hides the number that matters.

---

## 10. Deliverables

Engine and four-layer pipeline; attribute dictionary and taxonomy for four
verticals; CLI; FastAPI service + web console; exporters; synthetic sample catalog
generator; automated tests; documentation.

Test coverage explicitly includes: unit conversion correctness including
offset-based temperature and the fraction-vs-dual-rating distinction; variant
matrix row selection; rejection of unverifiable quotes; arbitration outcomes and
loser retention; that inherited and generated values are distinguishable from
observed ones; that the validation gate cannot be disabled; and that SQL
execution refuses statements that can write.

---

## 11. Future work

Vision-language extraction from dimension drawings and spec-table images;
BMEcat/GDSN **import** as well as export; per-customer pricing and contract
logic; market localization; distributed ingestion for multi-million-SKU
catalogs; direct ERP and PIM connectors; active learning that retrains
extraction from accumulated corrections.
