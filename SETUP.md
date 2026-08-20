# Setup & Run Guide

From a clean machine to a built catalog. Commands are written for **Windows
PowerShell** (your setup); macOS/Linux equivalents are noted where they differ.

---

## Step 1 — Install Python dependencies

```powershell
cd "C:\Users\Kavy Khilrani\Desktop\Projects\offline-document-intelligence"

# Optional but recommended: an isolated environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
```

If PowerShell blocks the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**Verify:**

```powershell
python -m product_intel.cli schema
```

You should see the seven categories and their ETIM/UNSPSC codes.

---

## Step 2 — Choose your AI backend

The engine runs in three modes. **All three produce a working catalog** — the
difference is how many attributes get recovered from prose that the
deterministic extractor could not parse.

| Mode | What it is | When to use |
|---|---|---|
| **Offline (Ollama)** | A model running on your own machine | Default. Demos, sensitive manufacturer data, no API cost. |
| **Cloud (OpenRouter)** | One key, most hosted models | Best extraction quality; useful when your GPU is busy. |
| **Off** | No model at all | Fastest. Proves the deterministic core works alone. |

Check what's active at any time:

```powershell
python -m product_intel.cli llm status
```

---

### Option A — Offline with Ollama

```powershell
winget install Ollama.Ollama
```

Then, in a **separate terminal that stays open**:

```powershell
ollama serve
```

Back in your project terminal, pull a model and select it:

```powershell
ollama pull qwen2.5:14b

python -m product_intel.cli llm use ollama
```

That last command switches the backend and immediately sends a test request.
Expect:

```
Switched LLM backend
  mode      : OFFLINE   (no request leaves this machine)
  provider  : ollama
  model     : qwen2.5:14b   (provider default)
  status    : ready

Probing ollama / qwen2.5:14b ...
OK  round trip 1.84s
```

`qwen2.5:14b` needs roughly 10 GB of VRAM. On a smaller card use
`ollama pull qwen2.5:7b` and `--model qwen2.5:7b`. See all suggestions with
`python -m product_intel.cli llm models`.

---

### Option B — Cloud with OpenRouter

**1. Get a key.**

- Sign up at **https://openrouter.ai**
- Go to **https://openrouter.ai/keys** → **Create Key**
- Copy it — it starts with `sk-or-v1-`
- Add credit at **https://openrouter.ai/credits**. A few dollars covers many
  full catalog builds; this engine only calls the model for attributes the
  deterministic pass could not find.

**2. Put the key in a `.env` file.**

Copy the template and edit it:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set the one line:

```
OPENROUTER_API_KEY=sk-or-v1-your-actual-key-here
```

Save and close. `.env` is gitignored, so the key is never committed, and it is
never written into `settings.json`.

> **Alternative — environment variable.** If you prefer not to use a file:
> ```powershell
> $env:OPENROUTER_API_KEY = "sk-or-v1-..."          # this session only
> [Environment]::SetEnvironmentVariable("OPENROUTER_API_KEY","sk-or-v1-...","User")   # permanent
> ```
> macOS/Linux: `export OPENROUTER_API_KEY="sk-or-v1-..."`
>
> A variable set in the shell always wins over the `.env` file.

**3. Switch the backend.**

```powershell
python -m product_intel.cli llm use openrouter
```

Expect:

```
Switched LLM backend
  mode      : CLOUD   (requests go to a hosted API)
  provider  : openrouter
  model     : anthropic/claude-3.5-haiku   (provider default)
  key env   : OPENROUTER_API_KEY [set]
  status    : ready

Probing openrouter / anthropic/claude-3.5-haiku ...
OK  round trip 0.91s
```

**4. Pick a different model (optional).**

```powershell
python -m product_intel.cli llm models
python -m product_intel.cli llm use openrouter --model openai/gpt-4o-mini
```

Each provider remembers its own model, so switching back and forth keeps both
selections.

---

### Option C — No model

```powershell
python -m product_intel.cli llm use off
```

Deterministic extraction only. Everything still runs; coverage is lower and the
scorecard shows it honestly.

---

## Step 3 — Generate the sample catalog

The repo ships a generator that produces realistic industrial source documents —
multi-column variant tables, mixed unit systems, a scanned page, deliberate
cross-source conflicts, and one SKU that exists only as a thin submittal.

```powershell
python scripts\generate_sample_catalog.py --out Sources
```

Creates ~16 files across three manufacturers: datasheets, submittals, HTML
product pages, a distributor price CSV, and product images.

> Using your own data instead? Drop PDFs, HTML, CSV/XLSX and images anywhere
> under `Sources\` (subfolders are fine) and skip this step.

---

## Step 4 — Build the catalog

```powershell
python -m product_intel.cli run Sources
```

This ingests every source, resolves product identities, extracts against the
attribute dictionary, normalizes units, assembles golden records, fills gaps
from product families, generates commerce copy, validates, scores, and indexes.

Expect roughly:

```
Quality scorecard
  metric                       before    after     lift
  Completeness (ecommerce)      73.2%    90.0%   +16.8%
  Accuracy                     100.0%   100.0%    +0.0%
  Overall                       85.9%    92.7%    +6.7%

  channel-ready SKUs        : 0 -> 9 of 19 sellable (47%)
  gaps filled from family   : 4
  content fields generated  : 144
  knowledge graph           : 56 nodes, 239 edges
```

Re-running is incremental — unchanged sources are skipped (~0.1s instead of ~5s).

---

## Step 5 — Explore

```powershell
python -m product_intel.cli status
python -m product_intel.cli list

# One product with the evidence behind every attribute
python -m product_intel.cli show VX100-2P-C20

# The "limited information" SKU that inherits from its family
python -m product_intel.cli show VX100-4P-C40

python -m product_intel.cli search "3 pole circuit breaker rated at least 30A"
python -m product_intel.cli search "stainless steel ball valve 1 inch NPT"

python -m product_intel.cli review
python -m product_intel.cli correct FV3000-100 body_material "Stainless Steel 316" --reviewer you

python -m product_intel.cli export bmecat
python -m product_intel.cli export csv
```

---

## Step 6 — The visual console

```powershell
streamlit run app.py
```

Opens at http://localhost:8501 with four views: **Scorecard**, **Products**
(every attribute expandable to its source quote), **Review queue** (correct
values inline), and **Search**.

The **AI backend toggle is in the sidebar** — flip between Offline, Cloud and
Off without leaving the browser. The choice is written to `settings.json`, so
the CLI picks it up too.

> Adding a key while Streamlit is running? Restart it — `.env` is read at
> startup.

---

## Switching backends later

Anywhere, any time:

```powershell
python -m product_intel.cli llm use ollama        # offline
python -m product_intel.cli llm use openrouter    # cloud
python -m product_intel.cli llm use off           # no model
python -m product_intel.cli llm status            # what's active
python -m product_intel.cli llm test              # probe the connection
```

To re-extract an existing catalog with a stronger model:

```powershell
python -m product_intel.cli llm use openrouter --model anthropic/claude-sonnet-4
python -m product_intel.cli ingest Sources --force
python -m product_intel.cli build
```

`--force` re-processes sources whose checksums are unchanged. Human corrections
survive this — they outrank every automated source.

---

## Troubleshooting

**`llm status` says Ollama is unavailable**
`ollama serve` must be running in its own terminal. Confirm with
`curl http://localhost:11434/api/tags`.

**`No API key in $OPENROUTER_API_KEY`**
`.env` must sit in the project root, next to `settings.json`. Check the line has
no quotes and no spaces around `=`. Confirm with:
```powershell
python -c "from product_intel.config import settings; print(bool(settings.api_key('openrouter')))"
```

**`HTTP 402: Insufficient credits`**
Add credit at https://openrouter.ai/credits.

**`HTTP 404: model not found`**
Check the exact ID at https://openrouter.ai/models — they are case-sensitive and
include the vendor prefix (`anthropic/claude-3.5-haiku`, not `claude-3.5-haiku`).

**The toggle doesn't seem to take effect**
A `PI_LLM_PROVIDER` / `PI_LLM_ENABLED` / `PI_LLM_MODEL` environment variable
overrides `settings.json`. `llm use` warns when one is shadowing your choice.
Clear it with `Remove-Item Env:\PI_LLM_PROVIDER`.

**Scanned pages report "no text layer"**
That is the honest result with no OCR engine installed. To enable OCR:
```powershell
winget install UB-Mannheim.TesseractOCR
pip install pytesseract
```

**First build is slow**
The embedding model (~2 GB) downloads once. To skip embeddings entirely, set
`"embedding_enabled": false` in `settings.json` — everything except semantic
search still works.

**Start over**
```powershell
python -m product_intel.cli clear --yes
```
Deletes derived data only. `Sources\` is never touched.

---

## What runs where

| | Offline (Ollama) | Cloud (OpenRouter) |
|---|---|---|
| Document parsing | your machine | your machine |
| Table & spec extraction | your machine | your machine |
| Unit normalization | your machine | your machine |
| Identity, conflicts, scoring | your machine | your machine |
| Gap-fill on unparsed prose | your machine | **sent to the API** |
| Copy generation | your machine | **sent to the API** |
| Embeddings | your machine | your machine |

Only the two enrichment steps ever call a model. Everything that determines
catalog *correctness* is local in both modes — which is why the offline path is
the default and not a fallback.
