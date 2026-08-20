/**
 * Product browser and the product detail page.
 *
 * The detail page is the centre of the whole product: every attribute row
 * expands to show the source, the locator and the verbatim quote, and the
 * quote links through to the document mirror with the passage highlighted.
 */

import {
  api, badge, confidence, el, frag, mount, navigate, num, openDrawer, pct, titleCase, toast,
} from '../core.js';
import { statusBadge } from './overview.js';

const ORIGIN_LABEL = {
  sourced: 'source',
  inferred: 'inferred',
  generated: 'generated',
  human: 'corrected',
  default: 'default',
};

const ORIGIN_TITLE = {
  sourced: 'Read from a source document. Expand to see the exact quote.',
  inferred: 'Not stated for this product. Derived from its family — expand for the reasoning.',
  generated: 'Authored from this product\'s verified attributes.',
  human: 'Corrected by a reviewer. Outranks every automated source.',
  default: 'Schema default. No source stated a value.',
};

let filters = { q: '', category: '', manufacturer: '', status: '', ready: '', families: '', sort: 'mpn' };

/* ------------------------------------------------------------------ list */

export async function productsView(_params, root) {
  const [overview] = await Promise.all([api.overview()]);

  const controls = el('div', { class: 'toolbar' });
  const results = el('div', {});

  async function refresh() {
    mount(results, el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Loading…'));
    const params = { limit: 500, sort: filters.sort };
    if (filters.q) params.q = filters.q;
    if (filters.category) params.category = filters.category;
    if (filters.manufacturer) params.manufacturer = filters.manufacturer;
    if (filters.status) params.status = filters.status;
    if (filters.ready !== '') params.ready = filters.ready === 'true';
    if (filters.families !== '') params.families = filters.families === 'true';

    const data = await api.products(params);
    mount(results, productTable(data));
  }

  const search = el('input', {
    class: 'input', type: 'search', placeholder: 'Filter by part number, manufacturer or name…',
    value: filters.q,
  });
  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    filters.q = search.value;
    debounce = setTimeout(refresh, 220);
  });

  const select = (key, label, options) => {
    const node = el('select', { class: 'select', style: { width: 'auto' } },
      el('option', { value: '' }, label),
      ...options.map(([value, text]) => el('option', { value, selected: filters[key] === value }, text)),
    );
    node.addEventListener('change', () => { filters[key] = node.value; refresh(); });
    return node;
  };

  mount(controls,
    el('div', { class: 'grow' }, search),
    select('category', 'All categories', overview.by_category.map((c) => [c.category_id, `${c.name} (${c.products})`])),
    select('manufacturer', 'All manufacturers', overview.by_manufacturer.map((m) => [m.manufacturer, m.manufacturer])),
    select('status', 'Any status', Object.keys(overview.by_status).map((s) => [s, titleCase(s)])),
    select('ready', 'Any readiness', [['true', 'Channel-ready'], ['false', 'Not ready']]),
    select('families', 'SKUs and families', [['false', 'Sellable SKUs only'], ['true', 'Family records only']]),
    select('sort', 'Sort: part number', [
      ['quality', 'Sort: quality'], ['completeness', 'Sort: completeness'],
      ['flags', 'Sort: review flags'], ['conflicts', 'Sort: conflicts'],
    ]),
  );

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Products'),
        el('div', { class: 'page-sub' }, 'Every attribute traces to the line it came from'),
      ),
    ),
    controls,
    results,
  );

  await refresh();
}

function productTable(data) {
  if (!data.items.length) {
    return el('div', { class: 'empty' },
      el('h3', {}, 'Nothing matches those filters'),
      el('p', {}, 'Try widening the search.'));
  }

  return el('div', { class: 'panel' },
    el('div', { class: 'panel-head' },
      el('div', { class: 'panel-title' }, `${num(data.total)} products`),
      data.total > data.items.length
        ? el('div', { style: { fontSize: '11.5px', color: 'var(--text-faint)' } },
            `showing first ${data.items.length}`)
        : null,
    ),
    el('div', { class: 'panel-body flush' },
      el('div', { class: 'table-wrap' },
        el('table', { class: 'table' },
          el('thead', {}, el('tr', {},
            el('th', {}, 'Part number'), el('th', {}, 'Manufacturer'), el('th', {}, 'Category'),
            el('th', {}, 'Status'), el('th', { class: 'num' }, 'Complete'),
            el('th', {}, 'Quality'), el('th', { class: 'num' }, 'Flags'),
          )),
          el('tbody', {},
            ...data.items.map((p) =>
              el('tr', { onClick: () => navigate(`/products/${encodeURIComponent(p.product_id)}`) },
                el('td', {},
                  el('div', { class: 'mono', style: { fontWeight: '550' } }, p.mpn),
                  p.is_family ? badge('family', 'neutral') : null,
                  p.suspected_duplicate_of ? badge('possible duplicate', 'danger') : null,
                ),
                el('td', { style: { color: 'var(--text-dim)' } }, p.manufacturer),
                el('td', { style: { color: 'var(--text-dim)' } }, p.category_name),
                el('td', {}, statusBadge(p.status), p.channel_ready ? badge('ready', 'ok') : null),
                el('td', { class: 'num' }, pct(p.completeness, 0)),
                el('td', {}, confidence(p.quality_overall)),
                el('td', { class: 'num' },
                  p.open_flags ? badge(String(p.open_flags), 'warn') : el('span', { style: { color: 'var(--text-faint)' } }, '—')),
              )),
          ),
        ),
      ),
    ),
  );
}

/* ---------------------------------------------------------------- detail */

export async function productDetailView(params, root) {
  const p = await api.product(params.id);

  const identity = [
    ['Manufacturer', p.manufacturer],
    ['Part number', p.mpn],
    p.gtin ? ['GTIN', p.gtin] : null,
    p.series ? ['Series', p.series] : null,
    ['Category', `${p.category_name} (${p.category_id})`],
    p.etim ? ['ETIM class', p.etim] : null,
    p.unspsc ? ['UNSPSC', p.unspsc] : null,
    p.alternate_mpns.length ? ['Also seen as', p.alternate_mpns.join(', ')] : null,
  ].filter(Boolean);

  mount(root,
    el('div', { class: 'breadcrumb' },
      el('a', { href: '/products' }, 'Products'), ' / ', p.mpn),

    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, p.name),
        el('div', { class: 'page-sub' },
          el('span', { class: 'mono' }, p.mpn), ' · ', p.manufacturer, ' · ', p.category_name),
        el('div', { style: { marginTop: '8px', display: 'flex', gap: '6px', flexWrap: 'wrap' } },
          statusBadge(p.status),
          p.channel_ready ? badge('channel-ready', 'ok') : badge('not publishable', 'warn'),
          p.is_family ? badge('family record', 'neutral') : null,
          p.conflicts.length ? badge(`${p.conflicts.length} conflict${p.conflicts.length > 1 ? 's' : ''}`, 'danger') : null,
          p.open_flags ? badge(`${p.open_flags} review item${p.open_flags > 1 ? 's' : ''}`, 'warn') : null,
        ),
      ),
      el('div', { style: { textAlign: 'right' } },
        el('div', { class: 'metric-label' }, 'Quality'),
        el('div', { class: 'metric-value' }, pct(p.quality.overall, 0)),
      ),
    ),

    p.suspected_duplicate_of
      ? el('div', { class: 'callout danger', style: { marginBottom: '14px' } },
          el('strong', {}, 'Possible duplicate. '), p.duplicate_evidence || '')
      : null,

    p.quality.missing_required.length
      ? el('div', { class: 'callout warn', style: { marginBottom: '14px' } },
          el('strong', {}, `Not publishable — ${p.quality.missing_required.length} required attribute(s) missing: `),
          p.quality.missing_required.map(titleCase).join(', '))
      : null,

    p.conflicts.length ? conflictsPanel(p) : null,

    el('h2', { class: 'section' }, `Attributes (${p.attributes.length})`),
    el('div', { class: 'legend', style: { marginBottom: '10px' } },
      ...['sourced', 'inferred', 'generated', 'human'].map((k) =>
        el('span', {}, el('i', { style: { background: `var(--${k === 'human' ? 'human' : k})` } }), ORIGIN_LABEL[k])),
    ),
    attributesPanel(p),

    el('div', { class: 'grid grid-2', style: { marginTop: '18px' } },
      el('div', { class: 'panel' },
        el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Identity')),
        el('div', { class: 'panel-body' },
          el('dl', { class: 'kv' },
            ...identity.flatMap(([k, v]) => [el('dt', {}, k), el('dd', { class: k === 'Part number' ? 'mono' : '' }, v)]))),
      ),
      el('div', { class: 'panel' },
        el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Quality detail')),
        el('div', { class: 'panel-body' },
          el('dl', { class: 'kv' },
            el('dt', {}, 'Completeness (core)'), el('dd', {}, pct(p.quality.completeness_core, 0)),
            el('dt', {}, 'Completeness (ecommerce)'), el('dd', {}, pct(p.quality.completeness_ecommerce, 0)),
            el('dt', {}, 'Accuracy'), el('dd', {}, pct(p.quality.accuracy, 0)),
            el('dt', {}, 'Consistency'), el('dd', {}, pct(p.quality.consistency, 0)),
            el('dt', {}, 'Distinctiveness'), el('dd', {}, pct(p.quality.distinctiveness, 0)),
            el('dt', {}, 'Category confidence'), el('dd', {}, p.category_confidence.toFixed(2)),
          )),
      ),
    ),

    p.sources.length ? sourcesPanel(p) : null,
    p.relations.length ? relationsPanel(p) : null,
    p.assets.length ? assetsPanel(p) : null,
  );
}

function attributesPanel(p) {
  const groups = { identity: [], specs: [], other: [], generated: [] };
  for (const a of p.attributes) {
    if (['manufacturer', 'mpn', 'gtin', 'series'].includes(a.code)) groups.identity.push(a);
    else if (a.origin === 'generated') groups.generated.push(a);
    else if (a.variant_defining || a.required_for.includes('core')) groups.specs.push(a);
    else groups.other.push(a);
  }

  const panel = el('div', { class: 'panel' });
  const body = el('div', { class: 'panel-body flush' });

  const section = (label, items) => {
    if (!items.length) return;
    body.appendChild(el('div', { class: 'attr-group-head' }, label));
    items.forEach((a) => body.appendChild(attributeRow(p, a)));
  };

  section('Identity', groups.identity);
  section('Key specifications', groups.specs);
  section('Additional attributes', groups.other);
  section('Generated content', groups.generated);

  panel.appendChild(body);
  return panel;
}

function attributeRow(product, a) {
  const wrap = el('div', {});
  let expanded = false;

  const row = el('div', {
    class: `attr${a.validation_errors.length ? ' has-error' : ''}`,
    onClick: () => {
      expanded = !expanded;
      chevron.textContent = expanded ? '▾' : '▸';
      if (expanded) wrap.appendChild(detail);
      else detail.remove();
    },
  });

  const chevron = el('div', { class: 'attr-chevron' }, '▸');

  row.append(
    el('div', { class: 'attr-name' },
      a.name,
      a.required_for.includes('core') ? el('span', { class: 'req', title: 'required' }, '*') : null),
    el('div', { class: `attr-value${a.display ? '' : ' empty'}` }, a.display || 'not stated'),
    el('div', { class: 'attr-origin' }, badge(ORIGIN_LABEL[a.origin] || a.origin, a.origin, ORIGIN_TITLE[a.origin])),
    confidence(a.confidence),
    chevron,
  );

  const detail = buildDetail(product, a);
  wrap.appendChild(row);
  return wrap;
}

function buildDetail(product, a) {
  const node = el('div', { class: 'attr-detail' });

  if (a.evidence && a.origin !== 'generated') {
    const ev = a.evidence;
    node.append(
      el('div', { class: 'meta-line' },
        el('strong', {}, ev.source_name || ev.source_id), ' · ', ev.locator,
        ev.page ? ` · page ${ev.page}` : '',
        ' · ', ev.method.replace(/_/g, ' '),
        ev.quote_verified
          ? el('span', { style: { color: 'var(--sourced)', marginLeft: '8px' } }, '✓ quote verified')
          : el('span', { style: { color: 'var(--warn)', marginLeft: '8px' } }, '⚠ quote not verified'),
      ),
      el('div', { class: `quote${ev.quote_verified ? '' : ' unverified'}` }, ev.quote),
      el('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } },
        el('button', {
          class: 'btn btn-sm',
          onClick: (e) => { e.stopPropagation(); showMirror(ev); },
        }, 'View in source document'),
        el('button', {
          class: 'btn btn-sm btn-ghost',
          onClick: (e) => { e.stopPropagation(); showObservations(product, a.code); },
        }, `All ${a.observation_count} observation${a.observation_count > 1 ? 's' : ''}`),
      ),
    );
  }

  if (a.inference) {
    node.append(
      el('div', { class: 'meta-line' },
        el('strong', {}, 'Inferred: '), a.inference.strategy.replace(/_/g, ' '),
        a.inference.from_product_mpn ? ` from ${a.inference.from_product_mpn}` : ''),
      el('div', { class: 'quote inferred' }, a.inference.rationale),
    );
  }

  if (a.raw_value && a.raw_value !== a.display) {
    node.append(el('div', { class: 'meta-line' },
      el('strong', {}, 'As written: '), el('span', { class: 'mono' }, a.raw_value)));
  }
  if (a.normalization_notes.length) {
    node.append(el('div', { class: 'meta-line' },
      el('strong', {}, 'Normalization: '), a.normalization_notes.join('; ')));
  }
  if (a.validation_errors.length) {
    node.append(el('div', { class: 'callout danger', style: { marginTop: '8px' } },
      a.validation_errors.join('; ')));
  }
  if (a.confidence_reasons.length) {
    node.append(el('div', { class: 'meta-line', style: { marginTop: '8px' } },
      el('strong', {}, `Confidence ${a.confidence.toFixed(2)}: `), a.confidence_reasons.join('; ')));
  }
  if (!node.childNodes.length) {
    node.append(el('div', { class: 'meta-line' }, 'No further provenance recorded for this value.'));
  }
  return node;
}

async function showMirror(ev) {
  try {
    const data = await api.mirror(ev.source_id, ev.quote);
    const body = el('div', {});

    if (data.highlight_offset === null || data.highlight_offset === undefined) {
      body.appendChild(el('div', { class: 'callout warn', style: { marginBottom: '12px' } },
        'The cited passage could not be located in the mirror. The value is marked unverified.'));
    }

    const pre = el('div', { class: 'mirror' });
    if (data.highlight_offset != null) {
      const start = data.highlight_offset;
      const length = Math.min(ev.quote.length + 60, data.markdown.length - start);
      pre.append(
        document.createTextNode(data.markdown.slice(0, start)),
        el('mark', { id: 'hl' }, data.markdown.slice(start, start + length)),
        document.createTextNode(data.markdown.slice(start + length)),
      );
    } else {
      pre.textContent = data.markdown;
    }
    body.appendChild(pre);

    openDrawer(data.filename, `${ev.locator}${ev.page ? ` · page ${ev.page}` : ''}`, body);
    setTimeout(() => document.getElementById('hl')?.scrollIntoView({ block: 'center' }), 60);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function showObservations(product, code) {
  try {
    const data = await api.observations(product.product_id, code);
    const body = el('div', {});

    if (data.conflict) {
      body.appendChild(el('div', { class: 'callout warn', style: { marginBottom: '14px' } },
        el('strong', {}, 'Sources disagreed. '),
        `Resolved by ${data.conflict.resolution_rule}.`));
    }

    data.observations.forEach((obs, i) => {
      const isWinner = data.winner && obs.value === data.winner.value
        && obs.evidence?.source_id === data.winner.evidence?.source_id;
      body.appendChild(el('div', { class: 'panel', style: { marginBottom: '10px' } },
        el('div', { class: 'panel-head' },
          el('div', { class: 'panel-title' },
            `${obs.display || '—'} `,
            isWinner ? badge('winner', 'ok') : badge('not used', 'neutral')),
          confidence(obs.confidence)),
        el('div', { class: 'panel-body' },
          obs.evidence
            ? frag(
                el('div', { class: 'meta-line' },
                  el('strong', {}, obs.evidence.source_name || obs.evidence.source_id),
                  ' · ', obs.evidence.locator, ' · ', obs.evidence.source_kind.replace(/_/g, ' ')),
                el('div', { class: 'quote' }, obs.evidence.quote))
            : el('div', { class: 'meta-line' }, obs.inference?.rationale || 'No evidence recorded.'),
        )));
    });

    openDrawer(data.name, `${data.observations.length} observation(s) across sources`, body);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function conflictsPanel(p) {
  return el('div', { class: 'panel', style: { marginBottom: '14px' } },
    el('div', { class: 'panel-head' },
      el('div', { class: 'panel-title' }, `Source conflicts (${p.conflicts.length})`)),
    el('div', { class: 'panel-body' },
      ...p.conflicts.map((c) =>
        el('div', { style: { marginBottom: '12px' } },
          el('div', { style: { marginBottom: '4px' } },
            el('strong', {}, c.name), ' ',
            badge(c.severity, c.severity === 'critical' ? 'danger' : 'warn')),
          el('div', { class: 'meta-line' },
            'Kept ', el('strong', { style: { color: 'var(--sourced)' } }, String(c.winning_value)),
            ' over ',
            c.losing_values.map((l) => String(l.value)).join(', '),
            ' — ', c.resolution_rule),
        )),
    ),
  );
}

function sourcesPanel(p) {
  return el('div', { class: 'panel', style: { marginTop: '14px' } },
    el('div', { class: 'panel-head' },
      el('div', { class: 'panel-title' }, `Source documents (${p.sources.length})`)),
    el('div', { class: 'panel-body' },
      ...p.sources.map((s) =>
        el('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', padding: '5px 0' } },
          badge(s.content_type, 'neutral'),
          el('span', {}, s.filename),
          el('span', { style: { color: 'var(--text-faint)', fontSize: '11.5px' } }, s.kind.replace(/_/g, ' ')),
          el('button', {
            class: 'btn btn-sm btn-ghost', style: { marginLeft: 'auto' },
            onClick: () => showMirror({ source_id: s.source_id, quote: '', locator: s.filename, page: null }),
          }, 'View mirror'),
        )),
    ),
  );
}

function relationsPanel(p) {
  const byType = {};
  for (const r of p.relations) (byType[r.predicate] ||= []).push(r);
  return el('div', { class: 'panel', style: { marginTop: '14px' } },
    el('div', { class: 'panel-head' },
      el('div', { class: 'panel-title' }, `Knowledge graph (${p.relations.length} relations)`)),
    el('div', { class: 'panel-body' },
      el('dl', { class: 'kv' },
        ...Object.entries(byType).flatMap(([predicate, rels]) => [
          el('dt', {}, titleCase(predicate)),
          el('dd', { style: { display: 'flex', gap: '5px', flexWrap: 'wrap' } },
            ...rels.slice(0, 14).map((r) => el('span', { class: 'chip' }, r.object_label)),
            rels.length > 14 ? el('span', { class: 'chip' }, `+${rels.length - 14} more`) : null),
        ]),
      ),
    ),
  );
}

function assetsPanel(p) {
  return el('div', { class: 'panel', style: { marginTop: '14px' } },
    el('div', { class: 'panel-head' },
      el('div', { class: 'panel-title' }, `Digital assets (${p.assets.length})`)),
    el('div', { class: 'panel-body' },
      ...p.assets.map((a) =>
        el('div', { style: { padding: '6px 0' } },
          el('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
            badge(a.shot_type.replace(/_/g, ' '), 'neutral'),
            el('span', { class: 'mono', style: { fontSize: '12px' } }, a.relative_path),
            el('span', { style: { color: 'var(--text-faint)', fontSize: '11.5px' } }, `${a.width}×${a.height}`),
            a.channel_compliant ? badge('compliant', 'ok') : badge('not compliant', 'warn')),
          a.alt_text ? el('div', { class: 'meta-line' }, el('strong', {}, 'Alt text: '), a.alt_text) : null,
          a.compliance_notes.length
            ? el('div', { class: 'meta-line', style: { color: 'var(--warn)' } }, a.compliance_notes.join('; '))
            : null,
        )),
    ),
  );
}
