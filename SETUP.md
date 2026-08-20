# Setup & Run Guide

From a clean machine to a running console. Commands are written for **Windows
PowerShell**; macOS/Linux equivalents are noted where they differ.

---

## Step 1 — Install dependencies

```powershell
cd "C:\Users\Kavy Khilrani\Desktop\Projects\offline-document-intelligence"

# Optional but recommended
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

You should see seven categories with their ETIM and UNSPSC codes.

---

## Step 2 — Choose your AI backend

Three modes. **All three produce a working catalog** — the difference is how
many attributes get recovered from prose the deterministic extractor could not
parse.

| Mode | What it is | When to use |
|---|---|---|
| **Offline (Ollama)** | A model on your own machine | Default. Demos, sensitive manufacturer data, no API cost. |
| **Cloud (AWS Bedrock)** | Bedrock via your AWS account | Best extraction quality; no local GPU needed. |
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

Back in your project terminal:

```powershell
ollama pull qwen2.5:14b
python -m product_intel.cli llm use ollama
```

That switches the backend and immediately sends a test request:

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
`ollama pull qwen2.5:7b` and `--model qwen2.5:7b`. See
`python -m product_intel.cli llm models` for all suggestions.

---

### Option B — Cloud with AWS Bedrock

**1. Enable model access.**

Bedrock models are opt-in per account and per region — this is the step people
skip, and it produces an `AccessDeniedException` later.

- Sign in to the **AWS Console** → search **Bedrock**
- Pick your region (top right). **US East (N. Virginia) `us-east-1`** has the
  widest model selection.
- Left sidebar → **Model access** → **Modify model access**
- Tick the models you want (Anthropic Claude, Amazon Nova) → **Submit**
- Amazon models are approved instantly; Anthropic models are usually approved
  within a minute or two.

**2. Create credentials.**

AWS Console → **IAM** → **Users** → **Create user**

- Name it something like `product-intel`
- **Attach policies directly** → search and attach **`AmazonBedrockFullAccess`**

  Tighter alternative, if you'd rather scope it down — create an inline policy:
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }]
  }
  ```
- Create the user, then open it → **Security credentials** →
  **Create access key** → choose **Application running outside AWS**
- Copy both the **Access key ID** (`AKIA…`) and the **Secret access key**.
  The secret is shown once.

**3. Put the credentials in a `.env` file.**

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in the two lines:

```
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

Save and close. `.env` is gitignored, and credentials are never written into
`settings.json`.

> **Alternatives.** Any standard AWS credential source works — boto3's normal
> chain is used, so you can skip `.env` entirely:
> ```powershell
> aws configure                                   # writes ~/.aws/credentials
> $env:AWS_ACCESS_KEY_ID = "AKIA..."              # this session only
> ```
> …or use a named profile:
> ```powershell
> python -m product_intel.cli llm use bedrock --profile my-profile
> ```
> …or an EC2/ECS/EKS instance role, with no credentials on disk at all.
> A variable set in the shell always wins over the `.env` file.

**4. Switch the backend.**

```powershell
python -m product_intel.cli llm use bedrock --region us-east-1
```

Expect:

```
Switched LLM backend
  mode      : CLOUD   (requests go to AWS Bedrock)
  provider  : bedrock
  model     : us.anthropic.claude-3-5-haiku-20241022-v1:0   (provider default)
  region    : us-east-1
  creds     : environment ($AWS_ACCESS_KEY_ID)
  status    : ready

Probing bedrock / us.anthropic.claude-3-5-haiku-20241022-v1:0 ...
OK  round trip 0.91s
```

**5. Pick a different model (optional).**

```powershell
python -m product_intel.cli llm models
```

This queries **your** account and lists what is actually enabled in your
region, rather than guessing. Then:

```powershell
python -m product_intel.cli llm use bedrock --model us.amazon.nova-lite-v1:0
```

> **Inference profiles.** Most current models can only be invoked through a
> cross-region inference profile — an ID starting `us.`, `eu.` or `apac.`.
> `us.anthropic.claude-3-5-haiku-20241022-v1:0` works;
> `anthropic.claude-3-5-haiku-20241022-v1:0` returns
> `ResourceNotFoundException`. When in doubt, copy the ID from `llm models`.

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

The repo ships a generator producing realistic industrial source documents —
multi-column variant tables, mixed unit systems, a scanned page, deliberate
cross-source conflicts, and a SKU that exists only as a thin submittal.

```powershell
python scripts\generate_sample_catalog.py --out Sources
```

> Using your own data instead? Drop PDFs, HTML, CSV/XLSX and images anywhere
> under `Sources\` and skip this step.

---

## Step 4 — Build the catalog

```powershell
python -m product_intel.cli run Sources
```

Ingests every source, resolves product identities, extracts against the
attribute dictionary, normalizes units, assembles golden records, fills gaps
from product families, generates commerce copy, validates, scores and indexes.

```
Quality scorecard
  metric                       before    after     lift
  Completeness (ecommerce)      73.2%    90.0%   +16.8%
  Accuracy                     100.0%   100.0%    +0.0%
  Overall                       85.9%    92.7%    +6.7%

  channel-ready SKUs        : 0 -> 9 of 19 sellable (47%)
  gaps filled from family   : 4
  content fields generated  : 144
```

Re-running is incremental — unchanged sources are skipped.

---

## Step 5 — Open the console

```powershell
python -m product_intel.cli serve
```

Opens **http://localhost:8000** with:

| View | What it's for |
|---|---|
| **Overview** | Before/after quality, provenance breakdown, and which required attributes are least covered across the catalog |
| **Products** | Filterable browser; every attribute row expands to its source, locator and verbatim quote, and links through to the highlighted passage in the document |
| **Review** | Prioritized flag queue with inline corrections |
| **Search** | Attribute matching plus semantic retrieval |
| **Schema** | The attribute dictionary — datatypes, units, legal values, aliases, rules |
| **Settings** | Backend toggle and catalog export |

The API is at **http://localhost:8000/api/docs** (interactive OpenAPI).

Useful flags:

```powershell
python -m product_intel.cli serve --port 9000
python -m product_intel.cli serve --host 0.0.0.0      # expose on your network
python -m product_intel.cli serve --reload            # auto-reload while developing
```

> Added credentials while the server is running? Restart it — `.env` is read at
> startup.

---

## Step 6 — Or stay in the terminal

```powershell
python -m product_intel.cli status
python -m product_intel.cli list

# One product with the evidence behind every attribute
python -m product_intel.cli show VX100-2P-C20

# The "limited information" SKU that inherits from its family
python -m product_intel.cli show VX100-4P-C40

python -m product_intel.cli search "3 pole circuit breaker rated at least 30A"
python -m product_intel.cli review
python -m product_intel.cli correct FV3000-100 body_material "Stainless Steel 316" --reviewer you

python -m product_intel.cli export bmecat
python -m product_intel.cli export csv
```

---

## Switching backends later

```powershell
python -m product_intel.cli llm use ollama        # offline
python -m product_intel.cli llm use bedrock       # cloud
python -m product_intel.cli llm use off           # no model
python -m product_intel.cli llm status            # what's active
python -m product_intel.cli llm test              # probe the connection
```

…or use the toggle in the console's **Settings** view. Both write to the same
`settings.json`, so they stay in step.

To re-extract an existing catalog with a stronger model:

```powershell
python -m product_intel.cli llm use bedrock --model us.anthropic.claude-sonnet-4-20250514-v1:0
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

**`No AWS credentials found`**
`.env` must sit in the project root, next to `settings.json`. No quotes, no
spaces around `=`. Confirm with:
```powershell
python -c "from product_intel.config import settings; print(settings.aws_credential_source())"
```

**`AccessDeniedException`**
Model access is not enabled for that model in that region. Bedrock console →
**Model access** → **Modify model access**. Also check the IAM identity has
`bedrock:InvokeModel`.

**`ResourceNotFoundException`**
The model ID is wrong for the region, or you used a bare model ID where an
inference profile is required. Run `python -m product_intel.cli llm models` and
copy an ID from the list.

**`InvalidSignatureException`**
Usually a mistyped secret key, or a machine clock that has drifted.

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

**Port 8000 is in use**
`python -m product_intel.cli serve --port 9000`

**Start over**
```powershell
python -m product_intel.cli clear --yes
```
Deletes derived data only. `Sources\` is never touched.

---

## What runs where

| | Offline (Ollama) | Cloud (Bedrock) |
|---|---|---|
| Document parsing | your machine | your machine |
| Table & spec extraction | your machine | your machine |
| Unit normalization | your machine | your machine |
| Identity, conflicts, scoring | your machine | your machine |
| Gap-fill on unparsed prose | your machine | **sent to Bedrock** |
| Copy generation | your machine | **sent to Bedrock** |
| Embeddings | your machine | your machine |

Only the two enrichment steps ever call a model. Everything that determines
catalog *correctness* is local in both modes — which is why the offline path is
the default and not a fallback.
