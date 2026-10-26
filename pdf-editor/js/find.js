// Find / find & replace over the PDF's existing text, and "edit existing text" (cover the original and type new text).
// IMPORTANT: replacing text places a patch over the original and writes new text on top; the original characters stay in the
// file underneath. Use the Redaction tool when content must really be removed.
import { state, on, emit, setTool } from './state.js';
import * as history from './history.js';
import { h, $, icon, toast, confirmDialog, debounce } from './utils.js';
import { getTextItems, getPageText, subRect, measure } from './text-index.js';
import { newCoverObject, newTextObject, insertObject, select } from './objects.js';
import { BASELINE } from './text-layout.js';
import { getPageCanvas, scrollToPageRect } from './viewer.js';
import { beginEdit } from './tools-text.js';

let bar, qIn, rIn, cnt, caseChk, rRow;
let matches = [], cur = -1, token = 0, mode = 'find';

/* ---------- colour sampling from the rendered page ---------- */
function sampleColors(pageId, rect) {
  const canvas = getPageCanvas(pageId), m = state.doc.pages.find(p => p.id === pageId);
  if (!canvas || !m) return { bg: '#ffffff', fg: '#000000' };
  const s = canvas.width / m.w, ctx = canvas.getContext('2d', { willReadFrequently: true });
  const px = (x, y) => { const X = Math.max(0, Math.min(canvas.width - 1, Math.round(x * s))), Y = Math.max(0, Math.min(canvas.height - 1, Math.round(y * s))); return ctx.getImageData(X, Y, 1, 1).data; };
  const votes = new Map();
  const add = d => { const k = `${d[0] >> 4},${d[1] >> 4},${d[2] >> 4}`; const v = votes.get(k) || { n: 0, d }; v.n++; votes.set(k, v); };
  for (let i = 0; i <= 8; i++) { const t = i / 8; add(px(rect.x + rect.w * t, rect.y - 1)); add(px(rect.x + rect.w * t, rect.y + rect.h + 1)); }
  add(px(rect.x - 1.5, rect.y + rect.h / 2)); add(px(rect.x + rect.w + 1.5, rect.y + rect.h / 2));
  const bgd = [...votes.values()].sort((a, b) => b.n - a.n)[0].d;
  let best = 0, fg = [0, 0, 0];
  const w = Math.max(1, Math.round(rect.w * s)), hh = Math.max(1, Math.round(rect.h * s));
  const data = ctx.getImageData(Math.max(0, Math.round(rect.x * s)), Math.max(0, Math.round(rect.y * s)), Math.min(w, canvas.width), Math.min(hh, canvas.height)).data;
  for (let i = 0; i < data.length; i += 4) { const dist = Math.abs(data[i] - bgd[0]) + Math.abs(data[i + 1] - bgd[1]) + Math.abs(data[i + 2] - bgd[2]); if (dist > best) { best = dist; fg = [data[i], data[i + 1], data[i + 2]]; } }
  const hex = c => `#${[c[0], c[1], c[2]].map(v => v.toString(16).padStart(2, '0')).join('')}`;
  return { bg: hex(bgd), fg: best > 120 ? hex(fg) : '#000000' };
}

/** cover characters [a,b) of a text item with a patch and place `newStr` on top. Returns the new text object (not yet committed). */
export function coverAndWrite(pageId, it, a, b, newStr) {
  const rect = subRect(it, a, b), { bg, fg } = sampleColors(pageId, rect);
  const pad = 0.8;
  const cover = newCoverObject('whiteout', rect.x - pad, rect.y - pad, rect.w + pad * 2, rect.h + pad * 2);
  cover.color = bg; cover.tx = true;
  const t = newTextObject(rect.x, rect.y);
  Object.assign(t, { text: newStr, font: it.font, bold: it.bold, italic: it.italic, size: Math.max(4, +it.fs.toFixed(2)), color: fg, align: 'left', underline: false, bg: null });
  t.w = Math.max(rect.w, Math.ceil(measure(newStr, it.font, t.size, it.bold, it.italic) * 1.1) + 6);
  t.h = t.size * 1.2;
  t.x = rect.x; t.y = it.baseY - BASELINE[it.font] * t.size;
  insertObject(pageId, cover); insertObject(pageId, t);
  return t;
}

/* ---------- edit existing text (tool) ---------- */
const hitLayers = new Map();
async function buildHits(pageIdx) {
  const m = state.doc?.pages[pageIdx]; if (!m || m.kind !== 'pdf') return;
  const overlay = state.overlays.get(m.id); if (!overlay) return;
  let layer = hitLayers.get(m.id);
  if (!layer || layer.parentNode !== overlay) { layer?.remove(); layer = h('div', { class: 'edit-hits' }); overlay.append(layer); hitLayers.set(m.id, layer); }
  layer.textContent = '';
  const items = await getTextItems(m);
  if (state.tool !== 'edittext') return;
  const covers = m.objects.filter(o => o.tx);
  items.forEach((it, i) => {
    if (!it.horizontal) return;
    const done = covers.some(c => it.x + it.w / 2 >= c.x && it.x + it.w / 2 <= c.x + c.w && it.y + it.h / 2 >= c.y && it.y + it.h / 2 <= c.y + c.h);
    const el = h('div', { class: `tx-hit${done ? ' done' : ''}`, title: 'Click to edit this text' });
    Object.assign(el.style, { left: `${it.x}px`, top: `${it.y}px`, width: `${it.w}px`, height: `${it.h}px` });
    el._item = it; layer.append(el);
  });
}
export function refreshHits() {
  for (const [id, l] of hitLayers) { if (state.tool !== 'edittext' || !l.isConnected) { l.remove(); hitLayers.delete(id); } }
  if (state.tool !== 'edittext' || !state.doc) return;
  for (let d = -1; d <= 2; d++) buildHits(state.currentPage + d);
}
export function editHit(hitEl) {
  const overlay = hitEl.closest('.overlay'), pageId = overlay.dataset.pageId, it = hitEl._item;
  const t = coverAndWrite(pageId, it, 0, it.str.length, it.str);
  history.commit('Edit text');
  hitEl.classList.add('done');
  setTool('select'); select(t.id);
  beginEdit(t.id, { selectAll: true });
}

/* ---------- find ---------- */
function clearHighlights() { document.querySelectorAll('.find-hit').forEach(n => n.remove()); }
function paint() {
  clearHighlights();
  matches.forEach((mt, i) => {
    const overlay = state.overlays.get(mt.pageId); if (!overlay) return;
    for (const r of mt.rects) {
      const el = h('div', { class: `find-hit${i === cur ? ' cur' : ''}` });
      Object.assign(el.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
      overlay.append(el);
    }
  });
  cnt.textContent = matches.length ? `${cur + 1} / ${matches.length}` : (qIn.value ? 'No matches' : '');
}
async function search() {
  const q = qIn.value, my = ++token;
  matches = []; cur = -1;
  if (!q) { paint(); return; }
  const cs = caseChk.checked, needle = cs ? q : q.toLowerCase();
  let scanned = 0, textPages = 0;
  for (const m of state.doc.pages) {
    if (my !== token) return;
    const { items, text, starts } = await getPageText(m);
    scanned++; if (items.length) textPages++;
    const hay = cs ? text : text.toLowerCase();
    const covers = m.objects.filter(o => o.tx);
    let from = 0, pos;
    while ((pos = hay.indexOf(needle, from)) >= 0 && matches.length < 3000) {
      from = pos + Math.max(1, needle.length);
      const end = pos + needle.length, parts = [];
      items.forEach((it, i) => { const s = starts[i], e = s + it.str.length; if (pos < e && end > s) parts.push({ i, a: Math.max(0, pos - s), b: Math.min(it.str.length, end - s) }); });
      if (!parts.length) continue;
      const rects = parts.map(p => subRect(items[p.i], p.a, p.b));
      const r0 = rects[0];
      if (covers.some(c => r0.x + r0.w / 2 >= c.x && r0.x + r0.w / 2 <= c.x + c.w && r0.y + r0.h / 2 >= c.y && r0.y + r0.h / 2 <= c.y + c.h)) continue;   // already replaced
      matches.push({ pageId: m.id, rects, parts, items });
    }
  }
  if (my !== token) return;
  cur = matches.length ? 0 : -1;
  paint();
  if (!matches.length && !textPages) cnt.textContent = 'No text layer (scanned page? try OCR)';
  if (cur >= 0) go(0);
}
function go(i) {
  if (!matches.length) return;
  cur = (i + matches.length) % matches.length;
  paint();
  const mt = matches[cur]; scrollToPageRect(mt.pageId, mt.rects[0].y);
}

/* ---------- replace ---------- */
function replaceMatch(mt, str) {
  if (mt.parts.length !== 1) return false;
  const p = mt.parts[0];
  coverAndWrite(mt.pageId, mt.items[p.i], p.a, p.b, str);
  return true;
}
async function doReplace(all) {
  if (!matches.length) return;
  const str = rIn.value;
  if (all) {
    if (!(await confirmDialog('Replace all?', `Replace ${matches.length} match${matches.length > 1 ? 'es' : ''} with “${str}”? The original text stays hidden underneath. You can undo with Ctrl+Z.`, 'Replace all'))) return;
    let n = 0, skipped = 0;
    for (const mt of matches) (replaceMatch(mt, str) ? n++ : skipped++);
    if (n) history.commit('Replace text');
    toast(`Replaced ${n}${skipped ? `; skipped ${skipped} that span several text pieces` : ''}.`, n ? 'ok' : 'error');
  } else {
    const mt = matches[cur];
    if (!replaceMatch(mt, str)) { toast('This match spans several text pieces. Use “Edit existing text” for it.', 'error'); return; }
    history.commit('Replace text');
  }
  search();
}

/* ---------- UI ---------- */
function buildBar() {
  qIn = h('input', { type: 'search', placeholder: 'Find in document', 'aria-label': 'Find text', autocomplete: 'off' });
  rIn = h('input', { type: 'text', placeholder: 'Replace with', 'aria-label': 'Replace with', autocomplete: 'off' });
  cnt = h('span', { class: 'fcount', 'aria-live': 'polite' });
  caseChk = h('input', { type: 'checkbox', 'aria-label': 'Match case' });
  const btn = (label, ic, fn) => h('button', { type: 'button', class: 'fb', title: label, 'aria-label': label, onclick: fn }, icon(ic));
  rRow = h('div', { class: 'frow', hidden: true }, rIn,
    h('button', { type: 'button', class: 'btn', onclick: () => doReplace(false) }, 'Replace'),
    h('button', { type: 'button', class: 'btn', onclick: () => doReplace(true) }, 'Replace all'));
  const note = h('div', { class: 'fnote', hidden: true }, 'Replacing covers the old text with a patch and writes new text over it. The original stays in the file underneath.');
  bar = h('div', { class: 'findbar', role: 'search', hidden: true },
    h('div', { class: 'frow' }, qIn, cnt, h('label', { class: 'fcase', title: 'Match case' }, caseChk, 'Aa'),
      btn('Previous match', 'chevron-left', () => go(cur - 1)), btn('Next match', 'chevron-right', () => go(cur + 1)), btn('Close', 'x', closeFind)),
    rRow, note);
  bar._note = note;
  document.body.append(bar);
  const run = debounce(search, 250);
  qIn.addEventListener('input', run);
  caseChk.addEventListener('change', search);
  qIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(cur + (e.shiftKey ? -1 : 1)); } if (e.key === 'Escape') { e.stopPropagation(); closeFind(); } });
  rIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); doReplace(false); } if (e.key === 'Escape') closeFind(); });
}
export function openFind(m = 'find') {
  if (!state.doc) return;
  if (!bar) buildBar();
  mode = m; bar.hidden = false; rRow.hidden = m !== 'replace'; bar._note.hidden = m !== 'replace';
  qIn.focus(); qIn.select();
  if (qIn.value) search();
}
export function closeFind() { if (!bar) return; bar.hidden = true; token++; matches = []; cur = -1; clearHighlights(); }
export const isFindOpen = () => !!bar && !bar.hidden;

export function init() {
  on('tool', refreshHits); on('page', refreshHits); on('pages:synced', () => { refreshHits(); if (isFindOpen() && qIn.value) search(); });
  on('doc:restore', () => { if (isFindOpen() && qIn.value) setTimeout(search, 50); });
  on('doc:open', () => { closeFind(); hitLayers.clear(); });
  void emit; void mode;
}
