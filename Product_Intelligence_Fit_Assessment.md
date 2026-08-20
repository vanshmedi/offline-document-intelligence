# Offline Document Intelligence → Product Intelligence
## Project Assessment, Fit Analysis & Roadmap to a Winning Solution

**Reviewed:** `offline-document-intelligence` (main @ `e9a3b9f`)
**Date:** 20 August 2026
**Against:** "AI-powered product intelligence for industrial manufacturers" problem statement

---

# Part 1 — What the project actually is

## 1.1 One-line summary

An **offline-first, evidence-preserving document intelligence pipeline** that turns heterogeneous government budget PDFs into a permanent, human-readable, machine-queryable knowledge repository — where every extracted number can be traced back to a specific document, page, and verbatim quote.

It is deliberately *not* built as a chatbot. The PRD is explicit: **the repository is the product; the AI is one interface over it.** If every model disappeared, the Markdown mirrors and original PDFs would still be a usable research asset.

## 1.2 Architecture

```
                     ┌─────────────────────────────────────────┐
  PDF (any lang)  →  │  INGEST PIPELINE (7 pluggable steps)    │
                     └─────────────────────────────────────────┘
                                      │
  checksum → parser → translator → extractor → db_ingest → embedder → vector_store
     │          │          │           │            │           │          │
     │          │          │           │            │           │          └─ JSON + numpy cosine, per country
     │          │          │           │            │           └───────────── BGE-m3 (sentence-transformers)
     │          │          │           │            └───────────────────────── DuckDB `metrics` table
     │          │          │           └────────────────────────────────────── local LLM → Pydantic FinancialMetric
     │          │          └────────────────────────────────────────────────── Ollama translategemma (off by default)
     │          └───────────────────────────────────────────────────────────── pdfplumber/PyMuPDF → document.md + tables.md
     └──────────────────────────────────────────────────────────────────────── MD5 → deterministic doc_id, dedup

                                      ▼
                          ┌───────────────────────┐
                          │   VALIDATION GATE     │  quote traceability, page-count parity,
                          └───────────────────────┘  required metadata → complete | needs_review | failed
                                      ▼
   DataBank/<Country>/<Category>/<doc_id>/{original.pdf, document.md, tables.md,
                                           metadata.json, metrics.json, audit.json}
   DataBank/manifest.json   DataBank/metrics.db   DataBank/<Country>/vector_store.json
                                      ▼
                          ┌───────────────────────┐
                          │    QUERY LAYER        │
                          └───────────────────────┘
        router (LLM classify + heuristic fallback)
             ├─ quantitative → LLM-generated DuckDB SQL → regex "SQL healing" → execute
             ├─ qualitative  → vector search → page-level consolidation
             └─ hybrid       → both, merged
                    → evidence-only prompt → answer + citations (doc, page, quote)
                    → every query logged to history.json
                                      ▼
                    CLI (click) · Streamlit UI (dark-glass chat)
```

## 1.3 Tech stack

| Layer | Technology |
|---|---|
| Language / typing | Python 3.11+, Pydantic v2 (all structured data is a model) |
| PDF parsing | pdfplumber (text, `extract_tables`, `find_tables` + bbox cropping), PyMuPDF |
| LLM serving | Ollama, local — `qwen2.5:14b` (extraction, SQL, routing), `translategemma:27b` (translation) |
| Embeddings | `BAAI/bge-m3` via sentence-transformers (CPU or CUDA torch) |
| Vector search | Hand-rolled: JSON store + numpy cosine, one file per country |
| Structured analytics | DuckDB (`metrics.db`, single wide table) |
| State | `manifest.json` (per-document, per-step status ledger) |
| CLI | click — `ingest`, `batch-ingest`, `batch-ingest-databank`, `query`, `chat`, `status`, `rebuild-db`, `clear`, `clear-history` |
| UI | Streamlit |
| Tests | unittest — checksum, router heuristics, validation gate (pass + fail), extractor happy-path + malformed-LLM-output |
| Network | **Zero external calls.** Fully air-gapped by design. |

## 1.4 The genuinely good engineering in here

These are the parts worth protecting in any pivot — they are the hard, unglamorous things most teams skip:

1. **Provenance is structural, not decorative.** `FinancialMetric` *requires* `page_reference` and `context_quote`. A figure literally cannot exist in the model without an evidence pointer.
2. **The validation gate enforces traceability.** `validation.py` re-opens `document.md`, locates the cited page block by its `<!-- PAGE_START: n -->` marker, and confirms the quote is a normalized substring (with an 85 % sliding-window fallback). Failures route the document to `needs_review` and out of retrieval.
3. **Quote alignment + value self-healing.** The extractor doesn't trust the LLM's quote: `_align_and_verify_quote` snaps it to a real line on the page by digit and token overlap. `_repair_json` rebuilds truncated/unbalanced JSON from small local models. This is exactly the defensive plumbing local-model pipelines need.
4. **Number healing in the parser.** `_heal_split_numbers` repairs PDF layout artifacts (`2 15,719` → `215,719`, `8 ,927` → `8,927`) before anything downstream sees them.
5. **Truly incremental ETL.** Deterministic checksum-derived IDs, per-step status in the manifest, `PipelineStep.execute` skips already-completed steps on re-run, DuckDB and the vector store both delete-then-insert by `document_id`. Re-ingesting one document cannot corrupt another.
6. **Interface-agnostic knowledge layer.** Browse the filesystem, run SQL, or chat — all three read the same unmodified artifacts. No interface-specific transforms.
7. **Honest "I don't know."** Evidence-only prompting, a sentinel string check, and a structured fallback that shows raw cited excerpts rather than inventing prose.
8. **Per-query audit trail.** `history.json` stores routing decision, raw SQL, healed SQL, SQL error, semantic hits with scores, the answer, and citations — for every single query.

## 1.5 What it solves today

Government/public-finance research: collapsing days of hunting through budget books, finance bills, demands for grants and think-tank PDFs into one searchable, citation-backed, offline repository — scoped per country, with quantitative questions answered by SQL over extracted metrics and qualitative ones by semantic retrieval.

---

# Part 2 — Fit against the problem statement

## 2.1 The core judgement

> **The domain is wrong. The machine is largely right.**

Swap "budget document" for "product datasheet" and "financial metric" for "product attribute" and roughly 60 % of this codebase is directly reusable. The problem statement's hardest requirement — *"validate and enrich information with **traceable outputs**"* — is the thing this project is already best at, and the thing most competing submissions will fake with a JSON blob and no evidence trail.

What's missing is not plumbing. It's the **product abstraction**: there is no notion of a product, a SKU, a category schema, an attribute dictionary, or a golden record. Today the unit of knowledge is *the document*. For product intelligence, the unit of knowledge must be *the product*, assembled from many documents.

## 2.2 Scorecard against the four expected outcomes

| Expected outcome | Status | Score | Why |
|---|---|---|---|
| **Generate structured product intelligence from limited inputs** | Partial | **35 %** | LLM→Pydantic structured extraction works and is battle-hardened. But: no target product schema, no attribute dictionary, no taxonomy, and — critically — **no generation of new content**. The system extracts what's written; it never *authors* a commerce-ready record. "From limited inputs" (infer from a family/series document, a sibling SKU, an image) is entirely absent. |
| **Improve product data quality and consistency** | Partial | **30 %** | Has a validation gate, number healing, quote alignment. Has *no* attribute normalization (units → SI, enums → canonical), no deduplication, no golden record, no conflict arbitration, and no measurable quality score. Consistency of the output is visibly weak in its own data (see 2.4). And `auto_approve_needs_review: true` in the shipped config **disables the validation gate entirely**. |
| **Validate and enrich information with traceable outputs** | **Strong** | **70 %** | The standout area. Page + verbatim-quote provenance on every value, an audit log per document, a per-query history ledger, evidence-only generation, citation-bearing answers, a `needs_review` state. Missing: *real* confidence scoring (currently hardcoded), conflict resolution across sources, a human review workflow, and "enrich" in the PIM sense (adding what isn't there). |
| **Scale efficiently across large catalogs** | Partial | **40 %** | The *design* scales beautifully — idempotent, resumable, incremental, deterministic, no cross-document writes. The *runtime* does not: brute-force full-scan vector search over a single in-memory JSON file, serial page-by-page LLM calls, no parallelism, no batching, no caching, no ANN index. The audit log for the one sample document shows the extractor step taking **76 minutes** with 7 pages timing out. |

**Overall: ~40–45 % of a winning solution — but it is the 40 % that is hardest to fake.** Provenance, auditability and incremental correctness are architectural decisions that competitors bolting on citations at the end cannot retrofit in a hackathon.

## 2.3 Capability map: transfers directly vs. must be built

**Transfers directly (keep, rename, repoint):**

| Current | Becomes |
|---|---|
| `DataBank/<Country>/<Category>/<doc_id>/` | `Catalog/<Manufacturer>/<Category>/<product_id>/` |
| `FinancialMetric` (name, value, unit, page_ref, quote) | `ProductAttribute` (name, value, unit, page_ref, quote, source, confidence) |
| `metrics.db` DuckDB | `attributes.db` — same shape, plus a `products` and `sources` table |
| Manifest + per-step status | Unchanged. This is the catalog-scale ingestion engine. |
| Validation gate | Unchanged in spirit; new rules (schema conformance, unit sanity, peer outliers) |
| Router + dual SQL/semantic retrieval | Attribute lookup (SQL) vs. descriptive/application search (vector) — same split, same value |
| Evidence-only prompting + citations | Grounded copy generation with per-sentence source links |
| `audit.json` + `history.json` | Explainability layer — already done |

**Must be built (the gap):** product schema & taxonomy, entity resolution & golden record, knowledge graph, multimodal/VLM parsing, OCR, web & non-PDF ingestion, content *generation*, quality scoring, human-in-the-loop review, real confidence, ANN-scale retrieval, commerce export formats, agentic orchestration.

## 2.4 Evidence of the quality gap, from the project's own artifacts

These are not hypotheticals — they're visible in the committed sample data:

- **Column-selection failure.** The extractor prompt explicitly demands *"the MOST RECENT budget estimate column (the rightmost year column)"*. In `metrics.json`, **20 of 24 metrics** are tagged `2025-26` and only 4 as `FY2026-27`. The model is reading the wrong column and the pipeline has no way to detect it. In product terms: reading the wrong variant's spec off a multi-column table — the single most common industrial-datasheet extraction failure.
- **Unit inconsistency.** 23 metrics `"Millions"`, 1 metric `"Billion"` (singular, different scale) — from one document. There is no unit dictionary and no normalization.
- **Attribute-name chaos.** `metric_name` values are raw row labels: `"B03 a)Levies and Fees"`, `"B 1 Tax Revenue Receipts"`. In a product context this is `"Operating Temp"` vs `"Operating Temperature Range"` vs `"Temp. (°C)"` — three attributes where there should be one.
- **Fake confidence.** Every metric carries `confidence_score: 1.0`, hardcoded at `extractor.py:233`. `settings.confidence_threshold: 0.7` is never consulted. Nothing in the system computes a confidence.
- **SQL grounding failure.** In `chat_test_results.txt`, query 1 generated `... AND category LIKE '%current expenditure%'` — hallucinating a value into the `category` column, returning zero rows and silently falling back to semantic search. The LLM is given sample `metric_name` and `fiscal_year` values but no schema constraints on other columns.
- **Scanned pages are invisible.** The parser writes `"OCR Warning: Page N contains no selectable text"` — and both the extractor and the embedder then skip any page containing that string. The audit log shows pages 2, 4 and 6 of the sample document silently dropped from the entire knowledge layer. **No OCR is implemented** despite the PRD specifying Surya OCR and listing scanned PDFs as in-scope.
- **Self-healing can silently corrupt.** `_correct_extracted_value` replaces the LLM's number with the nearest number *on the same line* within edit distance ≤ 3. On a multi-column budget row this can swap one year's figure for another's and log it as a successful "heal". It optimises for *a number that exists* rather than *the right number* — and it will do the same across variant columns in a product spec table.

## 2.5 Bugs and hygiene issues to fix before any demo

| Severity | Issue | Location |
|---|---|---|
| **Blocker** | `settings.general_country_name` is referenced but **not defined on the `Settings` model** → `batch-ingest-databank` (the exact command the runbook instructs you to run) throws `AttributeError` on line 1. | `cli.py:474` vs `config.py:11-21` |
| High | `auto_approve_needs_review: true` means the validation gate never blocks anything. The flagship correctness feature is switched off in the shipped config. | `settings.json` |
| High | GPU support documented but not implemented. `gpu_batch_size` is not a `Settings` field and is never read; there is **no `cuda`/`device` reference anywhere in the codebase**. The runbook's `[embedder] GPU detected... Using CUDA` output cannot occur. | `gpu_runbook.md` vs `embedder.py` |
| High | SQL safety is a substring keyword blocklist on a read-write connection. DuckDB `COPY ... TO`, `ATTACH`, `CREATE` and `INSTALL` all pass through. `_generate_sql` also string-interpolates `country` directly into SQL. | `chat.py:169-195, 100-118` |
| Medium | Verified-ID filtering happens *after* top-k retrieval, so unverified documents can crowd out valid results before filtering. | `chat.py:88-92` |
| Medium | Documents are written to `<Country_With_Underscores>/` but the vector store is written to and read from `<Country With Spaces>/` — two sibling directories for the same country. | `cli.py:83-87` vs `vector_store.py:11` |
| Medium | The `General` cross-country scope described in the runbook is never queried — `_semantic_search` only opens `VectorStore(country)`. | `chat.py:87` |
| Low | `batch_ingest` success counting is decorative (skips count as successes; the code comment concedes the confusion). | `cli.py:424-442` |
| Low | MD5 for checksums. SHA-256 costs nothing and reads better in an auditability story. | `checksum.py`, `cli.py` |
| Low | `VectorStore.rebuild_all_indexes` is a `pass` stub, contradicting the PRD's "derived artifacts may be deleted and regenerated at any time". | `vector_store.py:93-100` |
| Low | PRD-specified components never implemented: OCR (Surya), language detection (Lingua), FastAPI service layer. | — |

---

# Part 3 — What to add to make it a winning solution

## 3.1 Target architecture: an Evidence-Grounded Product Intelligence Fabric

Keep the existing repository philosophy. Add a product layer on top of it.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  L1 · INGEST & PERCEIVE                                                      │
│  PDF · HTML/web · XLSX/CSV · DOCX · images · BMEcat/GDSN XML · PIM exports   │
│  → layout-aware parse → OCR fallback → VLM for tables-in-images, diagrams,    │
│    dimension drawings → canonical Markdown mirror + extracted asset registry  │
│  Every fragment tagged: source_id, page, bbox, extraction_method             │
└──────────────────────────────────────────────────────────────────────────────┘
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  L2 · RESOLVE & STRUCTURE                                                    │
│  Product identity resolution (MPN/GTIN normalization, variant vs. base)      │
│  Category classification → ETIM / eCl@ss / UNSPSC                            │
│  Schema-conformant attribute extraction against the category's               │
│    ATTRIBUTE DICTIONARY (datatype, unit, allowed values, cardinality)        │
│  Unit & enum normalization → canonical SI / controlled vocabulary            │
│  GOLDEN RECORD assembly: multi-source merge with precedence + conflict log   │
└──────────────────────────────────────────────────────────────────────────────┘
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  L3 · ENRICH & GENERATE                                                      │
│  Gap-filling agents (family/series RAG, sibling SKU inference, image reading)│
│  Marketing copy · SEO title/meta/keywords · feature bullets · applications   │
│  Comparison tables · search synonyms · market localization                   │
│  Image asset intelligence: shot-type classification, compliance, alt-text    │
│  ▸ Every generated field carries a citation OR an explicit `inferred` class  │
└──────────────────────────────────────────────────────────────────────────────┘
                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  L4 · VALIDATE & GOVERN                                                      │
│  Schema validation · rule engine · peer-group outlier detection              │
│  Real confidence scoring → routes low-confidence to HUMAN REVIEW QUEUE       │
│  Quality scorecard (completeness / accuracy / consistency, before → after)   │
│  Export: normalized JSON · BMEcat · GDSN · Akeneo/Salsify/Shopify CSV · API  │
└──────────────────────────────────────────────────────────────────────────────┘

  CROSS-CUTTING:  Provenance Ledger  ·  Product Knowledge Graph  ·  Manifest /
                  incremental engine  ·  Audit trail   ← all four already exist
                                                          in embryonic form
```

## 3.2 The twelve additions, in priority order

### A. Product schema & attribute dictionary — *the single biggest gap*

Introduce a `ProductSchema` layer: a category taxonomy, and per category an attribute dictionary defining each attribute's canonical name, datatype, unit family, allowed values, cardinality, and whether it's required for a given sales channel.

Extraction then becomes **schema-directed** — instead of "find any figures on this page," it becomes "find *these 18 attributes* for *this product*, and return `null` with a reason if absent." This alone fixes attribute-name chaos, gives you a completeness denominator, and makes the output diffable.

> **Credibility move:** map to real industrial standards — **ETIM**, **eCl@ss**, **UNSPSC**, **GS1/GDSN**. Naming these tells judges from a manufacturing background that you understand their world. Ship one or two ETIM classes fully modelled rather than a generic schema.

### B. Entity resolution & the golden record

A product appears in a datasheet, a catalog page, a website table, and a price list. You need:

- **Identity resolution** — MPN/GTIN normalization, fuzzy matching, variant-vs-base-product disambiguation (`ABC-100-24V` and `ABC-100-12V` are variants of `ABC-100`).
- **Conflict arbitration** — when the datasheet says 24 V and the website says 12 V, resolve by source precedence (official datasheet > catalog > website > distributor), record *both* values with sources, and flag the conflict.
- **Golden record** — one canonical product record whose every field points back at the source that won, plus the losers.

The PRD's principle *"documents never lose identity"* needs its dual: **products have one identity across documents.**

### C. Knowledge graph

The problem statement names knowledge graphs explicitly, and product intelligence is inherently relational:

```
Product ──variant_of──▶ BaseProduct ──belongs_to──▶ Category ──requires──▶ AttributeSet
   │                                                     │
   ├──compatible_with──▶ Product        ├──certified_by──▶ Standard (UL, CE, ATEX, IP)
   ├──accessory_for────▶ Product        ├──documented_in─▶ Document ──▶ Page ──▶ Quote
   ├──spare_part_of────▶ Product        └──used_in───────▶ Application / Industry
   └──replaced_by──────▶ Product
```

This unlocks the demos that actually impress: *"show me every product that replaces the discontinued X-200 and is ATEX-certified"*, cross-sell/accessory suggestion, and — most importantly — **gap filling by traversal**: if a variant is missing an attribute, inherit it from the base product *and record the inheritance as its provenance*. Keep provenance on edges too. NetworkX in-memory or Kùzu/Neo4j if you want persistence; either is a weekend.

### D. Multimodal: OCR + vision-language models

The problem statement names VLMs and digital assets. This project is text-only and silently discards scanned pages.

- **OCR fallback** (Surya / PaddleOCR / docTR) for pages with no text layer — the PRD already promised this.
- **A local VLM** (Qwen2.5-VL, InternVL) for: spec tables that are images, dimension drawings, pin-out and wiring diagrams, and reading values directly off product photos.
- **Layout-aware parsing** (Marker / Docling / Unstructured) to preserve the table structure that pdfplumber flattens — this is the direct fix for the wrong-column failure in 2.4.
- **Asset intelligence**: classify product images (hero / angle / lifestyle / line-drawing / exploded view), check resolution, aspect ratio and background against channel requirements, and generate alt-text. Unglamorous, real PIM work that demos well.

### E. Content generation — the missing headline

The system extracts; it never *creates*. "Generate structured product intelligence" and "enrichment" require authoring:

short & long descriptions · feature bullets · SEO title, meta description, keywords · application/use-case narratives · comparison tables against named competitors · search synonyms and alternate part numbers · localized copy per market.

The differentiator: generate it **grounded**, using the citation machinery already built. Every generated sentence links to the source claim it derives from, and anything not derivable is marked `inferred` rather than presented as fact. That is a demo moment: hover a marketing sentence → see the datasheet line it came from.

### F. Data quality scoring engine

"Improve data quality" needs a number. Build a scorecard with four axes:

- **Completeness** — filled required attributes ÷ required attributes for this category and channel.
- **Accuracy** — share of attributes with verified quote-level provenance (you already compute the alignment; just surface it).
- **Consistency** — unit sanity, cross-attribute rules (`IP68 ⇒ ingress rating present`), enum conformance.
- **Distinctiveness** — peer-group outlier detection (a 4000 mm bolt in a category whose p99 is 200 mm).

Then show **before → after** across the whole catalog. A single chart reading *"completeness 34 % → 87 %, 12 400 SKUs, 41 minutes"* wins more points than any architecture slide.

### G. Human-in-the-loop workflow

Explicitly requested, and currently just a status string. Build:

a review queue sorted by confidence × business value · side-by-side source-document view with the cited region highlighted · accept / reject / edit with keyboard-driven bulk actions · reviewer identity and timestamp in the audit trail · **and a learning loop** — corrections become few-shot examples, per-manufacturer extraction templates, or promoted normalization rules, so the same mistake isn't made on the next 500 SKUs.

That last part is what turns "human-in-the-loop" from a checkbox into a scalability argument.

### H. Real confidence scoring

Replace the hardcoded `1.0` with a composite of signals you're already close to having:

quote-match strength from `_align_and_verify_quote` · agreement across two extraction passes or two models (self-consistency) · schema-validation outcome (datatype, range, enum) · source authority · corroboration count across independent sources · extraction method (native text > OCR > VLM inference).

Confidence then drives auto-publish vs. review-queue routing — which is the actual mechanism by which the system scales without a human touching everything.

### I. Agentic orchestration

Replace the fixed linear pipeline with agents where it earns its keep:

- **Attribute Researcher** — for a missing attribute, decides *where* to look: spec table, body text, an image, the family datasheet, a sibling SKU.
- **Verifier** — independently re-reads the source to confirm a value; disagreement drops confidence rather than being silently overwritten (this is the principled replacement for `_correct_extracted_value`).
- **Conflict Arbiter** — resolves multi-source disagreement and writes the reasoning to the audit log.
- **Gap Filler** — traverses the knowledge graph to infer from family/base products, always tagging the result `inferred` with the inference path.

### J. Ingestion breadth

Today: `rglob("*.pdf")`. Needed: HTML/product-page scraping, XLSX/CSV spec and price files, DOCX, image folders, and existing PIM/ERP exports (BMEcat, GDSN XML). The `PipelineStep` ABC and the parser interface are already clean enough that this is mostly new step classes — a cheap, highly visible win.

### K. Scale engineering

| Problem today | Fix |
|---|---|
| Full-scan cosine over one in-memory JSON per country | ANN index — **LanceDB**, **hnswlib**, FAISS or Qdrant; embedded, no server needed |
| Serial page-by-page LLM calls (76 min/doc observed) | Async + batched inference; **vLLM** for throughput; batch pages per request |
| No embedding cache | Cache by `sha256(chunk_text) + model_version` — re-ingestion becomes near-free |
| Single wide DuckDB table, no indexes | Index `(product_id, attribute_name)`; partition by category; delta-merge instead of delete-then-insert |
| No parallelism | Process-pool across documents; the no-cross-document-writes invariant already makes this safe |
| GPU claimed but absent | Actually implement device selection and `batch_size` in `embedder.py` |

Then **publish throughput numbers**: SKUs/hour, cost per 1 000 SKUs, and a projection to 100 000 SKUs. "Scale efficiently" is a claim that must be measured to count.

### L. Commerce-ready output & interoperability

"Commerce-ready" means it must leave the system in a format someone can load. Add exporters — normalized product JSON, **BMEcat** (with ETIM), **GDSN**, and channel CSVs for Akeneo / Salsify / inRiver / Shopify — plus a **FastAPI** read/write service (which the PRD already listed as a deliverable and was never built). Nothing exports today.

## 3.3 If you only do five things

Ranked by (judge impact) ÷ (effort):

1. **Repoint the domain and define the product schema + attribute dictionary** (§A). Nothing else matters until the system knows what a product is.
2. **Schema-directed extraction + unit/enum normalization + golden record** (§A/§B). Turns raw strings into commerce-ready data.
3. **The quality scorecard with a before → after number** (§F). This is what gets remembered in the room.
4. **Grounded content generation with click-through-to-source citations** (§E). This is your visual "wow" moment, and it's built on machinery you already have.
5. **Knowledge graph + gap-filling by family traversal** (§C/§I). This is what directly answers *"from limited product information"* — the phrase at the centre of the problem statement.

Everything else — VLM, HITL UI, ANN indexing, exporters — is upside, in that order.

## 3.4 Suggested phasing

| Phase | Work | Outcome |
|---|---|---|
| **0 · Repair** (½ day) | Fix `general_country_name`, disable blanket auto-approve, harden SQL execution, implement GPU/batching or delete the claim from the runbook | The demo path runs |
| **1 · Repoint** (2 days) | `Product` / `ProductAttribute` / `ProductSource` models; `Catalog/` layout; attribute dictionary for 2–3 ETIM classes; schema-directed extraction prompt | Structured product records with provenance |
| **2 · Normalize** (2 days) | Unit & enum normalization, identity resolution, golden record with conflict log, real confidence scoring | Consistency + a defensible quality story |
| **3 · Perceive** (2–3 days) | OCR fallback, layout-aware parsing, VLM for image tables & diagrams, HTML/XLSX ingestion, asset intelligence | Multimodal coverage; scanned & web sources stop being invisible |
| **4 · Generate** (2 days) | Grounded copy/SEO generation, gap-filling agents, knowledge graph + family traversal | The "limited inputs → rich intelligence" narrative |
| **5 · Govern & scale** (2–3 days) | Quality scorecard, HITL review UI, ANN index, batching/parallelism, BMEcat/CSV export + FastAPI | Scale numbers, explainability, commerce-ready output |

## 3.5 What the demo should show

1. Drop in **one thin input** — a two-page datasheet, or just an MPN and a photo.
2. Watch the pipeline run: parse → OCR/VLM on the image table → schema-directed extraction → normalization → graph resolution against the product family.
3. Show the **golden record**: 40 attributes, each with a confidence badge and a click-through to the highlighted source region.
4. Show a **conflict** being caught and arbitrated, and a **gap** being filled by family inheritance — labelled `inferred`, not asserted.
5. Show **generated** marketing copy and SEO metadata, hover a sentence → the source line lights up.
6. Show the **review queue**: three low-confidence attributes, one correction, and the rule it just learned.
7. Show the **scorecard**: completeness 34 % → 87 % across 12 400 SKUs, with wall-clock time.
8. Hit **Export** → BMEcat / Akeneo CSV downloads.
9. Close on **"and this ran entirely offline, on one GPU, with no data leaving the building"** — a genuine differentiator for manufacturers guarding unreleased-product specs.

---

# Part 4 — Bottom line

**What you have:** a well-architected, provenance-first, offline document intelligence engine with unusually mature auditability and incremental-processing discipline — solving a public-finance problem.

**What the challenge needs:** the same engine, repointed at products, plus a product data model, normalization, a knowledge graph, multimodal perception, content generation, quality measurement, and a human review loop.

**The honest gap:** ~40–45 % complete. But the remaining 55 % is mostly *additive* — new layers over a foundation that doesn't need rewriting. Critically, the part you already have (evidence-grounded traceability) is the part that takes longest to build and is hardest for competitors to retrofit under time pressure. Most submissions will demo an impressive LLM output nobody can verify. Yours can demo an output where **every single field clicks through to the pixel it came from.**

That is the winning angle. Lead with it.
