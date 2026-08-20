"""
Product Intelligence Console.

    streamlit run app.py

Four views onto the same catalog artifacts -- nothing here computes anything the
CLI does not, it only makes the evidence trail clickable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from product_intel.config import settings
from product_intel.engine import ProductIntelligenceEngine
from product_intel.manifest import CatalogStore
from product_intel.review import LearnedRules, ReviewQueue, apply_correction
from product_intel.search import answer
from product_intel.validation import catalog_scorecard

st.set_page_config(
    page_title="Product Intelligence Console",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #0e1117; }
      .pi-card { background:#161b26; border:1px solid #262d3d; border-radius:10px;
                 padding:14px 18px; margin-bottom:12px; }
      .pi-metric { font-size:1.9rem; font-weight:700; color:#e6edf3; line-height:1.1; }
      .pi-label { font-size:.72rem; text-transform:uppercase; letter-spacing:.09em; color:#7d8795; }
      .pi-delta-up { color:#3fb950; font-size:.82rem; font-weight:600; }
      .pi-delta-flat { color:#7d8795; font-size:.82rem; }
      .pi-chip { display:inline-block; padding:2px 9px; border-radius:20px; font-size:.66rem;
                 font-weight:700; letter-spacing:.04em; margin-right:6px; }
      .chip-src { background:#12301f; color:#56d364; }
      .chip-inf { background:#132b45; color:#6cb6ff; }
      .chip-gen { background:#2d2416; color:#e3b341; }
      .chip-crit{ background:#3d1a1c; color:#ff7b72; }
      .chip-warn{ background:#33290f; color:#e3b341; }
      .pi-quote { border-left:3px solid #30475e; padding:6px 12px; margin:6px 0 10px 0;
                  color:#9aa5b1; font-size:.8rem; font-family:ui-monospace,monospace;
                  background:#11151d; border-radius:0 6px 6px 0; }
      .pi-loc { color:#6e7681; font-size:.72rem; font-family:ui-monospace,monospace; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource(show_spinner=False)
def get_engine() -> ProductIntelligenceEngine:
    return ProductIntelligenceEngine()


@st.cache_data(ttl=20, show_spinner=False)
def load_products():
    return CatalogStore().load_all()


def chip(kind: str, label: str) -> str:
    return f'<span class="pi-chip chip-{kind}">{label}</span>'


def origin_of(av, attr) -> tuple[str, str]:
    if attr is not None and attr.generated:
        return "gen", "GENERATED"
    if av.inference is not None:
        return "inf", "INFERRED"
    return "src", "SOURCED"


def metric_card(label: str, value: str, delta: str | None = None) -> str:
    delta_html = ""
    if delta:
        cls = "pi-delta-up" if delta.startswith("+") and delta != "+0.0%" else "pi-delta-flat"
        delta_html = f'<div class="{cls}">{delta}</div>'
    return (
        f'<div class="pi-card"><div class="pi-label">{label}</div>'
        f'<div class="pi-metric">{value}</div>{delta_html}</div>'
    )


def render_llm_toggle() -> None:
    """
    Offline / cloud switch.

    The choice is written to settings.json so it survives a restart, and the
    cached engine is dropped so the next action picks up the new backend.
    """
    from product_intel.config import reload_settings, save_settings
    from product_intel.llm.provider import (
        OLLAMA_SUGGESTED_MODELS,
        OPENROUTER_SUGGESTED_MODELS,
        LLMUnavailable,
        get_provider,
    )

    cfg = reload_settings()
    st.markdown('<div class="pi-label">AI backend</div>', unsafe_allow_html=True)

    modes = ["Offline (Ollama)", "Cloud (OpenRouter)", "Off (deterministic)"]
    current = (
        2 if not cfg.llm_enabled or cfg.llm_provider == "null"
        else 1 if cfg.llm_provider == "openrouter"
        else 0
    )
    chosen = st.radio("AI backend", modes, index=current, label_visibility="collapsed")
    target = {0: "ollama", 1: "openrouter", 2: "off"}[modes.index(chosen)]

    if modes.index(chosen) != current:
        updates = (
            {"llm_enabled": False, "llm_provider": "null"}
            if target == "off"
            else {"llm_enabled": True, "llm_provider": target, "llm_model": None}
        )
        save_settings(updates)
        st.cache_resource.clear()
        st.cache_data.clear()
        st.rerun()

    cfg = reload_settings()

    if target != "off":
        suggestions = (
            OPENROUTER_SUGGESTED_MODELS if target == "openrouter" else OLLAMA_SUGGESTED_MODELS
        )
        names = [n for n, _ in suggestions]
        active = cfg.active_model
        if active not in names:
            names.insert(0, active)
        picked = st.selectbox(
            "Model", names, index=names.index(active),
            help="\n".join(f"{n} — {d}" for n, d in suggestions),
        )
        if picked != active:
            save_settings({f"{target}_model": picked, "llm_model": None})
            st.cache_resource.clear()
            st.rerun()

    probe = get_provider(cfg)
    if not cfg.llm_enabled:
        st.info("Deterministic extraction only. Coverage is lower but nothing is fabricated.")
    elif probe.available:
        where = "on this machine" if cfg.is_offline else "via OpenRouter"
        st.success(f"Ready — {where}")
    elif target == "ollama":
        st.warning("Ollama is not responding.")
        st.code("ollama serve\nollama pull " + cfg.active_model, language="bash")
    else:
        st.warning(f"No API key in ${cfg.api_key_env_for()}")
        st.code(f"{cfg.api_key_env_for()}=sk-or-v1-...", language="bash")
        st.caption("Add it to the `.env` file at the project root, then restart this app.")

    if cfg.llm_enabled and st.button("Test connection", use_container_width=True):
        with st.spinner("Probing..."):
            try:
                get_provider(cfg).complete_json(
                    'Reply with exactly: {"ok": true}', expect="object"
                )
                st.success("Connection OK")
            except LLMUnavailable as exc:
                st.error(str(exc)[:300])

    st.caption(
        ("🔒 Fully offline — no data leaves this machine"
         if cfg.is_offline else
         f"☁️ Sending document text to {cfg.llm_provider}")
    )


engine = get_engine()
products = load_products()

with st.sidebar:
    st.markdown("### ◆ Product Intelligence")
    st.caption("Evidence-grounded catalog engine")

    # The backend toggle renders before the empty-catalog guard: choosing the
    # backend is exactly what you want to do *before* the first ingest.
    render_llm_toggle()
    st.divider()

    if not products:
        st.warning("Catalog is empty.")
        st.caption("Generate the sample catalog and build it:")
        st.code(
            "python scripts/generate_sample_catalog.py --out Sources\n"
            "python -m product_intel.cli run Sources",
            language="bash",
        )
        st.stop()

    view = st.radio(
        "View",
        ["Scorecard", "Products", "Review queue", "Search"],
        label_visibility="collapsed",
    )
    st.divider()
    card = catalog_scorecard(products)
    st.metric("Products", card["products"])
    st.metric("Channel-ready", f"{card['channel_ready']} / {card['sellable']}")
    st.metric("Open review items", ReviewQueue().stats()["open"])


# ---------------------------------------------------------------------------
if view == "Scorecard":
    st.markdown("## Catalog quality")
    after = catalog_scorecard(products)
    before_scores = [p.quality_before_enrichment for p in products if p.quality_before_enrichment]

    def before_mean(field: str) -> float:
        if not before_scores:
            return 0.0
        return sum(getattr(q, field) for q in before_scores) / len(before_scores)

    cols = st.columns(4)
    rows = [
        ("Completeness (ecommerce)", "completeness_ecommerce"),
        ("Accuracy", "accuracy"),
        ("Consistency", "consistency"),
        ("Overall", "overall"),
    ]
    for col, (label, key) in zip(cols, rows):
        b, a = before_mean(key), after[key]
        col.markdown(
            metric_card(label, f"{a * 100:.1f}%", f"{(a - b) * 100:+.1f}%" if before_scores else None),
            unsafe_allow_html=True,
        )

    cols = st.columns(4)
    cols[0].markdown(metric_card("Attributes sourced", str(after["attributes_sourced"])), unsafe_allow_html=True)
    cols[1].markdown(metric_card("Attributes generated", str(after["attributes_generated"])), unsafe_allow_html=True)
    cols[2].markdown(metric_card("Values inferred", str(after["inferred_attributes"])), unsafe_allow_html=True)
    cols[3].markdown(metric_card("Conflicts recorded", str(after["conflicts"])), unsafe_allow_html=True)

    st.markdown("### Confidence")
    c1, c2 = st.columns(2)
    c1.markdown(metric_card("Sourced attributes", f"{after['confidence_sourced']:.2f}"), unsafe_allow_html=True)
    c2.markdown(metric_card("Generated content", f"{after['confidence_generated']:.2f}"), unsafe_allow_html=True)
    st.caption(
        "Reported separately on purpose: authored copy is expected to score below a value "
        "read from a spec table, and blending them hides the number that matters."
    )

    st.markdown("### Completeness by category")
    by_cat: dict = {}
    for p in products:
        by_cat.setdefault(p.category_id, []).append(p)
    table = []
    for cid, group in sorted(by_cat.items()):
        table.append(
            {
                "Category": engine.taxonomy.get(cid).name,
                "Products": len(group),
                "Completeness": sum(g.quality.completeness_ecommerce for g in group) / len(group),
                "Accuracy": sum(g.quality.accuracy for g in group) / len(group),
                "Channel-ready": sum(1 for g in group if g.quality.channel_ready),
            }
        )
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Completeness": st.column_config.ProgressColumn("Completeness", min_value=0, max_value=1, format="%.0f%%"),
            "Accuracy": st.column_config.ProgressColumn("Accuracy", min_value=0, max_value=1, format="%.0f%%"),
        },
    )


# ---------------------------------------------------------------------------
elif view == "Products":
    left, right = st.columns([1, 2.4])

    with left:
        st.markdown("#### Catalog")
        mfrs = sorted({p.identity.manufacturer for p in products})
        chosen_mfr = st.selectbox("Manufacturer", ["All"] + mfrs)
        pool = [p for p in products if chosen_mfr == "All" or p.identity.manufacturer == chosen_mfr]
        pool.sort(key=lambda p: p.identity.mpn)
        labels = {f"{p.identity.mpn} — {engine.taxonomy.get(p.category_id).name}": p for p in pool}
        chosen = st.radio("Product", list(labels), label_visibility="collapsed")
        product = labels[chosen]

    with right:
        schema = engine.schema_for(product)
        st.markdown(f"## {product.identity.manufacturer} {product.identity.mpn}")
        st.caption(
            f"{product.display_name()} · {schema.name} · "
            f"ETIM {schema.etim or 'n/a'} / UNSPSC {schema.unspsc or 'n/a'}"
        )

        q = product.quality
        cols = st.columns(4)
        cols[0].markdown(metric_card("Completeness", f"{q.completeness_ecommerce * 100:.0f}%"), unsafe_allow_html=True)
        cols[1].markdown(metric_card("Accuracy", f"{q.accuracy * 100:.0f}%"), unsafe_allow_html=True)
        cols[2].markdown(metric_card("Overall", f"{q.overall * 100:.0f}%"), unsafe_allow_html=True)
        cols[3].markdown(
            metric_card("Status", (product.status.value if hasattr(product.status, "value") else str(product.status)).replace("_", " ")),
            unsafe_allow_html=True,
        )

        if q.missing_required:
            st.warning(f"Missing for the {settings.target_channel} channel: {', '.join(q.missing_required)}")

        if product.conflicts:
            st.markdown("#### Conflicts")
            for c in product.conflicts:
                kind = "crit" if c.severity == "critical" else "warn"
                st.markdown(
                    f'{chip(kind, c.severity.upper())} <b>{c.code}</b> — kept '
                    f'<code>{c.winning_value}</code>',
                    unsafe_allow_html=True,
                )
                st.caption(f"Resolved by {c.resolution_rule}")
                for loser in c.losing_values[:3]:
                    st.markdown(
                        f'<div class="pi-quote">rejected <b>{loser["value"]}</b> '
                        f'from {loser["source_kind"]} · {loser["locator"] or "n/a"}<br>{loser["quote"] or ""}</div>',
                        unsafe_allow_html=True,
                    )

        st.markdown("#### Attributes")
        show_gen = st.checkbox("Include generated content", value=False)

        for code in sorted(product.attributes):
            av = product.attributes[code]
            attr = schema.get(code)
            kind, label = origin_of(av, attr)
            if kind == "gen" and not show_gen:
                continue
            name = attr.name if attr else code
            colour = "#3fb950" if av.confidence >= 0.85 else ("#e3b341" if av.confidence >= 0.7 else "#ff7b72")

            with st.expander(f"{name} — {av.display()[:80]}", expanded=False):
                st.markdown(
                    chip(kind, label)
                    + f'<span style="color:{colour};font-weight:700">confidence {av.confidence:.2f}</span>',
                    unsafe_allow_html=True,
                )
                if av.evidence is not None:
                    ev = av.evidence
                    verified = "verified" if ev.quote_verified else "UNVERIFIED"
                    st.markdown(
                        f'<div class="pi-loc">{ev.source_id} · {ev.locator} · '
                        f'{ev.source_kind.value} · {ev.method.value} · {verified}</div>',
                        unsafe_allow_html=True,
                    )
                    if ev.quote:
                        st.markdown(f'<div class="pi-quote">{ev.quote}</div>', unsafe_allow_html=True)
                if av.inference is not None:
                    st.info(f"**{av.inference.strategy}** — {av.inference.rationale}")
                if av.normalization_notes:
                    st.caption(" · ".join(av.normalization_notes))
                if av.validation_errors:
                    st.error(" · ".join(av.validation_errors))
                if av.raw_value and av.raw_value != str(av.value):
                    st.caption(f"as written in the source: `{av.raw_value}`")


# ---------------------------------------------------------------------------
elif view == "Review queue":
    st.markdown("## Review queue")
    st.caption("Ordered by reason weight × severity × (1 − confidence): the items where a reviewer's attention is worth most.")

    queue = ReviewQueue()
    flags = queue.prioritized(limit=200)
    if not flags:
        st.success("Nothing needs review.")
        st.stop()

    by_id = {p.identity.product_id: p for p in products}
    reasons = sorted({f.reason.split(":")[0] for f in flags})
    chosen_reasons = st.multiselect("Filter by reason", reasons, default=reasons)
    flags = [f for f in flags if f.reason.split(":")[0] in chosen_reasons]

    st.caption(f"{len(flags)} item(s)")
    for flag in flags[:60]:
        product = by_id.get(flag.product_id)
        if product is None:
            continue
        kind = "crit" if flag.severity == "critical" else "warn"
        with st.container():
            st.markdown(
                chip(kind, flag.severity.upper())
                + f"<b>{product.identity.manufacturer} {product.identity.mpn}</b>"
                + f' <span class="pi-loc">{flag.attribute_code or "product-level"}</span>',
                unsafe_allow_html=True,
            )
            st.caption(flag.reason)

            if flag.attribute_code and flag.attribute_code in product.attributes:
                av = product.attributes[flag.attribute_code]
                if av.evidence and av.evidence.quote:
                    st.markdown(
                        f'<div class="pi-quote">{av.evidence.quote[:300]}<br>'
                        f'<span class="pi-loc">{av.evidence.source_id} · {av.evidence.locator}</span></div>',
                        unsafe_allow_html=True,
                    )

            if flag.attribute_code:
                c1, c2 = st.columns([3, 1])
                new_value = c1.text_input(
                    "Corrected value",
                    value="" if flag.suggested_value is None else str(flag.suggested_value),
                    key=f"v_{flag.flag_id}",
                    label_visibility="collapsed",
                )
                if c2.button("Apply", key=f"b_{flag.flag_id}", use_container_width=True):
                    schema = engine.schema_for(product)
                    old = product.attributes.get(flag.attribute_code)
                    av = apply_correction(
                        product, flag.attribute_code, new_value, schema, reviewer="console"
                    )
                    if av.value is None:
                        st.error("; ".join(av.validation_errors))
                    else:
                        learned = LearnedRules()
                        promoted = learned.learn_from_correction(
                            product, flag.attribute_code,
                            old.value if old else None, av.value, "console",
                        )
                        learned.flush()
                        queue.resolve(flag.flag_id, f"corrected to {av.value}", "console")
                        queue.flush()
                        engine.store.save_product(product)
                        st.cache_data.clear()
                        st.success(f"Set to {av.value}")
                        if promoted:
                            st.info(f"Learned: {promoted}")
                        st.rerun()
            st.divider()


# ---------------------------------------------------------------------------
elif view == "Search":
    st.markdown("## Search")
    st.caption(
        "Attribute filters resolve to parameterised SQL; anything left over falls back to "
        "semantic similarity. Every value is shown with the source it was read from."
    )

    query = st.text_input(
        "Query",
        placeholder="3 pole circuit breaker rated at least 30A",
        label_visibility="collapsed",
    )
    examples = [
        "3 pole circuit breaker rated at least 30A",
        "stainless steel ball valve 1 inch NPT",
        "blower with at least 1000 CFM",
    ]
    cols = st.columns(len(examples))
    for col, example in zip(cols, examples):
        if col.button(example, use_container_width=True):
            query = example

    if query:
        result = answer(query, products, engine.taxonomy, limit=12)
        st.code(f"{result['plan']['mode']}: {result['plan']['explanation']}", language=None)

        if not result["results"]:
            st.warning(result["answer"])
        else:
            for r in result["results"]:
                with st.container():
                    st.markdown(f"**{r['manufacturer']} {r['mpn']}** — {r['category']}")
                    st.caption(f"{r['name']} · quality {r['quality']:.2f} · {r['status']}")
                    if r["attributes"]:
                        st.table([{"Attribute": k, "Value": v} for k, v in r["attributes"].items()])

            with st.expander(f"Evidence — {len(result['citations'])} citations"):
                for c in result["citations"]:
                    mark = "" if c["verified"] else " ⚠ unverified"
                    st.markdown(
                        f'**{c["product"]}** · {c["attribute"]} = `{c["value"]}`'
                        f'<div class="pi-loc">{c["source_id"]} · {c["locator"]}{mark}</div>'
                        f'<div class="pi-quote">{(c["quote"] or "")[:260]}</div>',
                        unsafe_allow_html=True,
                    )
