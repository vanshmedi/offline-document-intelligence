/**
 * Review queue.
 *
 * The point of this screen is throughput: a reviewer should be able to fix a
 * value or dismiss a flag without leaving the row. Corrections are applied as
 * human-authored observations, which outrank every automated source, and the
 * response reports back any rule the system learned from the correction.
 */

import { api, badge, confidence, el, mount, navigate, num, titleCase, toast } from '../core.js';

let active = { severity: '', reason: '' };

export async function reviewView(_params, root) {
  const results = el('div', {});
  const controls = el('div', { class: 'toolbar' });

  async function refresh() {
    mount(results, el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Loading…'));
    const data = await api.review({ limit: 200, severity: active.severity, reason: active.reason });
    mount(controls, ...filterChips(data.stats, refresh));
    mount(results, data.items.length ? list(data, refresh) : done());
    updateBadge(data.stats.open);
  }

  mount(root,
    el('div', { class: 'page-head' },
      el('div', {},
        el('h1', { class: 'page-title' }, 'Review queue'),
        el('div', { class: 'page-sub' },
          'Ordered by what a reviewer\'s attention is worth — reason weight × (1 − confidence)'),
      ),
    ),
    controls,
    results,
  );

  await refresh();
}

function filterChips(stats, refresh) {
  const chip = (label, count, key, value) => {
    const on = active[key] === value;
    const button = el('button', {
      class: `btn btn-sm${on ? ' btn-primary' : ''}`,
      onClick: () => { active[key] = on ? '' : value; refresh(); },
    }, label, count !== null ? el('span', { style: { opacity: '.7' } }, ` ${count}`) : null);
    return button;
  };

  return [
    el('span', { style: { fontSize: '12px', color: 'var(--text-faint)', marginRight: '4px' } },
      `${num(stats.open)} open · ${num(stats.resolved)} resolved`),
    chip('Critical only', null, 'severity', 'critical'),
    ...Object.entries(stats.by_reason || {})
      .sort((a, b) => b[1] - a[1])
      .map(([reason, count]) => chip(titleCase(reason), count, 'reason', reason)),
  ];
}

function done() {
  return el('div', { class: 'empty' },
    el('h3', {}, 'Nothing left to review'),
    el('p', {}, 'Every flag in this filter has been resolved.'));
}

function list(data, refresh) {
  const wrap = el('div', {});
  data.items.forEach((flag) => wrap.appendChild(flagCard(flag, refresh)));
  return wrap;
}

function flagCard(flag, refresh) {
  const card = el('div', { class: `flag ${flag.severity}` });

  const input = flag.allowed_values && flag.allowed_values.length
    ? el('select', { class: 'select' },
        el('option', { value: '' }, 'Choose a value…'),
        ...flag.allowed_values.map((v) =>
          el('option', { value: v, selected: v === flag.current_value }, v)))
    : el('input', {
        class: 'input',
        type: flag.datatype === 'number' ? 'number' : 'text',
        step: 'any',
        placeholder: flag.unit ? `value in ${flag.unit}` : 'corrected value',
        value: flag.current_value ?? flag.suggested_value ?? '',
      });

  const actions = el('div', { class: 'flag-actions' });

  const save = el('button', {
    class: 'btn btn-primary btn-sm',
    onClick: async () => {
      const value = input.value;
      if (value === '' || value === null) { toast('Enter a value first', 'err'); return; }
      save.disabled = true;
      save.textContent = 'Saving…';
      try {
        const res = await api.correct({
          product_id: flag.product_id,
          code: flag.attribute_code,
          value,
          flag_id: flag.flag_id,
          reviewer: 'console',
        });
        toast(`${flag.attribute_name} set to ${res.applied_value}`, 'ok');
        if (res.learned_rule) {
          toast(`Learned: ${res.learned_rule}`, 'ok', 7000);
        }
        card.style.opacity = '0.4';
        setTimeout(refresh, 700);
      } catch (err) {
        const detail = err.detail?.errors ? err.detail.errors.join('; ') : err.message;
        toast(detail, 'err', 6000);
        save.disabled = false;
        save.textContent = 'Apply correction';
      }
    },
  }, 'Apply correction');

  const dismiss = el('button', {
    class: 'btn btn-sm btn-ghost',
    onClick: async () => {
      dismiss.disabled = true;
      try {
        await api.resolveFlag(flag.flag_id, { resolution: 'accepted as-is', reviewer: 'console' });
        toast('Flag dismissed', 'ok');
        card.style.opacity = '0.4';
        setTimeout(refresh, 500);
      } catch (err) {
        toast(err.message, 'err');
        dismiss.disabled = false;
      }
    },
  }, 'Accept as-is');

  if (flag.attribute_code) {
    actions.append(input, save, dismiss);
  } else {
    actions.append(dismiss);
  }

  card.append(
    el('div', { class: 'flag-head' },
      el('div', { style: { minWidth: '0' } },
        el('div', { style: { display: 'flex', gap: '7px', alignItems: 'center', flexWrap: 'wrap' } },
          badge(titleCase(flag.reason_kind), flag.severity === 'critical' ? 'danger' : 'warn'),
          el('a', {
            href: `/products/${encodeURIComponent(flag.product_id)}`,
            class: 'mono',
            style: { fontWeight: '550' },
          }, flag.product_mpn),
          flag.attribute_name
            ? el('span', { style: { color: 'var(--text-dim)' } }, flag.attribute_name)
            : null,
        ),
        el('div', { class: 'flag-reason' }, flag.reason.replace(/^[a-z_]+:\s*/, '')),
        flag.evidence
          ? el('div', { class: 'flag-meta', style: { marginTop: '5px' } },
              `${flag.evidence.source_name || flag.evidence.source_id} · ${flag.evidence.locator}`)
          : null,
      ),
      confidence(flag.confidence),
    ),
    actions,
  );

  return card;
}

export function updateBadge(open) {
  const badgeNode = document.getElementById('review-badge');
  if (!badgeNode) return;
  if (open > 0) {
    badgeNode.textContent = String(open);
    badgeNode.hidden = false;
  } else {
    badgeNode.hidden = true;
  }
}
