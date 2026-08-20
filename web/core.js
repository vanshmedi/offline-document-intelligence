/**
 * Core utilities: API client, DOM helpers, formatting, router.
 *
 * No framework and no build step. The whole console is ES modules served
 * straight from disk, which means `uvicorn` is the only process needed to
 * demo it -- no npm install, no bundler, nothing to compile.
 */

/* ---------- API ---------- */

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail;
    try {
      const body = await res.json();
      detail = body.detail ?? body;
    } catch {
      detail = await res.text();
    }
    const message = typeof detail === 'string' ? detail : (detail?.message || JSON.stringify(detail));
    const err = new Error(message || `HTTP ${res.status}`);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

const qs = (params) => {
  const clean = Object.entries(params || {}).filter(
    ([, v]) => v !== undefined && v !== null && v !== '' && v !== false,
  );
  return clean.length ? '?' + new URLSearchParams(clean).toString() : '';
};

export const api = {
  overview:      ()            => request('/api/overview'),
  products:      (p)           => request('/api/products' + qs(p)),
  product:       (id)          => request(`/api/products/${encodeURIComponent(id)}`),
  observations:  (id, code)    => request(`/api/products/${encodeURIComponent(id)}/observations/${code}`),
  mirror:        (sid, hl)     => request(`/api/sources/${sid}/mirror` + qs({ highlight: hl })),
  review:        (p)           => request('/api/review' + qs(p)),
  correct:       (body)        => request('/api/review/correct', { method: 'POST', body: JSON.stringify(body) }),
  resolveFlag:   (id, body)    => request(`/api/review/${id}/resolve`, { method: 'POST', body: JSON.stringify(body) }),
  search:        (p)           => request('/api/search' + qs(p)),
  schema:        ()            => request('/api/schema'),
  categorySchema:(id)          => request(`/api/schema/${id}`),
  llm:           ()            => request('/api/llm'),
  llmSwitch:     (body)        => request('/api/llm', { method: 'POST', body: JSON.stringify(body) }),
  llmTest:       ()            => request('/api/llm/test', { method: 'POST' }),
  startIngest:   (p)           => request('/api/jobs/ingest' + qs(p), { method: 'POST' }),
  startBuild:    (p)           => request('/api/jobs/build' + qs(p), { method: 'POST' }),
  job:           (id)          => request(`/api/jobs/${id}`),
  export:        (body)        => request('/api/export', { method: 'POST', body: JSON.stringify(body) }),
  health:        ()            => request('/api/health'),
};

/* ---------- DOM ---------- */

/**
 * Create an element.
 *
 * Text content is assigned via textContent, never innerHTML, so a value read
 * out of a manufacturer's PDF can never execute as markup in this console.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  append(node, children);
  return node;
}

function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export const frag = (...children) => {
  const f = document.createDocumentFragment();
  append(f, children);
  return f;
};

export const mount = (node, ...children) => {
  node.replaceChildren();
  append(node, children);
  return node;
};

export const $ = (sel, root = document) => root.querySelector(sel);

/* ---------- formatting ---------- */

export const pct = (v, digits = 1) =>
  `${((Number(v) || 0) * 100).toFixed(digits)}%`;

export const num = (v) =>
  (Number(v) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });

export function delta(after, before, digits = 1) {
  const d = (Number(after) || 0) - (Number(before) || 0);
  const cls = d > 0.0005 ? 'up' : d < -0.0005 ? 'down' : 'flat';
  const sign = d > 0.0005 ? '+' : '';
  return { cls, text: `${sign}${(d * 100).toFixed(digits)}%`, value: d };
}

export function confidenceColor(c) {
  if (c >= 0.85) return 'var(--sourced)';
  if (c >= 0.7) return 'var(--accent)';
  if (c >= 0.5) return 'var(--warn)';
  return 'var(--danger)';
}

export function confidence(c) {
  const value = Number(c) || 0;
  return el('span', { class: 'conf', title: `confidence ${value.toFixed(2)}` },
    el('span', { class: 'conf-bar' },
      el('span', { style: { width: `${Math.round(value * 100)}%`, background: confidenceColor(value) } })),
    el('span', { class: 'conf-val' }, value.toFixed(2)),
  );
}

export const badge = (text, kind = 'neutral', title = '') =>
  el('span', { class: `badge ${kind}`, title: title || undefined }, text);

export function bar(value, before = null) {
  const track = el('div', { class: 'bar-track' });
  if (before !== null && before !== undefined) {
    track.appendChild(el('div', {
      class: 'bar-fill before',
      style: { width: `${Math.round(before * 100)}%`, position: 'absolute', inset: '0 auto 0 0' },
    }));
  }
  track.appendChild(el('div', {
    class: 'bar-fill',
    style: { width: `${Math.round((Number(value) || 0) * 100)}%`, position: 'relative' },
  }));
  return track;
}

export const titleCase = (s) =>
  String(s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

/* ---------- toasts ---------- */

export function toast(message, kind = 'ok', ms = 4200) {
  const node = el('div', { class: `toast ${kind}` }, message);
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s';
    setTimeout(() => node.remove(), 260);
  }, ms);
}

/* ---------- drawer ---------- */

export function openDrawer(title, subtitle, body) {
  $('#drawer-title').textContent = title;
  $('#drawer-sub').textContent = subtitle || '';
  mount($('#drawer-body'), body);
  $('#drawer').hidden = false;
  $('#scrim').hidden = false;
}

export function closeDrawer() {
  $('#drawer').hidden = true;
  $('#scrim').hidden = true;
}

/* ---------- router ---------- */

const routes = [];
let notFound = null;

export const route = (pattern, handler) => routes.push({ pattern, handler });
export const setNotFound = (handler) => { notFound = handler; };

export function navigate(path, replace = false) {
  if (replace) history.replaceState({}, '', path);
  else history.pushState({}, '', path);
  resolve();
}

function match(pattern, path) {
  const p = pattern.split('/').filter(Boolean);
  const a = path.split('/').filter(Boolean);
  if (p.length !== a.length) return null;
  const params = {};
  for (let i = 0; i < p.length; i++) {
    if (p[i].startsWith(':')) params[p[i].slice(1)] = decodeURIComponent(a[i]);
    else if (p[i] !== a[i]) return null;
  }
  return params;
}

export async function resolve() {
  const path = location.pathname;
  const main = $('#main');

  for (const { pattern, handler } of routes) {
    const params = match(pattern, path);
    if (params) {
      mount(main, el('div', { class: 'loading' }, el('span', { class: 'spinner' }), 'Loading…'));
      try {
        await handler(params, main);
      } catch (err) {
        console.error(err);
        mount(main, el('div', { class: 'empty' },
          el('h3', {}, 'Something went wrong'),
          el('p', {}, err.message),
          el('button', { class: 'btn', onClick: () => resolve() }, 'Retry'),
        ));
      }
      highlightNav(path);
      window.scrollTo(0, 0);
      return;
    }
  }
  if (notFound) notFound(main);
}

function highlightNav(path) {
  document.querySelectorAll('.nav-item').forEach((item) => {
    const r = item.dataset.route;
    const active = r === '/' ? path === '/' : path.startsWith(r);
    item.classList.toggle('active', active);
  });
}

export function startRouter() {
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href^="/"]');
    if (!link || link.target === '_blank' || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    navigate(link.getAttribute('href'));
  });
  window.addEventListener('popstate', resolve);
  resolve();
}
