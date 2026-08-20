/** Search, schema browser, and settings. */

import {
  api, badge, confidence, el, mount, navigate, num, pct, titleCase, toast,
} from '../core.js';
import { statusBadge } from './overview.js';

/* ---------------------------------------------------------------- search */

export async function searchView(_params, root) {
  const params = new URLSearchParams(location.search);
  const input = el('input', {
    class: 'input', type: 'search', autofocus: true,
    placeholder: 'e.g. 3 pole circuit breaker rated 63A, or stainless ball valve 1 inch NPT',
    value: params.get('q') || '',
  });
  const results = el('div', {});

  async function run() {
    const q = input.value.trim();
    if (!q) { mount(results); return; }
    history.replaceState({}, '', `/search?q=${encodeURIComponent(q)}`);
    mount(results, el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Searching…'));
    try {
      const data = await api.search({ q, limit: 30 });
      mount(results, renderResults(data));
    } catch (err) {
      mount(results, el('div', { class: 'callout danger' }, err.message));
    }
  }

  const form = el('form', {
    class: 'toolbar',
    onSubmit: (e) => { e.preventDefault(); run(); },
  },
    el('div', { class: 'grow' }, input),
    el('button', { class: 'btn btn-primary', type: 'submit' }, 'Search'),
  );

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Search'),
        el('div', { class: 'page-sub' },
          'Attribute matching and semantic retrieval, combined — a spec query and a descriptive one both work'),
      ),
    ),
    form,
    results,
  );

  if (input.value) run();
}

function renderResults(data) {
  if (!data.hits.length) {
    return el('div', { class: 'empty' },
      el('h3', {}, 'No matches'),
      el('p', {}, 'Try fewer terms, or a part number.'));
  }

  return el('div', {},
    el('div', { style: { fontSize: '12px', color: 'var(--text-faint)', marginBottom: '10px' } },
      `${num(data.total)} matches`,
      data.semantic_available
        ? ' · semantic + attribute matching'
        : ' · attribute matching only (no vector index — run a build with embeddings enabled)'),
    el('div', { class: 'panel' },
      el('div', { class: 'panel-body flush' },
        el('div', { class: 'table-wrap' },
          el('table', { class: 'table' },
            el('thead', {}, el('tr', {},
              el('th', {}, 'Part number'), el('th', {}, 'Product'), el('th', {}, 'Matched on'),
              el('th', {}, 'Status'), el('th', {}, 'Score'))),
            el('tbody', {},
              ...data.hits.map((h) =>
                el('tr', { onClick: () => navigate(`/products/${encodeURIComponent(h.product.product_id)}`) },
                  el('td', { class: 'mono', style: { fontWeight: '550' } }, h.product.mpn),
                  el('td', {},
                    el('div', {}, h.product.name),
                    el('div', { style: { fontSize: '11.5px', color: 'var(--text-faint)' } },
                      `${h.product.manufacturer} · ${h.product.category_name}`)),
                  el('td', { style: { fontSize: '11.5px', color: 'var(--text-dim)' } },
                    h.matched_on.length ? h.matched_on.join(', ') : badge(h.match_kind, 'neutral')),
                  el('td', {}, statusBadge(h.product.status)),
                  el('td', {}, confidence(h.score)),
                )),
            ),
          ),
        ),
      ),
    ),
  );
}

/* ---------------------------------------------------------------- schema */

export async function schemaView(_params, root) {
  const categories = await api.schema();

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Attribute schema'),
        el('div', { class: 'page-sub' },
          'The contract that makes extraction schema-directed instead of open-ended'),
      ),
    ),
    el('div', { class: 'panel' },
      el('div', { class: 'panel-body flush' },
        el('div', { class: 'table-wrap' },
          el('table', { class: 'table' },
            el('thead', {}, el('tr', {},
              el('th', {}, 'Category'), el('th', {}, 'Vertical'), el('th', {}, 'ETIM'),
              el('th', {}, 'UNSPSC'), el('th', { class: 'num' }, 'Attributes'),
              el('th', { class: 'num' }, 'Required (core)'), el('th', { class: 'num' }, 'Rules'),
              el('th', { class: 'num' }, 'Products'))),
            el('tbody', {},
              ...categories.map((c) =>
                el('tr', { onClick: () => showCategory(c.id) },
                  el('td', {}, el('strong', {}, c.name)),
                  el('td', { style: { color: 'var(--text-dim)' } }, titleCase(c.vertical)),
                  el('td', { class: 'mono' }, c.etim || '—'),
                  el('td', { class: 'mono' }, c.unspsc || '—'),
                  el('td', { class: 'num' }, c.attribute_count),
                  el('td', { class: 'num' }, c.required_core),
                  el('td', { class: 'num' }, c.rules),
                  el('td', { class: 'num' }, c.product_count),
                )),
            ),
          ),
        ),
      ),
    ),
    el('div', { id: 'cat-detail', style: { marginTop: '16px' } }),
  );
}

async function showCategory(id) {
  const target = document.getElementById('cat-detail');
  mount(target, el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Loading…'));
  const cat = await api.categorySchema(id);

  mount(target,
    el('div', { class: 'panel' },
      el('div', { class: 'panel-head' },
        el('div', { class: 'panel-title' }, `${cat.name} — ${cat.attributes.length} attributes`),
        el('div', { style: { fontSize: '11.5px', color: 'var(--text-faint)' } },
          cat.etim ? `ETIM ${cat.etim}` : '')),
      el('div', { class: 'panel-body flush' },
        el('div', { class: 'table-wrap' },
          el('table', { class: 'table' },
            el('thead', {}, el('tr', {},
              el('th', {}, 'Attribute'), el('th', {}, 'Type'), el('th', {}, 'Unit'),
              el('th', {}, 'Required'), el('th', {}, 'Recognised as'))),
            el('tbody', {},
              ...cat.attributes.map((a) =>
                el('tr', { style: { cursor: 'default' } },
                  el('td', {},
                    a.name,
                    a.variant_defining ? badge('variant', 'human', 'Distinguishes one variant from another') : null,
                    a.generated ? badge('generated', 'generated') : null),
                  el('td', { style: { color: 'var(--text-dim)' } },
                    a.datatype,
                    a.allowed_values
                      ? el('div', { style: { fontSize: '11px', color: 'var(--text-faint)' } },
                          a.allowed_values.slice(0, 4).join(' · ') + (a.allowed_values.length > 4 ? ' …' : ''))
                      : null),
                  el('td', { class: 'mono' }, a.unit || '—'),
                  el('td', {}, a.required_for.length
                    ? a.required_for.map((r) => badge(r, r === 'core' ? 'danger' : 'neutral'))
                    : el('span', { style: { color: 'var(--text-faint)' } }, '—')),
                  el('td', { style: { fontSize: '11px', color: 'var(--text-faint)' } },
                    a.aliases.slice(0, 5).join(', ')),
                )),
            ),
          ),
        ),
      ),
      cat.rules.length
        ? el('div', { class: 'panel-body' },
            el('div', { class: 'metric-label' }, 'Validation rules'),
            ...cat.rules.map((r) =>
              el('div', { class: 'meta-line', style: { fontSize: '12.5px', padding: '3px 0' } },
                el('span', { class: 'mono', style: { color: 'var(--text-dim)' } }, r.id), ' — ', r.message)))
        : null,
    ),
  );
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* -------------------------------------------------------------- settings */

export async function settingsView(_params, root) {
  const status = await api.llm();

  const panel = el('div', {});
  render();

  async function render() {
    const s = await api.llm();
    const modeCopy = {
      offline: ['Offline', 'Ollama on this machine. Nothing leaves the building.'],
      cloud: ['Cloud', `AWS Bedrock in ${s.region}.`],
      off: ['Off', 'No model is called. Deterministic extraction only.'],
    };
    const [modeLabel, modeDetail] = modeCopy[s.mode];

    const modeButtons = el('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } },
      ...[['ollama', 'Offline (Ollama)'], ['bedrock', 'Cloud (AWS Bedrock)'], ['off', 'Off']]
        .map(([value, label]) => {
          const on = (value === 'off' && s.mode === 'off') || (value !== 'off' && s.provider === value);
          return el('button', {
            class: `btn${on ? ' btn-primary' : ''}`,
            onClick: async () => {
              try {
                await api.llmSwitch({ provider: value });
                toast(`Switched to ${label}`, 'ok');
                render();
              } catch (err) { toast(err.message, 'err'); }
            },
          }, label);
        }),
    );

    const modelSelect = s.mode === 'off' ? null : (() => {
      const options = s.suggested_models.map((m) => m.id);
      if (s.model && !options.includes(s.model)) options.unshift(s.model);
      const node = el('select', { class: 'select' },
        ...options.map((id) => el('option', { value: id, selected: id === s.model }, id)));
      node.addEventListener('change', async () => {
        try {
          await api.llmSwitch({ provider: s.provider, model: node.value });
          toast(`Model set to ${node.value}`, 'ok');
          render();
        } catch (err) { toast(err.message, 'err'); }
      });
      return el('div', { class: 'field' },
        el('label', {}, 'Model'),
        node,
        el('div', { class: 'hint' },
          s.suggested_models.find((m) => m.id === s.model)?.note || ''),
      );
    })();

    mount(panel,
      el('div', { class: 'panel' },
        el('div', { class: 'panel-head' },
          el('div', { class: 'panel-title' }, 'AI backend'),
          badge(modeLabel, s.mode === 'cloud' ? 'human' : s.mode === 'offline' ? 'ok' : 'neutral')),
        el('div', { class: 'panel-body', style: { display: 'grid', gap: '14px' } },
          el('div', { class: 'field' },
            el('label', {}, 'Where inference runs'),
            modeButtons,
            el('div', { class: 'hint' }, modeDetail)),

          modelSelect,

          s.mode !== 'off'
            ? el('div', { class: `callout ${s.available ? 'ok' : 'warn'}` },
                el('strong', {}, s.available ? 'Ready. ' : 'Not reachable. '),
                s.available
                  ? (s.credential_source
                      ? `Credentials from ${s.credential_source}.`
                      : s.endpoint || '')
                  : 'The pipeline still runs — deterministic extraction only, with lower coverage.')
            : null,

          !s.available && s.remediation.length
            ? el('div', {},
                el('div', { class: 'metric-label' }, 'To fix'),
                el('div', { class: 'code' }, s.remediation.join('\n')))
            : null,

          s.env_shadowing.length
            ? el('div', { class: 'callout warn' },
                el('strong', {}, `${s.env_shadowing.join(', ')} is set in the environment. `),
                'It overrides settings.json, so this toggle will not take effect until it is unset.')
            : null,

          el('div', { style: { display: 'flex', gap: '8px' } },
            el('button', {
              class: 'btn',
              disabled: s.mode === 'off',
              onClick: async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true; btn.textContent = 'Testing…';
                const res = await api.llmTest();
                btn.disabled = false; btn.textContent = 'Test connection';
                toast(res.ok ? `Connection OK (${res.elapsed_s}s)` : res.error, res.ok ? 'ok' : 'err', 7000);
              },
            }, 'Test connection'),
          ),
        ),
      ),

      el('div', { class: 'panel', style: { marginTop: '14px' } },
        el('div', { class: 'panel-head' }, el('div', { class: 'panel-title' }, 'Export the catalog')),
        el('div', { class: 'panel-body' },
          el('div', { style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } },
            ...[['json', 'JSON (with evidence)'], ['csv', 'CSV (channel feed)'],
                ['bmecat', 'BMEcat + ETIM'], ['gdsn', 'GDSN']].map(([fmt, label]) =>
              el('button', {
                class: 'btn',
                onClick: async (e) => {
                  const btn = e.currentTarget;
                  btn.disabled = true;
                  try {
                    const res = await api.export({ fmt, ready_only: false });
                    toast(`Exported ${res.products} products (${(res.bytes / 1024).toFixed(0)} KB)`, 'ok');
                    window.location.href = res.download;
                  } catch (err) { toast(err.message, 'err'); }
                  btn.disabled = false;
                },
              }, label)),
          ),
          el('div', { class: 'hint', style: { marginTop: '10px' } },
            'BMEcat carries the ETIM class and every attribute\'s confidence. JSON carries the full evidence trail.'),
        ),
      ),
    );
  }

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Settings'),
        el('div', { class: 'page-sub' }, 'Backend selection and catalog export'),
      ),
    ),
    panel,
  );
}
