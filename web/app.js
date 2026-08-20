/**
 * Application entry point: routes, the persistent chrome, and the pipeline runner.
 */

import {
  $, api, closeDrawer, el, mount, navigate, route, setNotFound, startRouter, toast,
} from './core.js';
import { overviewView } from './views/overview.js';
import { productDetailView, productsView } from './views/products.js';
import { reviewView, updateBadge } from './views/review.js';
import { schemaView, searchView, settingsView } from './views/misc.js';

route('/', overviewView);
route('/products', productsView);
route('/products/:id', productDetailView);
route('/review', reviewView);
route('/search', searchView);
route('/schema', schemaView);
route('/settings', settingsView);

setNotFound((root) => {
  mount(root, el('div', { class: 'empty' },
    el('h3', {}, 'Page not found'),
    el('p', {}, location.pathname),
    el('a', { class: 'btn', href: '/' }, 'Back to overview'),
  ));
});

/* ---------- drawer dismissal ---------- */

$('#drawer-close').addEventListener('click', closeDrawer);
$('#scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawer();
});

/* ---------- backend chip ---------- */

async function refreshBackendChip() {
  try {
    const s = await api.llm();
    const dot = $('#backend-dot');
    const label = $('#backend-label');
    dot.className = `dot ${s.mode === 'off' ? 'off' : s.available ? 'ok' : 'warn'}`;
    const modeText = { offline: 'Offline', cloud: 'AWS Bedrock', off: 'No model' }[s.mode];
    label.textContent = s.mode === 'off' ? modeText : `${modeText}${s.available ? '' : ' · unreachable'}`;
    $('#backend-chip').title = s.detail + (s.model ? `\n${s.model}` : '');
  } catch {
    $('#backend-label').textContent = 'unknown';
  }
}

async function refreshReviewBadge() {
  try {
    const data = await api.overview();
    updateBadge(data.review?.open || 0);
  } catch { /* the overview page will surface the error */ }
}

/* ---------- pipeline runner ---------- */

$('#btn-run').addEventListener('click', async () => {
  const button = $('#btn-run');
  button.disabled = true;

  const progress = el('span', {});
  const bar = el('div', { class: 'progress' }, progress);
  const log = el('div', { class: 'joblog' });
  const body = el('div', {},
    el('div', { class: 'callout', style: { marginBottom: '12px' } },
      'Ingesting every source under the configured Sources folder, then enriching, ',
      'validating and scoring the catalog. Unchanged sources are skipped.'),
    bar, log,
  );

  const { openDrawer } = await import('./core.js');
  openDrawer('Running pipeline', 'ingest → enrich → validate → score', body);

  try {
    let job = await api.startIngest({ build_after: true });
    while (job.state === 'queued' || job.state === 'running') {
      await new Promise((r) => setTimeout(r, 900));
      job = await api.job(job.job_id);
      progress.style.width = `${Math.round(job.progress * 100)}%`;
      mount(log, ...job.log.map((line) => el('div', {}, line)));
      log.scrollTop = log.scrollHeight;
    }

    if (job.state === 'failed') {
      toast(job.error || 'Pipeline failed', 'err', 9000);
    } else {
      const after = job.result?.build?.after;
      toast(
        after
          ? `Done — ${after.channel_ready} of ${after.sellable} SKUs channel-ready`
          : 'Pipeline finished',
        'ok', 7000,
      );
      refreshReviewBadge();
      const { resolve } = await import('./core.js');
      resolve();
    }
  } catch (err) {
    toast(err.message, 'err', 9000);
  } finally {
    button.disabled = false;
  }
});

/* ---------- boot ---------- */

startRouter();
refreshBackendChip();
refreshReviewBadge();
setInterval(refreshBackendChip, 45000);
