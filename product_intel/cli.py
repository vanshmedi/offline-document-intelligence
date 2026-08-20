"""Command line interface for the Product Intelligence Engine."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click

from product_intel.config import settings
from product_intel.engine import ProductIntelligenceEngine
from product_intel.export.exporters import DEFAULT_EXTENSIONS, EXPORTERS
from product_intel.manifest import CatalogStore, ManifestManager
from product_intel.models import ProductStatus
from product_intel.pipeline.db_ingest import CatalogDB
from product_intel.review import LearnedRules, ReviewQueue, apply_correction
from product_intel.schema.dictionary import load_taxonomy
from product_intel.validation import catalog_scorecard


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, settings.log_level, logging.INFO),
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def _bar(value: float, width: int = 22) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * filled + "·" * (width - filled)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """Product Intelligence Engine - turn scattered product data into commerce-ready records."""
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# Ingestion and build
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--manufacturer", help="Manufacturer hint when documents do not state one.")
@click.option("--force", is_flag=True, help="Re-process sources even if unchanged.")
@click.option("--no-parallel", is_flag=True, help="Process sources serially.")
def ingest(paths, manufacturer: Optional[str], force: bool, no_parallel: bool) -> None:
    """Ingest source documents (PDF, HTML, CSV, XLSX, images) into the catalog."""
    targets = list(paths) or [settings.sources_path]
    engine = ProductIntelligenceEngine()

    click.echo(click.style("Ingesting", bold=True) + f" from: {', '.join(str(p) for p in targets)}")
    click.echo(f"LLM: {engine.provider.name} ({'available' if engine.provider.available else 'unavailable - deterministic extraction only'})")

    report = engine.ingest(targets, force=force, manufacturer_hint=manufacturer, parallel=not no_parallel)

    click.echo()
    click.echo(click.style("Ingest complete", fg="green", bold=True))
    click.echo(f"  sources seen      : {report.sources_seen}")
    click.echo(f"  processed         : {report.sources_processed}")
    click.echo(f"  skipped (unchanged): {report.sources_skipped}")
    if report.sources_failed:
        click.echo(click.style(f"  failed            : {report.sources_failed}", fg="red"))
    click.echo(f"  products created  : {report.products_created}")
    click.echo(f"  products updated  : {report.products_updated}")
    click.echo(f"  fragments         : {report.fragments}")
    click.echo(f"  observations      : {report.observations}")
    if report.llm_calls:
        click.echo(f"  LLM calls         : {report.llm_calls}")
    if report.rejected_quotes:
        click.echo(click.style(f"  rejected quotes   : {report.rejected_quotes} (unverifiable, discarded)", fg="yellow"))
    click.echo(f"  duration          : {report.duration_s:.2f}s")

    for name, error in report.failures:
        click.echo(click.style(f"  FAILED {name}: {error}", fg="red"))
    for warning in report.warnings[:10]:
        click.echo(click.style(f"  warn: {warning}", fg="yellow"))

    click.echo()
    click.echo("Next: " + click.style("product-intel build", bold=True))


@cli.command()
@click.option("--no-enrich", is_flag=True, help="Skip gap filling and content generation.")
@click.option("--no-index", is_flag=True, help="Skip building the vector index.")
def build(no_enrich: bool, no_index: bool) -> None:
    """Enrich, validate, score and index the whole catalog."""
    engine = ProductIntelligenceEngine()
    click.echo(click.style("Building catalog", bold=True))
    out = engine.build(enrich=not no_enrich, index=not no_index)

    if out.get("error"):
        raise click.ClickException(out["error"])

    before, after, lift = out["before"], out["after"], out["lift"]

    click.echo()
    click.echo(click.style("Quality scorecard", bold=True))
    click.echo(f"  {'metric':<26}{'before':>9}{'after':>9}{'lift':>9}   after")
    rows = [
        ("Completeness (core)", "completeness_core"),
        ("Completeness (ecommerce)", "completeness_ecommerce"),
        ("Completeness (enhanced)", "completeness_enhanced"),
        ("Accuracy", "accuracy"),
        ("Consistency", "consistency"),
        ("Distinctiveness", "distinctiveness"),
        ("Overall", "overall"),
    ]
    for label, key in rows:
        b, a = before.get(key, 0.0), after.get(key, 0.0)
        delta = a - b
        colour = "green" if delta > 0.0005 else ("red" if delta < -0.0005 else None)
        click.echo(
            f"  {label:<26}{_pct(b):>9}{_pct(a):>9}"
            + click.style(f"{delta * 100:>+8.1f}%", fg=colour)
            + f"   {_bar(a)}"
        )

    click.echo()
    click.echo(
        f"  channel-ready SKUs        : {before['channel_ready']} -> "
        + click.style(f"{after['channel_ready']}", fg="green", bold=True)
        + f" of {after['sellable']} sellable ({after['channel_ready_pct']:.0f}%)"
        + f"   [+{after['families']} family records]"
    )
    click.echo(f"  attributes (sourced)      : {after['attributes_sourced']} @ mean confidence {after['confidence_sourced']:.2f}")
    click.echo(f"  attributes (generated)    : {after['attributes_generated']} @ mean confidence {after['confidence_generated']:.2f}")
    click.echo(f"  attributes inferred       : {after['inferred_attributes']}")
    click.echo(f"  conflicts recorded        : {after['conflicts']}")

    if "enrichment" in out:
        e = out["enrichment"]
        click.echo(f"  gaps filled from family   : {e['gap_filled']}")
        click.echo(f"  content fields generated  : {e['generated']}")

    graph = out.get("graph", {})
    if graph:
        click.echo(f"  knowledge graph           : {graph['nodes']} nodes, {graph['edges']} edges")
    if out.get("database"):
        d = out["database"]
        click.echo(f"  database rows             : {d['products']} products, {d['attributes']} attributes")
    if out.get("index", {}).get("indexed"):
        i = out["index"]
        cached = i.get("cache_hits", 0)
        click.echo(f"  vector index              : {i['indexed']} products ({cached} from cache, device {i.get('device') or 'n/a'})")

    review = out.get("review", {})
    if review.get("open"):
        click.echo()
        click.echo(click.style(f"  {review['open']} items need human review", fg="yellow", bold=True)
                   + f" across {review['products_affected']} products")
        for reason, count in sorted(review["by_reason"].items(), key=lambda kv: -kv[1]):
            click.echo(f"      {reason:<24} {count}")
        click.echo("  Run: " + click.style("product-intel review", bold=True))

    click.echo()
    click.echo(f"  duration: {out['duration_s']}s")


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--manufacturer", help="Manufacturer hint when documents do not state one.")
def run(paths, manufacturer: Optional[str]) -> None:
    """Ingest then build in one step."""
    ctx = click.get_current_context()
    ctx.invoke(ingest, paths=paths, manufacturer=manufacturer, force=False, no_parallel=False)
    click.echo()
    ctx.invoke(build, no_enrich=False, no_index=False)


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show catalog and pipeline status."""
    manifest = ManifestManager()
    store = CatalogStore()
    products = store.load_all()

    click.echo(click.style("Sources", bold=True))
    stats = manifest.stats()
    click.echo(f"  registered: {stats.get('total', 0)}")
    for key, count in sorted(stats.items()):
        if key != "total":
            click.echo(f"    {key:<14} {count}")

    click.echo()
    click.echo(click.style("Catalog", bold=True))
    click.echo(f"  products: {len(products)}")
    if products:
        by_status: dict = {}
        by_category: dict = {}
        for p in products:
            key = p.status.value if hasattr(p.status, "value") else str(p.status)
            by_status[key] = by_status.get(key, 0) + 1
            by_category[p.category_id] = by_category.get(p.category_id, 0) + 1
        for key, count in sorted(by_status.items()):
            click.echo(f"    {key:<14} {count}")
        click.echo()
        click.echo(click.style("  By category", bold=True))
        taxonomy = load_taxonomy()
        for cid, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
            click.echo(f"    {taxonomy.get(cid).name:<28} {count}")

        card = catalog_scorecard(products)
        click.echo()
        click.echo(click.style("  Quality", bold=True))
        click.echo(f"    overall            {_pct(card['overall'])}  {_bar(card['overall'])}")
        click.echo(f"    completeness (ecom){_pct(card['completeness_ecommerce'])}  {_bar(card['completeness_ecommerce'])}")
        click.echo(f"    accuracy           {_pct(card['accuracy'])}  {_bar(card['accuracy'])}")
        click.echo(f"    channel ready      {card['channel_ready']}/{card['products']}")

    queue = ReviewQueue()
    qstats = queue.stats()
    if qstats["total"]:
        click.echo()
        click.echo(click.style("Review queue", bold=True))
        click.echo(f"  open: {qstats['open']}  resolved: {qstats['resolved']}")

    db = CatalogDB()
    summary = db.table_summary()
    if summary:
        click.echo()
        click.echo(click.style("Database", bold=True) + f"  {summary}")


@cli.command()
@click.argument("identifier")
@click.option("--evidence/--no-evidence", default=True, help="Show the source behind each value.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def show(identifier: str, evidence: bool, as_json: bool) -> None:
    """Show one product in full, with the evidence behind every attribute."""
    engine = ProductIntelligenceEngine()
    product = engine.get_product(identifier)
    if product is None:
        raise click.ClickException(f"No product matches '{identifier}'.")

    schema = engine.schema_for(product)

    if as_json:
        click.echo(json.dumps(product.model_dump(mode="json"), indent=2))
        return

    ident = product.identity
    click.echo(click.style(f"{ident.manufacturer} {ident.mpn}", bold=True))
    click.echo(f"  {product.display_name()}")
    click.echo(f"  product_id : {ident.product_id}")
    click.echo(f"  category   : {schema.name}  (ETIM {schema.etim or 'n/a'} / UNSPSC {schema.unspsc or 'n/a'})")
    if ident.series:
        click.echo(f"  series     : {ident.series}")
    if ident.alternate_mpns:
        click.echo(f"  also seen as: {', '.join(ident.alternate_mpns)}")
    status_value = product.status.value if hasattr(product.status, "value") else str(product.status)
    click.echo(f"  status     : {status_value}")
    click.echo(f"  sources    : {len(product.source_ids)}   assets: {len(product.asset_ids)}")

    q = product.quality
    click.echo()
    click.echo(click.style("  Quality", bold=True))
    click.echo(f"    completeness (ecommerce) {_pct(q.completeness_ecommerce)}  {_bar(q.completeness_ecommerce)}")
    click.echo(f"    accuracy                 {_pct(q.accuracy)}  {_bar(q.accuracy)}")
    click.echo(f"    consistency              {_pct(q.consistency)}  {_bar(q.consistency)}")
    click.echo(f"    overall                  {_pct(q.overall)}  {_bar(q.overall)}")
    if q.missing_required:
        click.echo(click.style(f"    missing required: {', '.join(q.missing_required)}", fg="yellow"))

    click.echo()
    click.echo(click.style("  Attributes", bold=True))
    for code in sorted(product.attributes):
        av = product.attributes[code]
        attr = schema.get(code)
        label = attr.name if attr else code
        origin = "GEN" if (attr and attr.generated) else ("INF" if av.inference else "SRC")
        colour = "green" if av.confidence >= 0.85 else ("yellow" if av.confidence >= 0.7 else "red")
        value = av.display()
        if len(value) > 68:
            value = value[:65] + "..."
        click.echo(
            f"    {label:<30} {value:<70} "
            + click.style(f"{av.confidence:.2f}", fg=colour)
            + f" {origin}"
        )
        if evidence:
            if av.evidence is not None:
                mark = "verified" if av.evidence.quote_verified else click.style("UNVERIFIED", fg="red")
                click.echo(f"        from {av.evidence.source_id} @ {av.evidence.locator} [{av.evidence.method.value}, {mark}]")
                quote = av.evidence.quote.replace("\n", " ")[:110]
                if quote:
                    click.echo(click.style(f'        "{quote}"', dim=True))
            elif av.inference is not None:
                click.echo(click.style(f"        inferred: {av.inference.rationale[:110]}", fg="cyan"))

    if product.conflicts:
        click.echo()
        click.echo(click.style("  Conflicts", bold=True))
        for c in product.conflicts:
            colour = "red" if c.severity == "critical" else "yellow"
            click.echo(click.style(f"    {c.code}: kept {c.winning_value}", fg=colour))
            click.echo(f"        rule: {c.resolution_rule}")
            for loser in c.losing_values[:3]:
                click.echo(f"        rejected {loser['value']} from {loser['source_kind']} ({loser['source_id']})")


@cli.command(name="list")
@click.option("--category", help="Filter by category id.")
@click.option("--manufacturer", help="Filter by manufacturer.")
@click.option("--status", "status_filter", help="Filter by status.")
@click.option("--limit", default=100, help="Maximum rows.")
def list_products(category, manufacturer, status_filter, limit) -> None:
    """List catalog products."""
    engine = ProductIntelligenceEngine()
    products = engine.products()

    if category:
        products = [p for p in products if p.category_id == category]
    if manufacturer:
        products = [p for p in products if manufacturer.lower() in p.identity.manufacturer.lower()]
    if status_filter:
        products = [p for p in products
                    if (p.status.value if hasattr(p.status, "value") else str(p.status)) == status_filter]

    products.sort(key=lambda p: (p.identity.manufacturer, p.identity.mpn))
    click.echo(f"{'MANUFACTURER':<22}{'MPN':<16}{'CATEGORY':<26}{'ATTRS':>6}{'QUAL':>7}{'STATUS':>16}")
    click.echo("-" * 93)
    for p in products[:limit]:
        status_value = p.status.value if hasattr(p.status, "value") else str(p.status)
        colour = "green" if status_value == "published" else ("yellow" if status_value == "needs_review" else None)
        click.echo(
            f"{p.identity.manufacturer[:21]:<22}{p.identity.mpn[:15]:<16}"
            f"{p.category_id[:25]:<26}{len(p.attributes):>6}{p.quality.overall:>7.2f}"
            + click.style(f"{status_value:>16}", fg=colour)
        )
    click.echo(f"\n{len(products)} product(s).")


@cli.command()
@click.argument("query")
@click.option("--limit", default=8)
@click.option("--json", "as_json", is_flag=True)
def search(query: str, limit: int, as_json: bool) -> None:
    """Search the catalog. Attribute filters resolve to SQL; free text falls back to semantics."""
    from product_intel.search import answer

    engine = ProductIntelligenceEngine()
    result = answer(query, engine.products(), engine.taxonomy, limit=limit)

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    click.echo(click.style(f'Query: "{query}"', bold=True))
    click.echo(click.style(f"  plan: {result['plan']['mode']} - {result['plan']['explanation']}", dim=True))
    click.echo()

    if not result["results"]:
        click.echo(click.style(result["answer"], fg="yellow"))
        return

    for r in result["results"]:
        click.echo(click.style(f"  {r['manufacturer']} {r['mpn']}", bold=True)
                   + f"  [{r['category']}]  quality {r['quality']:.2f}  {r['status']}")
        for label, value in r["attributes"].items():
            click.echo(f"      {label:<28} {value}")
        click.echo()

    if result["citations"]:
        click.echo(click.style(f"Evidence ({len(result['citations'])} citations)", bold=True))
        for c in result["citations"][:12]:
            mark = "" if c["verified"] else click.style(" [unverified]", fg="red")
            click.echo(f"  {c['product']} / {c['attribute']} = {c['value']}")
            click.echo(click.style(f"      {c['source_id']} @ {c['locator']}{mark}", dim=True))


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", default=25, help="How many flags to show.")
@click.option("--product", "product_filter", help="Only show flags for one product.")
def review(limit: int, product_filter: Optional[str]) -> None:
    """Show the human review queue, highest impact first."""
    engine = ProductIntelligenceEngine()
    queue = ReviewQueue()
    flags = queue.prioritized(limit=500)

    if product_filter:
        target = engine.get_product(product_filter)
        if target is None:
            raise click.ClickException(f"No product matches '{product_filter}'.")
        flags = [f for f in flags if f.product_id == target.identity.product_id]

    if not flags:
        click.echo(click.style("Review queue is empty.", fg="green"))
        return

    by_product = {p.identity.product_id: p for p in engine.products()}
    click.echo(click.style(f"{len(flags)} open item(s), highest impact first", bold=True))
    click.echo()

    for flag in flags[:limit]:
        product = by_product.get(flag.product_id)
        label = f"{product.identity.manufacturer} {product.identity.mpn}" if product else flag.product_id
        colour = "red" if flag.severity == "critical" else "yellow"
        click.echo(click.style(f"  [{flag.severity.upper()}]", fg=colour) + f" {label}")
        click.echo(f"      attribute : {flag.attribute_code or '(product-level)'}")
        click.echo(f"      reason    : {flag.reason}")
        if flag.suggested_value is not None:
            click.echo(f"      current   : {flag.suggested_value}")
        click.echo(click.style(f"      flag_id   : {flag.flag_id}", dim=True))
        click.echo()

    click.echo("Fix with: " + click.style(
        "product-intel correct <MPN> <attribute> <value>", bold=True))


@cli.command()
@click.argument("identifier")
@click.argument("attribute")
@click.argument("value")
@click.option("--reviewer", default="reviewer", help="Who is making this correction.")
@click.option("--note", default="", help="Why.")
def correct(identifier: str, attribute: str, value: str, reviewer: str, note: str) -> None:
    """
    Apply a human correction. It outranks every automated source and is learned from.
    """
    engine = ProductIntelligenceEngine()
    product = engine.get_product(identifier)
    if product is None:
        raise click.ClickException(f"No product matches '{identifier}'.")

    schema = engine.schema_for(product)
    if schema.get(attribute) is None:
        raise click.ClickException(
            f"'{attribute}' is not defined for category '{schema.name}'. "
            f"Valid: {', '.join(sorted(schema.attributes)[:12])}..."
        )

    old = product.attributes.get(attribute)
    old_value = old.value if old else None

    av = apply_correction(product, attribute, value, schema, reviewer=reviewer, note=note)
    if av.value is None:
        raise click.ClickException(f"'{value}' is not valid for {attribute}: {'; '.join(av.validation_errors)}")

    learned = LearnedRules()
    promoted = learned.learn_from_correction(product, attribute, old_value, av.value, reviewer)
    learned.flush()

    queue = ReviewQueue()
    resolved = 0
    for flag in queue.open_flags(product.identity.product_id):
        if flag.attribute_code == attribute:
            queue.resolve(flag.flag_id, f"corrected to '{av.value}' by {reviewer}", reviewer)
            resolved += 1
    queue.flush()

    engine.store.save_product(product)
    CatalogDB().upsert([product])

    click.echo(click.style("Correction applied", fg="green", bold=True))
    click.echo(f"  {product.identity.mpn} / {attribute}: {old_value!r} -> {av.value!r}")
    click.echo(f"  confidence: {av.confidence:.2f} (human corrections outrank every automated source)")
    if resolved:
        click.echo(f"  resolved {resolved} review flag(s)")
    if promoted:
        click.echo(click.style(f"  learned rule: {promoted}", fg="cyan"))
        click.echo("  This will be applied automatically on future ingests.")


# ---------------------------------------------------------------------------
# LLM provider toggle
# ---------------------------------------------------------------------------


@cli.group()
def llm() -> None:
    """Switch between the offline (Ollama) and cloud (AWS Bedrock) backends."""


def _provider_report(cfg) -> None:
    from product_intel.llm.provider import get_provider

    provider = get_provider(cfg)
    disabled = not cfg.llm_enabled or cfg.llm_provider == "null"

    if disabled:
        mode, mode_colour, note = "OFF", "yellow", "   (deterministic extraction only)"
    elif cfg.is_offline:
        mode, mode_colour, note = "OFFLINE", "green", "   (no request leaves this machine)"
    else:
        mode, mode_colour, note = "CLOUD", "cyan", "   (requests go to AWS Bedrock)"

    click.echo(click.style(f"  mode      : {mode}", fg=mode_colour, bold=True) + note)
    click.echo(f"  provider  : {cfg.llm_provider}")
    if not disabled:
        click.echo(f"  model     : {cfg.active_model}"
                   + ("" if cfg.llm_model else "   (provider default)"))
    click.echo(f"  enabled   : {cfg.llm_enabled}")

    if disabled:
        click.echo(click.style(
            "  No model will be called. Coverage is lower; nothing is fabricated.", dim=True))
        return

    if cfg.llm_provider == "ollama":
        click.echo(f"  endpoint  : {cfg.ollama_base_url}")
    elif cfg.llm_provider == "bedrock":
        click.echo(f"  region    : {cfg.aws_region}")
        source = cfg.aws_credential_source()
        found = source != "not found"
        click.echo("  creds     : " + click.style(source, fg="green" if found else "red"))

    reachable = provider.available
    click.echo("  status    : " + click.style(
        "ready" if reachable else "unavailable", fg="green" if reachable else "red"))

    if not reachable:
        click.echo()
        if cfg.llm_provider == "ollama":
            click.echo(click.style("  Ollama is not responding. Start it with:", fg="yellow"))
            click.echo("      ollama serve")
            click.echo(f"      ollama pull {cfg.active_model}")
        else:
            click.echo(click.style("  No AWS credentials found. Add them to .env:", fg="yellow"))
            click.echo(f"      {cfg.aws_access_key_id_env}=AKIA...")
            click.echo(f"      {cfg.aws_secret_access_key_env}=...")
            click.echo("  ...or run: aws configure")
        click.echo(click.style(
            "  The pipeline still runs -- deterministic extraction only, reduced coverage.",
            dim=True))


@llm.command("status")
def llm_status() -> None:
    """Show which backend is active and whether it can be reached."""
    click.echo(click.style("LLM backend", bold=True))
    _provider_report(settings)


@llm.command("use")
@click.argument("provider", type=click.Choice(["ollama", "bedrock", "off"]))
@click.option("--model", help="Model to use with this provider. Omit for the provider's default.")
@click.option("--region", help="AWS region (bedrock only).")
@click.option("--profile", help="AWS named profile (bedrock only). Omit to use the default chain.")
@click.option("--test/--no-test", default=True, help="Send a probe request after switching.")
def llm_use(provider: str, model: Optional[str], region: Optional[str],
            profile: Optional[str], test: bool) -> None:
    """
    Switch the active LLM backend. The choice is persisted to settings.json.

    \b
      ollama   offline, on this machine
      bedrock  AWS Bedrock, via the standard AWS credential chain
      off      no model; deterministic extraction only
    """
    from product_intel.config import reload_settings, save_settings

    updates: dict = {}
    if provider == "off":
        updates["llm_enabled"] = False
        updates["llm_provider"] = "null"
    else:
        updates["llm_enabled"] = True
        updates["llm_provider"] = provider
        # Clear any global override so the provider's own default applies.
        updates["llm_model"] = None
        if model:
            # Store against the provider so switching back and forth remembers both.
            updates[f"{provider}_model"] = model
        if provider == "bedrock":
            if region:
                updates["aws_region"] = region
            if profile:
                updates["aws_profile"] = profile

    try:
        save_settings(updates)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"Could not save settings: {exc}")

    cfg = reload_settings()
    click.echo(click.style("Switched LLM backend", fg="green", bold=True))
    _provider_report(cfg)

    # Environment variables outrank settings.json by design. Silently losing a
    # toggle to a stale PI_* export is a genuinely confusing failure, so say so.
    import os as _os

    shadowing = [k for k in ("PI_LLM_PROVIDER", "PI_LLM_ENABLED", "PI_LLM_MODEL") if k in _os.environ]
    if shadowing:
        click.echo()
        click.echo(click.style(
            f"  Note: {', '.join(shadowing)} is set in your environment and overrides "
            f"settings.json. Unset it for this toggle to take effect.", fg="yellow"))

    if test and cfg.llm_enabled:
        click.echo()
        ctx = click.get_current_context()
        ctx.invoke(llm_test)


@llm.command("test")
def llm_test() -> None:
    """Send one probe request to the active backend and report what came back."""
    import time as _time

    from product_intel.config import reload_settings
    from product_intel.llm.provider import LLMUnavailable, get_provider

    cfg = reload_settings()
    provider = get_provider(cfg)

    if not cfg.llm_enabled:
        click.echo(click.style("LLM is disabled. Nothing to test.", fg="yellow"))
        click.echo("Enable it with: " + click.style("product-intel llm use ollama", bold=True))
        return

    click.echo(f"Probing {cfg.llm_provider} / {cfg.active_model} ...")
    started = _time.time()
    try:
        result = provider.complete_json(
            'Reply with exactly this JSON and nothing else: {"ok": true, "engine": "<your model name>"}',
            expect="object",
        )
    except LLMUnavailable as exc:
        click.echo(click.style("FAILED", fg="red", bold=True))
        click.echo(f"  {exc}")
        raise SystemExit(1)

    elapsed = _time.time() - started
    click.echo(click.style("OK", fg="green", bold=True) + f"  round trip {elapsed:.2f}s")
    click.echo(f"  response: {json.dumps(result)[:160]}")


@llm.command("models")
@click.option("--live/--no-live", default=True,
              help="Query AWS for what is actually enabled in your account (bedrock only).")
def llm_models(live: bool) -> None:
    """List models for each backend."""
    from product_intel.llm.provider import (
        BEDROCK_SUGGESTED_MODELS,
        OLLAMA_SUGGESTED_MODELS,
        BedrockProvider,
        LLMUnavailable,
    )

    click.echo(click.style("Ollama (offline)", bold=True))
    for name, note in OLLAMA_SUGGESTED_MODELS:
        marker = " *" if name == settings.ollama_model else "  "
        click.echo(f"{marker} {name:<46} {note}")
    click.echo(click.style("\n  pull with: ollama pull <name>", dim=True))

    click.echo()
    click.echo(click.style(f"AWS Bedrock (cloud, region {settings.aws_region})", bold=True))

    listed = False
    if live and settings.aws_credentials_present():
        try:
            models = BedrockProvider(settings).list_available_models()
            if models:
                listed = True
                click.echo(click.style("  Enabled in your account:", dim=True))
                for m in models:
                    marker = " *" if m["id"] == settings.bedrock_model else "  "
                    click.echo(f"{marker} {m['id']:<46} {m['name']} [{m['kind']}]")
        except LLMUnavailable as exc:
            click.echo(click.style(f"  Could not query AWS: {exc}", fg="yellow"))
        except Exception as exc:  # noqa: BLE001
            click.echo(click.style(f"  Could not query AWS: {exc}", fg="yellow"))

    if not listed:
        click.echo(click.style("  Suggested (availability varies by region and account):", dim=True))
        for name, note in BEDROCK_SUGGESTED_MODELS:
            marker = " *" if name == settings.bedrock_model else "  "
            click.echo(f"{marker} {name:<46} {note}")
        click.echo(click.style(
            "\n  Most current models need a cross-region inference profile "
            "(IDs starting 'us.', 'eu.' or 'apac.').", dim=True))
        click.echo(click.style(
            "  Enable models at: Bedrock console -> Model access", dim=True))

    click.echo()
    click.echo("Select with: " + click.style(
        "product-intel llm use bedrock --model us.amazon.nova-lite-v1:0", bold=True))
    click.echo(click.style("  * = current default for that provider", dim=True))


# ---------------------------------------------------------------------------
# Web console
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address. Use 0.0.0.0 to expose on the network.")
@click.option("--port", default=8000, type=int)
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (development).")
@click.option("--open-browser/--no-open-browser", default=True)
def serve(host: str, port: int, reload: bool, open_browser: bool) -> None:
    """Start the API and the web console."""
    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn is not installed. Run: pip install 'uvicorn[standard]' fastapi"
        )

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    click.echo(click.style("Product Intelligence console", bold=True))
    click.echo(f"  console : {url}")
    click.echo(f"  API docs: {url}/api/docs")
    click.echo(f"  catalog : {settings.catalog_path}")
    click.echo(f"  backend : {settings.llm_provider}"
               + ("" if settings.llm_provider == "null" else f" / {settings.active_model}"))
    click.echo()

    if open_browser and not reload:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "product_intel.api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


# ---------------------------------------------------------------------------
# Export and maintenance
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("fmt", type=click.Choice(sorted(EXPORTERS)))
@click.option("--out", type=click.Path(path_type=Path), help="Output file path.")
@click.option("--ready-only/--all", default=None, help="Export only channel-ready products.")
def export(fmt: str, out: Optional[Path], ready_only: Optional[bool]) -> None:
    """Export the catalog (json, csv, bmecat, gdsn)."""
    engine = ProductIntelligenceEngine()
    products = engine.products()
    if not products:
        raise click.ClickException("Catalog is empty. Run ingest and build first.")

    if out is None:
        out = settings.catalog_path / "exports" / f"catalog{DEFAULT_EXTENSIONS[fmt]}"
    if ready_only is None:
        ready_only = fmt in ("bmecat", "gdsn")

    result = EXPORTERS[fmt](products, engine.taxonomy, out, ready_only=ready_only)
    click.echo(click.style("Exported", fg="green", bold=True)
               + f" {result['products']} product(s) as {fmt}")
    click.echo(f"  {result['path']}")
    if ready_only:
        held = len(products) - result["products"]
        if held:
            click.echo(click.style(
                f"  {held} product(s) withheld: not channel-ready. Use --all to include them.",
                fg="yellow"))


@cli.command()
@click.argument("sql")
def query(sql: str) -> None:
    """Run a read-only SQL query against the catalog database."""
    db = CatalogDB()
    try:
        rows = db.query(sql)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    if not rows:
        click.echo("No rows.")
        return
    headers = list(rows[0].keys())
    widths = [max(len(h), max(len(str(r[h])[:38]) for r in rows)) for h in headers]
    click.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    click.echo("-" * (sum(widths) + 2 * len(widths)))
    for row in rows[:200]:
        click.echo("  ".join(str(row[h])[:38].ljust(w) for h, w in zip(headers, widths)))
    click.echo(f"\n{len(rows)} row(s).")


@cli.command()
def schema() -> None:
    """Show the attribute dictionary: categories, attributes and rules."""
    taxonomy = load_taxonomy()
    click.echo(click.style(f"Taxonomy v{taxonomy.version}", bold=True))
    for cid, cat in taxonomy.categories.items():
        click.echo()
        click.echo(click.style(f"  {cat.name}", bold=True)
                   + f"  [{cid}]  ETIM {cat.etim or 'n/a'} / UNSPSC {cat.unspsc or 'n/a'}")
        click.echo(f"    attributes: {len(cat.attributes)}  "
                   f"core: {len(cat.required_codes('core'))}  "
                   f"ecommerce: {len(cat.required_codes('ecommerce'))}  "
                   f"rules: {len(cat.rules)}")
        specific = [a for a in cat.attributes.values() if a.variant_defining]
        if specific:
            click.echo(f"    variant-defining: {', '.join(a.code for a in specific)}")


@cli.command("rebuild-db")
def rebuild_db() -> None:
    """Rebuild the analytics database from the product files on disk."""
    engine = ProductIntelligenceEngine()
    counts = CatalogDB().rebuild(engine.products())
    click.echo(click.style("Rebuilt", fg="green", bold=True) + f" {counts}")


@cli.command()
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.option("--keep-sources", is_flag=True, help="Keep parsed source mirrors.")
def clear(yes: bool, keep_sources: bool) -> None:
    """Delete the derived catalog. Source documents are never touched."""
    import shutil

    root = settings.catalog_path
    if not root.exists():
        click.echo("Nothing to clear.")
        return
    if not yes and not click.confirm(f"Delete everything under {root}?", default=False):
        click.echo("Aborted.")
        return

    for item in root.iterdir():
        if keep_sources and item.name == "sources":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    click.echo(click.style("Cleared.", fg="green"))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
