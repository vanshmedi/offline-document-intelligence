# Product Intelligence Engine

**AI-powered product intelligence for industrial commerce.**
Turns scattered product information — datasheets, catalogs, web pages, price
files, images — into structured, validated, commerce-ready product records
where **every single field can be traced back to the exact line it came from.**

Built for the Unilog challenge: manufacturers and distributors in electrical,
plumbing/PVF, HVAC and industrial supply hold thousands of complex SKUs whose
data lives in PDFs, websites and ERP extracts. Getting that into a channel-ready
PIM is slow, manual, and error-prone.

---

## The one thing that makes this different

Most solutions to this problem produce a confident JSON blob nobody can verify.
This one cannot produce an unverifiable value — the data model has no field for it.

```
Body Material                  Stainless Steel 316                    0.86 SRC
    from src_51a18c27 @ p.1 / Table 1 / row 4  [native_table, verified]
    "Body Material | SS316"

Country of Origin              Germany                                0.38 INF
    inferred: not stated for VX100-4P-C40; inherited from family product
    VX-Series, which all variants in this series share
```

Three things are always true of every value in the catalog:

1. **It has a source, or it is explicitly marked as inferred.** There is no third state.
2. **Its quote was re-checked against the canonical mirror on disk.** A quote that
   cannot be located marks the value unverified and costs it confidence.
3. **Its confidence is computed, not asserted** — from extraction method, quote
   verification, schema conformance, source precedence, cross-source
   corroboration and normalization assumptions.

---

## Quick start

```bash
pip install -r requirements.txt

# Pick an AI backend (or skip — it defaults to offline Ollama)
python -m product_intel.cli llm use ollama        # offline, on this machine
python -m product_intel.cli llm use openrouter    # cloud, needs a key in .env
python -m product_intel.cli llm use off           # no model at all

# Generate a synthetic industrial catalog (electrical / plumbing / HVAC)
python scripts/generate_sample_catalog.py --out Sources

# Ingest, enrich, validate, score and index
python -m product_intel.cli run Sources

# Explore
python -m product_intel.cli status
python -m product_intel.cli show VX100-2P-C20
python -m product_intel.cli search "3 pole circuit breaker rated at least 30A"
python -m product_intel.cli review
python -m product_intel.cli export bmecat

# Visual console (the backend toggle lives in the sidebar)
streamlit run app.py
```

**Full step-by-step setup, including how to get and install an API key:
[SETUP.md](SETUP.md).**

It runs with **no language model and no GPU**. Extraction is deterministic-first;
the LLM is a gap-filler, not the engine. To prove it:

```bash
PI_LLM_ENABLED=false PI_EMBEDDING_ENABLED=false python -m product_intel.cli run Sources
```

---

## Offline or cloud, switchable at runtime

| Mode | Backend | Trade-off |
|---|---|---|
| **Offline** | Ollama on this machine | Nothing leaves the building. The default, and the right answer for unreleased manufacturer specs. |
| **Cloud** | OpenRouter — one key, most hosted models | Higher extraction coverage on messy prose; costs per token. |
| **Off** | none | Fastest. Deterministic extraction only. |

```bash
python -m product_intel.cli llm status     # what's active, and can it be reached
python -m product_intel.cli llm models     # models known to work well here
python -m product_intel.cli llm test       # probe the connection
python -m product_intel.cli llm use openrouter --model openai/gpt-4o-mini
```

The choice persists to `settings.json` and is shared by the CLI and the console.
Each provider remembers its own model, so switching back and forth never leaves
a cloud API pointed at an Ollama tag it has never heard of.

**Only two steps ever call a model** — gap-filling on prose the deterministic
pass could not parse, and copy generation. Parsing, table extraction, unit
normalization, identity resolution, conflict arbitration, validation and scoring
are local in every mode. That is why offline is the default rather than a
fallback: nothing that determines catalog *correctness* depends on the network.

API keys are read from the environment or a gitignored `.env` file, and are
never written to `settings.json`.

---

## Architecture

```
┌── L1 · INGEST & PERCEIVE ────────────────────────────────────────────────┐
│  PDF · HTML · CSV/XLSX · images                                          │
│  layout-aware parse → OCR fallback → provenance-tagged Fragments         │
│  Tables keep their shape. Pages with no text layer are reported, not     │
│  silently dropped.                                                       │
└──────────────────────────────────────────────────────────────────────────┘
                                    ▼
┌── L2 · RESOLVE & STRUCTURE ──────────────────────────────────────────────┐
│  identity resolution (MPN/GTIN normalization, variant ↔ family)          │
│  category classification → ETIM / UNSPSC                                 │
│  SCHEMA-DIRECTED extraction against the attribute dictionary             │
│  unit + enum normalization → one canonical vocabulary                    │
│  golden record: multi-source arbitration, losers and reasons retained    │
└──────────────────────────────────────────────────────────────────────────┘
                                    ▼
┌── L3 · ENRICH & GENERATE ────────────────────────────────────────────────┐
│  knowledge graph (variant_of, compatible_with, certified_by, …)          │
│  gap filling by family traversal — tagged `inferred`, never as observed  │
│  grounded copy: descriptions, bullets, SEO, keywords                     │
│  generated numbers are checked against source facts and flagged if new   │
└──────────────────────────────────────────────────────────────────────────┘
                                    ▼
┌── L4 · VALIDATE & GOVERN ────────────────────────────────────────────────┐
│  schema conformance · cross-attribute rules · peer-group outliers        │
│  confidence scoring → human review queue → corrections → learned rules   │
│  quality scorecard (before → after)                                      │
│  export: JSON · CSV · BMEcat 2005 + ETIM · GDSN                          │
└──────────────────────────────────────────────────────────────────────────┘
```

### The four expected outcomes

| Outcome | How it is met |
|---|---|
| **Structured data generation from limited inputs** | Schema-directed extraction against a per-category attribute dictionary; gap filling from the product family for SKUs whose own documents state only their ratings; grounded generation of descriptions, bullets, SEO metadata and search keywords. |
| **Accuracy & consistency** | Unit and enum normalization to one canonical vocabulary; cross-attribute rules; peer-group outlier detection; multi-source conflict arbitration by source precedence with the losing values retained. |
| **AI validation & enrichment** | Six-factor confidence scoring; quote re-verification against the canonical mirror; a prioritized human review queue whose corrections are promoted to rules that apply to every future ingest. |
| **Scalable catalog engine** | Checksum-keyed incremental ingest; parallel parsing; content-hash embedding cache; indexed DuckDB with delta-merge upsert; ANN index above ~2k products; deterministic extraction means most attributes cost zero inference. |

---

## Why deterministic-first

The extractor runs in two tiers:

**Tier 1 — deterministic.** Spec tables and key/value blocks are resolved against
the attribute dictionary's aliases with no model at all. On real datasheets this
recovers most attributes, at zero inference cost, with exact provenance.

**Tier 2 — LLM gap-fill.** Only the attributes Tier 1 could not find, over only
the fragments plausibly containing them. The prompt names the exact attributes,
their datatypes and their legal values, and demands a verbatim quote. **A value
whose quote cannot be found in the source is discarded, not repaired.**

This matters for cost and for correctness. Extraction quality does not degrade
when the model is unavailable — coverage does, visibly, in the scorecard.

---

## The attribute dictionary

`product_intel/schema/data/attribute_sets.json` is the contract that makes
extraction directed rather than open-ended. Per attribute it declares the
canonical name, datatype, unit family, legal values, plausibility bounds,
cardinality, the surface aliases that appear in real documents, and which sales
channel requires it.

```json
{ "code": "interrupting_rating_ka", "name": "Interrupting Rating (Icu)",
  "datatype": "number", "unit_family": "current", "canonical_unit": "kA",
  "min": 1, "max": 200, "required_for": ["ecommerce"],
  "aliases": ["interrupting rating", "breaking capacity", "icu", "ics",
              "short circuit rating", "aic", "kaic"] }
```

This is what fixes attribute-name chaos (`Operating Temp` / `Operating
Temperature Range` / `Temp. (°C)` are one attribute), gives completeness a
denominator, and makes two runs diffable.

Shipped categories map to **ETIM** and **UNSPSC** classes:

| Category | ETIM | Vertical |
|---|---|---|
| Circuit Breaker | EC000109 | Electrical |
| Contactor | EC000029 | Electrical |
| Ball Valve | EC002516 | Plumbing / PVF |
| Pipe Fitting | EC002424 | Plumbing / PVF |
| Centrifugal Blower | EC001355 | HVAC |
| Thermostat | EC000414 | HVAC |
| Generic Industrial | — | Industrial |

Adding a vertical is a data change, not a code change.

---

## Normalization

```
'1/2"'          → 12.7 mm        '600 PSI WOG'  → 41.37 bar
'1 1/2 in'      → 38.1 mm        '1200 CFM'     → 2038.81 m³/h
'2.4 lbs'       → 1.089 kg       '1.0 in. w.g.' → 0.00249 bar
'-20 F'         → -28.89 °C      '3/4 HP'       → 559.27 W
'SS316'         → Stainless Steel 316    'FNPT' → NPT Threaded
'full bore'     → Full Port      '35mm DIN rail' → DIN Rail
```

The raw surface form and raw unit are kept on every value, so the conversion is
reversible and auditable. Two cases worth noting because both silently corrupt
data when handled naively:

- **Fahrenheit needs an offset, not a factor.** A multiplicative conversion turns
  −20 °F into −11 °C.
- **`240/415 V AC` is not a fraction.** Dividing it yields a confident 0.578 V.
  Only proper numerators over binary-ish denominators are treated as fractions.

---

## Conflict arbitration

When a datasheet says 10 kA and the web page says 6 kA:

```
interrupting_rating_ka: kept 10.0
    rule: source precedence: datasheet (90) outranks the alternatives (60)
    rejected 6.0 from manufacturer_web (src_69a73760)
```

Precedence: `human > datasheet > ERP > catalog > manufacturer web > price file >
distributor web > image > inferred`. Numeric near-agreement within tolerance is
treated as **corroboration**, not conflict, and raises confidence. Nothing is
discarded — every losing value stays on the product with its source.

---

## Human in the loop, and the learning loop

```bash
product-intel review
product-intel correct FV3000-100 body_material "Stainless Steel 316" --reviewer alice
```

A correction is recorded as a `HUMAN`-method observation, which outranks every
automated source — so it survives re-ingestion of the document that was wrong.

Corrections are then **promoted to rules**:

- an enum surface form a reviewer mapped becomes a learned synonym applied to
  every future ingest;
- three identical corrections for one manufacturer/attribute become a
  manufacturer default.

That is what turns human-in-the-loop from a checkbox into a scalability
argument: a reviewer corrects one product, not five hundred.

---

## Commands

| Command | Purpose |
|---|---|
| `ingest [paths]` | Parse sources into product observations. Incremental. |
| `build` | Enrich, validate, score, index the whole catalog. |
| `run [paths]` | Both, in one step. |
| `status` | Catalog, source and quality summary. |
| `show <MPN>` | One product with the evidence behind every attribute. |
| `list` | Filterable product list. |
| `search <query>` | Attribute filters → SQL; free text → semantics. |
| `review` | Human review queue, highest impact first. |
| `correct <MPN> <attr> <value>` | Apply a correction and learn from it. |
| `llm status` / `use` / `test` / `models` | Switch and inspect the AI backend. |
| `export <fmt>` | `json` · `csv` · `bmecat` · `gdsn`. |
| `query <sql>` | Read-only SQL over the catalog database. |
| `schema` | Print the attribute dictionary. |
| `rebuild-db` | Rebuild analytics tables from product files on disk. |
| `clear` | Delete derived data. Sources are never touched. |

---

## Storage layout

```
Catalog/
  manifest.json               per-source processing state, drives incrementality
  catalog.db                  DuckDB: products · attributes · conflicts
  graph.json                  knowledge graph edges
  review_queue.json           open and resolved review flags
  learned_rules.json          corrections promoted to rules
  vector_index.json           product embeddings
  products/<product_id>.json  the golden record, with all observations
  sources/<source_id>/
      mirror.md               canonical human-readable mirror
      fragments.json          provenance-tagged parsed units
      source.json             source metadata
      audit.json              per-step processing audit
  assets/<asset_id>.json      image analysis and channel compliance
  exports/
```

`catalog.db`, `graph.json` and `vector_index.json` are **derived** — delete them
and `rebuild-db` / `build` regenerates them. The product JSON and the source
mirrors are the durable artifacts: the catalog stays browsable, greppable and
diffable with no database and no AI.

---

## Configuration

`settings.json`, overridable per-field by `PI_*` environment variables. Unknown
keys are **rejected**, so a stale or misspelled key fails loudly instead of
being silently ignored.

| Key | Default | Notes |
|---|---|---|
| `llm_provider` | `ollama` | `ollama` · `openrouter` · `openai` · `null` |
| `llm_model` | `null` | Explicit override; leave null to use the provider's default |
| `ollama_model` | `qwen2.5:14b` | Used when the provider is `ollama` |
| `openrouter_model` | `anthropic/claude-3.5-haiku` | Used when the provider is `openrouter` |
| `llm_enabled` | `true` | `false` runs fully deterministically |
| `embedding_model` | `BAAI/bge-m3` | |
| `embedding_device` | `auto` | `auto` · `cpu` · `cuda` |
| `deterministic_first` | `true` | rules before models |
| `review_confidence_threshold` | `0.70` | below this → review queue |
| `publish_confidence_threshold` | `0.85` | above this → auto-publish |
| `target_channel` | `ecommerce` | `core` · `ecommerce` · `enhanced` |

There is deliberately **no flag that disables validation.** A product that fails
is routed to review; it is never marked complete.

---

## Testing

```bash
python -m unittest discover -s tests -v
```

66 tests, weighted towards the properties the system's value depends on: unit
conversion correctness, the fraction-vs-dual-rating distinction, variant matrix
row selection, quote verification, arbitration, that inferred and generated
values are never mistaken for observed ones, that the validation gate cannot be
disabled, and that SQL execution refuses anything that can write to disk.

---

## Design decisions worth knowing about

**Variant matrices are read by row, matched on part number.** A selection table
lists five SKUs across seven columns. Values are taken from the row whose key
column matches the product's own MPN. If no row matches, nothing is extracted —
an honest gap beats a confident wrong value.

**Identity attributes are never extracted.** A family datasheet names every
sibling's part number; extracting `mpn` from shared fragments would assign one
variant's number to another and manufacture a conflict on the primary key.
Identity comes from resolution, not extraction.

**Near-duplicate part numbers are flagged, not merged.** OCR turns `FV-3000`
into `FV-2000`; but `VX100-1P-C06` and `VX100-1P-C16` are also one character
apart and are genuinely different products. Nothing in the strings distinguishes
those cases, so the system reports a suspicion and a human decides. Silently
merging two real SKUs destroys data irrecoverably.

**Family records exist and are not sellable.** A series record gives variants one
authoritative home for shared attributes to inherit from. It is exempt from
variant-defining requirements and from channel readiness.

**Rule expressions are parsed, never `eval`'d.** Rules live in a JSON data file,
and a data file must not be able to execute code.

**SQL is allowlisted by leading keyword and denied `COPY`/`ATTACH`/`INSTALL`.**
A keyword blocklist alone lets `COPY … TO '/tmp/x'` through, and DuckDB will
happily write that file.
