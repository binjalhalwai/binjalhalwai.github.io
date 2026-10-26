// Scrolling document view: page containers, lazy canvas rendering/virtualization, zoom & fit, navigation.
import { state, on, emit } from './state.js';
import { PT_PX, clamp, debounce, h, $ } from './utils.js';
import { startRender, releasePageProxy } from './pdf-renderer.js';
import { renderPageObjects } from './objects.js';
import { sameBox } from './page-geom.js';
import { buildFormLayer, refreshAllLayers } from './forms.js';

const LEVELS = [25, 33, 50, 67, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500];
const MIN_Z = 0.2, MAX_Z = 8;
let scroller, pagesEl;
const ctrls = new Map(); // pageId -> {el, model, canvas, scale, job, near, token}
let ioNear, ioFar;
const queue = [];
let running = 0;

export function init() {
  scroller = $('#viewer'); pagesEl = $('#pages');
  ioNear = new IntersectionObserver(entries => {
    for (const e of entries) {
      const c = ctrls.get(e.target.dataset.id); if (!c) continue;
      c.near = e.isIntersecting;
      if (c.near) schedule(c);
    }
  }, { root: scroller, rootMargin: '1500px 0px' });
  ioFar = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (e.isIntersecting) continue;
      const c = ctrls.get(e.target.dataset.id); if (c) release(c);
    }
  }, { root: scroller, rootMargin: '5000px 0px' });

  let raf = 0;
  scroller.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; updateCurrent(); }); }, { passive: true });
  scroller.addEventListener('wheel', e => {
    if (!(e.ctrlKey || e.metaKey) || !state.doc) return;
    e.preventDefault();
    zoomTo(state.zoom * Math.exp(-e.deltaY * 0.0025));
  }, { passive: false });
  const refit = debounce(() => { if (state.doc && state.fitMode) fit(state.fitMode); }, 150);
  new ResizeObserver(refit).observe(scroller);

  on('doc:open', () => buildPages());
  on('doc:restore', () => syncPages());
  on('pages', () => syncPages());
}

function createPageEl(model, index) {
  const overlay = h('div', { class: 'overlay', 'data-page-id': model.id });
  const el = h('div', { class: 'page', 'data-id': model.id, 'data-index': index, style: `--w:${model.w};--h:${model.h}`, role: 'group', 'aria-label': `Page ${index + 1}` },
    model.kind === 'pdf' ? h('div', { class: 'loading' }, `Page ${index + 1}`) : null, overlay, h('div', { class: 'formlayer' }));
  return { el, overlay };
}

export function buildPages() {
  for (const c of ctrls.values()) teardown(c);
  ctrls.clear(); state.overlays.clear(); queue.length = 0;
  pagesEl.textContent = '';
  state.doc.pages.forEach((model, i) => {
    const { el, overlay } = createPageEl(model, i);
    pagesEl.append(el);
    state.overlays.set(model.id, overlay);
    const c = { el, model, canvas: null, scale: 0, job: null, near: false, token: 0 };
    ctrls.set(model.id, c);
    ioNear.observe(el); ioFar.observe(el);
    renderPageObjects(model.id);
  });
  state.fitMode = 'width';
  fit('width');
  scroller.scrollTop = 0;
  $('#pg-total').textContent = state.doc.pages.length;
  state.currentPage = 0;
  emit('page', 0);
}

const geomSame = (a, b) => a.w === b.w && a.h === b.h && a.rot === b.rot && sameBox(a.view, b.view);

/** Reconcile page elements with state.doc.pages after reorder / delete / rotate / crop / undo (reuses rendered canvases). */
export function syncPages() {
  if (!state.doc) return;
  const pages = state.doc.pages, old = new Map(ctrls), keepTop = scroller.scrollTop;
  ctrls.clear(); state.overlays.clear(); queue.length = 0;
  const els = []; let recreated = false;
  pages.forEach((model, i) => {
    let c = old.get(model.id);
    if (c && geomSame(c.model, model)) { old.delete(model.id); c.model = model; }
    else {
      if (c) { teardown(c); c.el.remove(); old.delete(model.id); recreated = true; }
      const { el, overlay } = createPageEl(model, i);
      c = { el, model, canvas: null, scale: 0, job: null, near: false, token: 0, overlay };
      ioNear.observe(el); ioFar.observe(el);
    }
    c.el.dataset.index = i; c.el.setAttribute('aria-label', `Page ${i + 1}`);
    const lo = c.el.querySelector('.loading'); if (lo) lo.textContent = `Page ${i + 1}`;
    c.overlay = c.el.querySelector('.overlay');
    ctrls.set(model.id, c); state.overlays.set(model.id, c.overlay);
    els.push(c.el);
  });
  for (const c of old.values()) { teardown(c); c.el.remove(); }
  pagesEl.append(...els);
  for (const c of ctrls.values()) { renderPageObjects(c.model.id); if (c.near && c.model.kind === 'pdf' && (!c.canvas || c.scale !== state.zoom)) schedule(c); }
  state.currentPage = Math.min(state.currentPage, pages.length - 1);
  scroller.scrollTop = keepTop;
  $('#pg-total').textContent = pages.length; $('#pg-input').max = pages.length;
  refreshAllLayers();
  if (recreated && state.fitMode) fit(state.fitMode);
  emit('pages:synced');
  updateCurrent();
}

function teardown(c) { release(c); ioNear.unobserve(c.el); ioFar.unobserve(c.el); }

function release(c) {
  c.token++;
  if (c.job) { c.job.cancel(); c.job = null; }
  if (c.canvas) { c.canvas.remove(); c.canvas.width = c.canvas.height = 0; c.canvas = null; c.scale = 0; releasePageProxy(c.model); }
  const i = queue.indexOf(c); if (i >= 0) queue.splice(i, 1);
  if (!c.el.querySelector('.loading') && c.model.kind === 'pdf') c.el.prepend(h('div', { class: 'loading' }, `Page ${+c.el.dataset.index + 1}`));
}

function schedule(c) {
  if (c.model.kind !== 'pdf') return;
  if (c.canvas && c.scale === state.zoom) return;
  if (!queue.includes(c)) queue.push(c);
  pump();
}

function pump() {
  while (running < 2 && queue.length) {
    // prefer pages closest to the viewport centre
    const mid = scroller.scrollTop + scroller.clientHeight / 2;
    queue.sort((a, b) => Math.abs(a.el.offsetTop + a.el.offsetHeight / 2 - mid) - Math.abs(b.el.offsetTop + b.el.offsetHeight / 2 - mid));
    const c = queue.shift();
    if (!c.near && c.canvas) continue;
    running++;
    renderCtrl(c).catch(e => console.warn('render failed:', e && e.name)).finally(() => { running--; pump(); });
  }
}

async function renderCtrl(c) {
  const scale = state.zoom;
  if (c.job) c.job.cancel();
  const token = ++c.token;
  const job = startRender(c.model, scale);
  c.job = job;
  const canvas = await job.promise;
  if (token !== c.token || !canvas) return;
  c.job = null;
  if (c.canvas) c.canvas.remove();
  c.canvas = canvas; c.scale = scale;
  c.el.querySelector('.loading')?.remove();
  c.el.insertBefore(canvas, c.el.firstChild);
  if (!c.formsBuilt) { c.formsBuilt = true; buildFormLayer(c.model.id, c.el); }
}

const rerenderVisible = debounce(() => { for (const c of ctrls.values()) if (c.near) schedule(c); }, 220);

export function setZoom(z, { keepFit = false } = {}) {
  z = clamp(z, MIN_Z, MAX_Z);
  if (!keepFit) state.fitMode = null;
  const ratio = pagesEl.offsetHeight ? (scroller.scrollTop + scroller.clientHeight / 2) / pagesEl.offsetHeight : 0;
  state.zoom = z;
  pagesEl.style.setProperty('--z', z);
  scroller.scrollTop = ratio * pagesEl.offsetHeight - scroller.clientHeight / 2;
  rerenderVisible();
  emit('zoom', z);
}
export const zoomTo = z => setZoom(z);
export function zoomStep(dir) {
  const pct = state.zoom / PT_PX * 100;
  const next = dir > 0 ? LEVELS.find(l => l > pct + 0.5) : [...LEVELS].reverse().find(l => l < pct - 0.5);
  setZoom((next ?? (dir > 0 ? LEVELS.at(-1) : LEVELS[0])) / 100 * PT_PX);
}
export function fit(mode) {
  if (!state.doc) return;
  const m = state.doc.pages[state.currentPage] || state.doc.pages[0];
  const narrow = window.innerWidth <= 900;
  const availW = scroller.clientWidth - (narrow ? 20 : 56);
  const availH = scroller.clientHeight - 36;
  let z = availW / m.w;
  if (mode === 'page') z = Math.min(z, availH / m.h);
  z = Math.min(z, 2.2);
  state.fitMode = mode;
  setZoom(z, { keepFit: true });
}

function updateCurrent() {
  if (!state.doc) return;
  const kids = pagesEl.children;
  const y = scroller.scrollTop + scroller.clientHeight * 0.35 - pagesEl.offsetTop;
  let lo = 0, hi = kids.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (kids[mid].offsetTop <= y) lo = mid; else hi = mid - 1; }
  if (lo !== state.currentPage) { state.currentPage = lo; emit('page', lo); }
}

export function goToPage(i, smooth = false) {
  if (!state.doc) return;
  i = clamp(i, 0, state.doc.pages.length - 1);
  const el = pagesEl.children[i];
  state.currentPage = i;
  scroller.scrollTo({ top: el.offsetTop + pagesEl.offsetTop - 12, behavior: smooth ? 'smooth' : 'auto' });
  emit('page', i);
}

export const getPageElement = id => ctrls.get(id)?.el || null;
export const getScroller = () => scroller;

export const getPageCanvas = id => ctrls.get(id)?.canvas || null;
export function scrollToPageRect(pageId, y) {
  const c = ctrls.get(pageId); if (!c) return;
  const top = c.el.offsetTop + pagesEl.offsetTop + y * state.zoom - scroller.clientHeight * 0.3;
  scroller.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
  const idx = state.doc.pages.findIndex(p => p.id === pageId);
  if (idx >= 0 && idx !== state.currentPage) { state.currentPage = idx; emit('page', idx); }
}
