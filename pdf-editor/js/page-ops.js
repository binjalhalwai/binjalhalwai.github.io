// Page-level operations on the document model (rotate, delete, duplicate, move, insert, crop).
// Each operation mutates state.doc.pages, commits one undo step and emits 'pages' so views re-sync.
import { state, emit } from './state.js';
import * as history from './history.js';
import { rotatePageModel, cropPageModel } from './page-geom.js';
import { uid, toast, confirmDialog } from './utils.js';
import { goToPage } from './viewer.js';

export const targetIds = () => {
  const pages = state.doc.pages;
  const sel = pages.filter(p => state.selectedPages.has(p.id)).map(p => p.id);
  return sel.length ? sel : [pages[Math.min(state.currentPage, pages.length - 1)].id];
};
export const indexOf = id => state.doc.pages.findIndex(p => p.id === id);
const byIds = ids => state.doc.pages.filter(p => ids.includes(p.id));

function done(label, focusIdx, scroll = true) {
  state.currentPage = Math.max(0, Math.min(focusIdx ?? state.currentPage, state.doc.pages.length - 1));
  history.commit(label);
  emit('pages');
  if (scroll) goToPage(state.currentPage);
}

export function rotatePages(ids, steps = 1) {
  const ms = byIds(ids); if (!ms.length) return;
  for (const m of ms) rotatePageModel(m, steps);
  done(steps === 1 ? 'Rotate page' : 'Rotate pages', undefined, false);
}

export async function deletePagesConfirm(ids) {
  if (ids.length >= state.doc.pages.length) { toast('A document needs at least one page.', 'error'); return false; }
  const hasObjs = byIds(ids).some(p => p.objects.length);
  if (ids.length > 1 || hasObjs) {
    if (!(await confirmDialog(`Delete ${ids.length} page${ids.length > 1 ? 's' : ''}?`, 'You can undo this with Ctrl+Z.', 'Delete', true))) return false;
  }
  return deletePages(ids);
}
export function deletePages(ids) {
  if (ids.length >= state.doc.pages.length) { toast('A document needs at least one page.', 'error'); return false; }
  const first = Math.min(...ids.map(indexOf));
  state.doc.pages = state.doc.pages.filter(p => !ids.includes(p.id));
  state.selectedPages = new Set();
  done('Delete pages', Math.min(first, state.doc.pages.length - 1));
  return true;
}

export function clonePage(m) {
  const c = structuredClone(m);
  c.id = uid('p');
  c.objects.forEach(o => { o.id = uid((o.type || 'o')[0]); });
  return c;
}

export function duplicatePages(ids) {
  const copies = [];
  const out = [];
  for (const p of state.doc.pages) {
    out.push(p);
    if (ids.includes(p.id)) { const c = clonePage(p); copies.push(c); out.push(c); }
  }
  state.doc.pages = out;
  state.selectedPages = new Set(copies.map(c => c.id));
  done('Duplicate pages', indexOf(copies[0].id));
  toast(`Duplicated ${copies.length} page${copies.length > 1 ? 's' : ''}.`, 'ok', 2500);
}

/** Move pages (keeping their relative order) to sit before page `beforeId` (null = end). */
export function movePagesTo(ids, beforeId) {
  const moving = state.doc.pages.filter(p => ids.includes(p.id));
  if (!moving.length || ids.includes(beforeId)) return;
  const rest = state.doc.pages.filter(p => !ids.includes(p.id));
  const at = beforeId ? rest.findIndex(p => p.id === beforeId) : rest.length;
  const next = [...rest.slice(0, at), ...moving, ...rest.slice(at)];
  if (next.every((p, i) => p === state.doc.pages[i])) return;
  state.doc.pages = next;
  state.selectedPages = new Set(ids);
  done('Move pages', indexOf(moving[0].id));
}
export function moveStep(ids, dir) {
  const pages = state.doc.pages, idx = ids.map(indexOf).sort((a, b) => a - b);
  if (dir < 0) { if (idx[0] === 0) return; movePagesTo(ids, pages[idx[0] - 1].id); }
  else { const last = idx[idx.length - 1]; if (last >= pages.length - 1) return; const after = pages[last + 1]; const nxt = pages[last + 2]; movePagesTo(ids, nxt ? nxt.id : null); void after; }
}

/** Insert page models at index `at` (0..n). */
export function insertPages(models, at, label = 'Insert pages') {
  const pages = state.doc.pages;
  at = Math.max(0, Math.min(at, pages.length));
  pages.splice(at, 0, ...models);
  state.selectedPages = new Set(models.map(m => m.id));
  done(label, at);
}

export function blankPage(w, h) { return { id: uid('p'), kind: 'blank', w, h, view: [0, 0, w, h], view0: [0, 0, w, h], rot: 0, rot0: 0, objects: [] }; }

/** margins in points {t,r,b,l}; applies to every id. Returns number of pages cropped. */
export function cropPages(ids, mg) {
  let n = 0;
  for (const m of byIds(ids)) {
    const cw = m.w - mg.l - mg.r, ch = m.h - mg.t - mg.b;
    if (cw < 36 || ch < 36) continue;
    cropPageModel(m, mg.l, mg.t, cw, ch); n++;
  }
  if (n) done('Crop pages', undefined, false);
  return n;
}

export function parseRange(text, total) {
  const set = new Set();
  for (const part of String(text).split(/[,\s]+/).filter(Boolean)) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!m) return null;
    let a = +m[1], b = m[2] ? +m[2] : a;
    if (a > b) [a, b] = [b, a];
    if (a < 1 || b > total) return null;
    for (let i = a; i <= b; i++) set.add(i - 1);
  }
  return set.size ? [...set].sort((x, y) => x - y) : null;
}
