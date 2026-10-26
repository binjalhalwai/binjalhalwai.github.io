// Left sidebar: lazily-rendered page thumbnails, current-page highlight, multi-select and drag-to-reorder.
import { state, on, emit } from './state.js';
import { h, $, icon } from './utils.js';
import { startRender } from './pdf-renderer.js';
import { goToPage } from './viewer.js';
import { sameBox } from './page-geom.js';
import * as ops from './page-ops.js';

const THUMB_W = 120;
let list, io, dropLine;
const items = new Map(); // pageId -> {el, box, numEl, job, done, model, sig}
let anchor = null;

const sigOf = m => `${m.w}|${m.h}|${m.rot}|${(m.view || []).join(',')}`;

export function init() {
  list = $('#thumbs');
  dropLine = h('div', { class: 'drop-line', hidden: true });
  io = new IntersectionObserver(entries => {
    for (const e of entries) {
      const it = items.get(e.target.dataset.id); if (!it) continue;
      if (e.isIntersecting) renderThumb(it); else freeThumb(it);
    }
  }, { root: list, rootMargin: '400px 0px' });
  on('doc:open', () => { state.selectedPages = new Set(); anchor = null; sync(true); });
  on('pages', () => sync());
  on('doc:restore', () => sync());
  on('page', i => {
    const id = state.doc?.pages[i]?.id;
    if (id && state.selectedPages.size <= 1) setSelected([id], false);
    highlight(i);
  });
  on('pagesel', paintSelection);

  list.addEventListener('dragover', e => {
    if (!dragIds) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    const t = dropTarget(e.clientY);
    placeDropLine(t);
  });
  list.addEventListener('dragleave', e => { if (!list.contains(e.relatedTarget)) dropLine.hidden = true; });
  list.addEventListener('drop', e => {
    if (!dragIds) return;
    e.preventDefault();
    const t = dropTarget(e.clientY);
    dropLine.hidden = true;
    const ids = dragIds; dragIds = null;
    ops.movePagesTo(ids, t.beforeId);
  });
}

function setSelected(ids, notify = true) {
  state.selectedPages = new Set(ids);
  if (notify) emit('pagesel');
  else paintSelection();
}
export function selectPage(id) { setSelected([id]); anchor = id; }

let dragIds = null;
function dropTarget(y) {
  const els = [...list.querySelectorAll('.thumb')];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (y < r.top + r.height / 2) return { beforeId: el.dataset.id, el };
  }
  return { beforeId: null, el: null };
}
function placeDropLine(t) {
  dropLine.hidden = false;
  if (t.el) list.insertBefore(dropLine, t.el); else list.append(dropLine);
}

function makeItem(m, i) {
  const box = h('div', { class: 'tcanvas' });
  const numEl = h('div', { class: 'tnum' });
  const btn = (label, ic, fn) => h('button', { type: 'button', class: 'tbtn', title: label, 'aria-label': label, onclick: e => { e.stopPropagation(); fn(); } }, icon(ic));
  const act = fn => () => { if (!state.selectedPages.has(m.id)) selectPage(m.id); fn(); };
  const tools = h('div', { class: 'tbtns' },
    btn('Rotate page', 'rotate', act(() => ops.rotatePages(ops.targetIds(), 1))),
    btn('Duplicate page', 'copy', act(() => ops.duplicatePages(ops.targetIds()))),
    btn('Delete page', 'trash', act(() => ops.deletePagesConfirm(ops.targetIds()))));
  const el = h('div', { class: 'thumb', role: 'option', tabindex: '0', draggable: 'true', 'data-id': m.id, 'aria-selected': 'false' }, box, h('div', { class: 'tfoot' }, numEl), tools);
  el.addEventListener('click', e => {
    const idx = state.doc.pages.findIndex(p => p.id === m.id);
    if (e.ctrlKey || e.metaKey) {
      const s = new Set(state.selectedPages); if (s.has(m.id) && s.size > 1) s.delete(m.id); else s.add(m.id);
      setSelected([...s]); anchor = m.id; return;
    }
    if (e.shiftKey && anchor) {
      const a = state.doc.pages.findIndex(p => p.id === anchor), [lo, hi] = a < idx ? [a, idx] : [idx, a];
      setSelected(state.doc.pages.slice(lo, hi + 1).map(p => p.id)); return;
    }
    selectPage(m.id); anchor = m.id;
    goToPage(idx);
  });
  el.addEventListener('keydown', e => {
    if (e.target !== el) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click(); }
  });
  el.addEventListener('dragstart', e => {
    if (!state.selectedPages.has(m.id)) selectPage(m.id);
    dragIds = ops.targetIds();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/x-page-ids', dragIds.join(','));
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => { dragIds = null; el.classList.remove('dragging'); dropLine.hidden = true; });
  return { el, box, numEl, job: null, done: false, model: m, sig: '' };
}

/** Reconcile the thumbnail list with state.doc.pages (reusing rendered thumbnails whose geometry did not change). */
export function sync(full = false) {
  if (!state.doc) return;
  const pages = state.doc.pages;
  if (full) { for (const it of items.values()) { it.job?.cancel(); io.unobserve(it.el); } items.clear(); list.textContent = ''; }
  const keep = new Set(pages.map(p => p.id));
  for (const [id, it] of [...items]) if (!keep.has(id)) { it.job?.cancel(); io.unobserve(it.el); it.el.remove(); items.delete(id); }
  const frag = [];
  pages.forEach((m, i) => {
    let it = items.get(m.id);
    if (!it) { it = makeItem(m, i); items.set(m.id, it); io.observe(it.el); }
    it.model = m;
    const sig = sigOf(m);
    if (sig !== it.sig) {                                   // geometry changed: resize + re-render
      it.sig = sig;
      freeThumb(it);
      it.box.style.width = `${THUMB_W}px`; it.box.style.height = `${Math.round(THUMB_W * m.h / m.w)}px`;
      it.box.textContent = '';
      if (it.visible) renderThumb(it);
    }
    it.numEl.textContent = String(i + 1);
    it.el.setAttribute('aria-label', `Page ${i + 1}`);
    frag.push(it.el);
  });
  list.append(...frag);
  $('#page-count-badge').textContent = `${pages.length}`;
  for (const id of [...state.selectedPages]) if (!keep.has(id)) state.selectedPages.delete(id);
  if (!state.selectedPages.size && pages[state.currentPage]) state.selectedPages.add(pages[state.currentPage].id);
  paintSelection();
  highlight(Math.min(state.currentPage, pages.length - 1));
  // re-observe so newly visible items render
  for (const it of items.values()) { io.unobserve(it.el); io.observe(it.el); }
}

async function renderThumb(it) {
  it.visible = true;
  if (it.done || it.job || it.model.kind !== 'pdf') return;
  const scale = (THUMB_W * (window.devicePixelRatio > 1 ? 1.5 : 1)) / it.model.w;
  const job = it.job = startRender(it.model, scale, { dpr: false });
  try {
    const canvas = await job.promise;
    if (canvas && it.job === job) { it.box.textContent = ''; it.box.append(canvas); it.done = true; it.canvas = canvas; }
  } catch (e) { console.warn('thumb failed:', e && e.name); }
  if (it.job === job) it.job = null;
}
function freeThumb(it) {
  it.visible = false;
  it.job?.cancel(); it.job = null;
  if (it.canvas) { it.canvas.width = it.canvas.height = 0; it.canvas.remove(); it.canvas = null; }
  it.done = false;
}

function paintSelection() {
  for (const [id, it] of items) {
    const s = state.selectedPages.has(id);
    it.el.classList.toggle('sel', s);
    it.el.setAttribute('aria-selected', s ? 'true' : 'false');
  }
}

function highlight(i) {
  const id = state.doc?.pages[i]?.id;
  for (const [pid, it] of items) {
    const cur = pid === id;
    it.el.classList.toggle('current', cur);
    if (cur) {
      const r = it.el.getBoundingClientRect(), lr = list.getBoundingClientRect();
      if (r.top < lr.top || r.bottom > lr.bottom) it.el.scrollIntoView({ block: 'nearest' });
    }
  }
}
