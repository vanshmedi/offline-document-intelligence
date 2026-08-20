/** Overview: the before/after quality story, and where the gaps are. */

import { api, badge, bar, delta, el, mount, num, pct } from '../core.js';

const AXES = [
  ['completeness_core', 'Completeness (core)'],
  ['completeness_ecommerce', 'Completeness (ecommerce)'],
  ['completeness_enhanced', 'Completeness (enhanced)'],
  ['accuracy', 'Accuracy'],
  ['consistency', 'Consistency'],
  ['distinctiveness', 'Distinctiveness'],
  ['overall', 'Overall'],
];

function metric(label, value, sub, deltaInfo) {
  return el('div', { class: 'metric' },
    el('div', { class: 'metric-label' }, label),
    el('div', { class: 'metric-value' }, value),
    el('div', { class: 'metric-sub' },
      sub,
      deltaInfo ? el('span', { class: `metric-delta ${deltaInfo.cls}`, style: { marginLeft: '8px' } }, deltaInfo.text) : null,
    ),
  );
}

export async function overviewView(_params, root) {
  const data = await api.overview();
  const s = data.scorecard;
  const before = data.scorecard_before;

  if (!data.catalog_built && !s.products) {
    mount(root, el('div', { class: 'empty' },
      el('h3', {}, 'The catalog is empty'),
      el('p', {}, 'Generate the sample catalog, then run the pipeline.'),
      el('div', { class: 'code' },
        'python scripts/generate_sample_catalog.py --out Sources\npython -m product_intel.cli run Sources'),
      el('p', { style: { marginTop: '16px' } }, 'Or run it from here:'),
      el('button', {
        class: 'btn btn-primary',
        onClick: () => document.getElementById('btn-run').click(),
      }, 'Run pipeline'),
    ));
    return;
  }

  const readyDelta = before
    ? { cls: s.channel_ready > before.channel_ready ? 'up' : 'flat',
        text: `+${s.channel_ready - before.channel_ready}` }
    : null;

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Catalog overview'),
        el('div', { class: 'page-sub' },
          `${num(s.sellable)} sellable SKUs and ${num(s.families)} family records across ` +
          `${data.by_category.length} categories, from ${num(data.sources)} source documents`),
      ),
    ),

    el('div', { class: 'grid grid-4' },
      metric('Channel-ready', `${num(s.channel_ready)}`,
        `of ${num(s.sellable)} sellable (${s.channel_ready_pct.toFixed(0)}%)`, readyDelta),
      metric('Completeness', pct(s.completeness_ecommerce, 0), 'ecommerce channel',
        before ? delta(s.completeness_ecommerce, before.completeness_ecommerce) : null),
      metric('Accuracy', pct(s.accuracy, 0), 'attributes with a verified quote',
        before ? delta(s.accuracy, before.accuracy) : null),
      metric('Open review items', num(data.review.open || 0),
        `across ${num(data.review.products_affected || 0)} products`),
    ),

    el('h2', { class: 'section' }, before ? 'Quality: before and after enrichment' : 'Quality'),
    el('div', { class: 'panel' },
      el('div', { class: 'panel-body' },
        ...AXES.map(([key, label]) => {
          const after = s[key] ?? 0;
          const prior = before ? before[key] ?? 0 : null;
          const d = before ? delta(after, prior) : null;
          return el('div', { class: 'bar-row' },
            el('div', { class: 'bar-label' }, label),
            bar(after, prior),
            el('div', { class: 'bar-nums' },
              pct(after, 0),
              d ? el('span', { class: `metric-delta ${d.cls}`, style: { marginLeft: '7px' } }, d.text) : null,
            ),
          );
        }),
        before
          ? el('div', { class: 'legend', style: { marginTop: '12px' } },
              el('span', {}, el('i', { style: { background: 'var(--line-strong)' } }), 'before enrichment'),
              el('span', {}, el('i', { style: { background: 'var(--accent)' } }), 'after enrichment'))
          : null,
      ),
    ),

    el('div', { class: 'grid grid-2', style: { marginTop: '14px' } },
      el('div', { class: 'panel' },
        el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Attribute provenance')),
        el('div', { class: 'panel-body' },
          el('dl', { class: 'kv' },
            el('dt', {}, 'Read from a source'), el('dd', {}, num(s.attributes_sourced),
              el('span', { style: { color: 'var(--text-faint)', marginLeft: '8px' } },
                `mean confidence ${(s.confidence_sourced || 0).toFixed(2)}`)),
            el('dt', {}, 'Inferred'), el('dd', {}, num(s.inferred_attributes),
              el('span', { style: { color: 'var(--text-faint)', marginLeft: '8px' } }, 'from the product family')),
            el('dt', {}, 'Generated'), el('dd', {}, num(s.attributes_generated),
              el('span', { style: { color: 'var(--text-faint)', marginLeft: '8px' } },
                `mean confidence ${(s.confidence_generated || 0).toFixed(2)}`)),
            el('dt', {}, 'Conflicts recorded'), el('dd', {}, num(s.conflicts)),
            el('dt', {}, 'Knowledge graph'), el('dd', {},
              `${num(data.graph.nodes || 0)} nodes, ${num(data.graph.edges || 0)} edges`),
          ),
        ),
      ),

      el('div', { class: 'panel' },
        el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Publishing status')),
        el('div', { class: 'panel-body' },
          el('dl', { class: 'kv' },
            ...Object.entries(data.by_status).sort((a, b) => b[1] - a[1]).flatMap(([status, count]) => [
              el('dt', {}, statusBadge(status)),
              el('dd', {}, num(count)),
            ]),
          ),
        ),
      ),
    ),

    el('h2', { class: 'section' }, 'Where the gaps are'),
    el('div', { class: 'panel' },
      el('div', { class: 'panel-head' },
        el('div', { class: 'panel-title' }, 'Required attribute coverage'),
        el('div', { style: { fontSize: '11.5px', color: 'var(--text-faint)' } },
          'lowest first — this is what to chase the manufacturer for'),
      ),
      el('div', { class: 'panel-body flush' },
        el('div', { class: 'table-wrap' },
          el('table', { class: 'table' },
            el('thead', {}, el('tr', {},
              el('th', {}, 'Attribute'), el('th', {}, 'Coverage'),
              el('th', { class: 'num' }, 'Filled'), el('th', { class: 'num' }, 'Applicable'))),
            el('tbody', {},
              ...data.attribute_coverage.slice(0, 14).map((row) =>
                el('tr', {},
                  el('td', {}, row.name),
                  el('td', {}, el('div', { style: { display: 'flex', alignItems: 'center', gap: '10px' } },
                    el('div', { style: { flex: '1', minWidth: '90px' } }, bar(row.coverage)),
                    el('span', { class: 'bar-nums', style: { minWidth: '44px' } }, pct(row.coverage, 0)))),
                  el('td', { class: 'num' }, num(row.filled)),
                  el('td', { class: 'num' }, num(row.applicable)),
                )),
            ),
          ),
        ),
      ),
    ),

    el('div', { class: 'grid grid-2', style: { marginTop: '14px' } },
      breakdownPanel('By category', data.by_category, 'name', (r) =>
        `${r.products} products · ${r.channel_ready} ready`, (r) => r.completeness),
      breakdownPanel('By manufacturer', data.by_manufacturer, 'manufacturer', (r) =>
        `${r.products} products · ${r.channel_ready} ready`, (r) => r.completeness),
    ),
  );
}

function breakdownPanel(title, rows, labelKey, subFn, valueFn) {
  return el('div', { class: 'panel' },
    el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, title)),
    el('div', { class: 'panel-body' },
      ...rows.map((row) =>
        el('div', { class: 'bar-row' },
          el('div', {},
            el('div', { class: 'bar-label', style: { color: 'var(--text)' } }, row[labelKey]),
            el('div', { style: { fontSize: '11px', color: 'var(--text-faint)' } }, subFn(row))),
          bar(valueFn(row)),
          el('div', { class: 'bar-nums' }, pct(valueFn(row), 0)),
        )),
    ),
  );
}

export function statusBadge(status) {
  const map = {
    published: ['ok', 'published'],
    enriched: ['neutral', 'enriched'],
    needs_review: ['warn', 'needs review'],
    draft: ['neutral', 'draft'],
    failed: ['danger', 'failed'],
  };
  const [kind, label] = map[status] || ['neutral', status];
  return badge(label, kind);
}
