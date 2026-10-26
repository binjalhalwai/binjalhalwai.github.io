// Phase 2 dialogs: insert blank pages, merge PDFs, images -> pages, extract, split, crop,
// header / footer / page numbers and watermark. Model changes go through page-ops.js so they are undoable.
import { state } from './state.js';
import * as history from './history.js';
import * as ops from './page-ops.js';
import { h, field, selectOf, numInput, showModal, toast, progressModal, downloadFile, safeFileName, confirmDialog, friendlyError, uid, $ } from './utils.js';
import { PAGE_SIZES, loadPdfSource } from './pdf-import.js';
import { runExport } from './export-ui.js';
import { fileToAsset } from './tools-image.js';
import { newTextObject, newImageObject, insertObject, renderPageObjects, FONT_CSS, FONT_LABEL } from './objects.js';
import { renderPageToCanvas } from './pdf-renderer.js';

const MM = 72 / 25.4;
const cur = () => state.doc.pages[state.currentPage];
const pick = (accept, multiple = true) => new Promise(res => {
  const i = h('input', { type: 'file', accept, multiple, hidden: true });
  i.addEventListener('change', () => { res([...i.files]); i.remove(); });
  i.addEventListener('cancel', () => { res([]); i.remove(); });
  document.body.append(i); i.click();
});

/* ---------------- insert blank pages ---------------- */
export async function openInsertBlankDialog() {
  const sizes = [['same', 'Same size as current page'], ...Object.entries(PAGE_SIZES).map(([k, v]) => [k, v.label])];
  const size = selectOf(sizes, 'same'), orient = selectOf([['p', 'Portrait'], ['l', 'Landscape']], 'p');
  const where = selectOf([['after', 'After the current page'], ['before', 'Before the current page'], ['start', 'At the start'], ['end', 'At the end']], 'after');
  const count = numInput(1, { min: 1, max: 100, 'aria-label': 'Number of pages' });
  const ok = await showModal({ title: 'Insert blank pages', content: [field('Page size', size), field('Orientation (when not “same size”)', orient), field('Position', where), field('How many', count)],
    actions: [{ label: 'Cancel', value: false }, { label: 'Insert', value: true, primary: true }] }).promise;
  if (!ok) return;
  let w, hh;
  if (size.value === 'same') { w = cur().w; hh = cur().h; }
  else { const s = PAGE_SIZES[size.value]; [w, hh] = orient.value === 'l' ? [s.h, s.w] : [s.w, s.h]; }
  const n = Math.max(1, Math.min(100, parseInt(count.value, 10) || 1));
  const at = { start: 0, end: state.doc.pages.length, before: state.currentPage, after: state.currentPage + 1 }[where.value];
  ops.insertPages(Array.from({ length: n }, () => ops.blankPage(w, hh)), at, 'Insert blank pages');
}

/* ---------------- merge PDFs ---------------- */
export async function appendPdfFiles(files, { where = 'end', quiet = false } = {}) {
  const t = quiet ? null : toast('Adding PDF pages…', 'info', 60000);
  const added = [];
  try {
    for (const f of files) {
      try {
        const r = await loadPdfSource(f);
        if (!r) continue;
        state.doc.sources.set(r.srcId, { id: r.srcId, file: f, pdf: r.pdf, encrypted: r.encrypted });
        added.push(...r.pages);
        if (r.encrypted) toast(`“${f.name}” is password-protected: saving will produce a flattened copy.`, 'info', 6000);
      } catch (e) { toast(`Could not add “${f.name}”: ${friendlyError(e, 'It may be damaged.')}`, 'error', 7000); }
    }
    if (!added.length) return 0;
    const at = { start: 0, end: state.doc.pages.length, after: state.currentPage + 1 }[where] ?? state.doc.pages.length;
    ops.insertPages(added, at, 'Add PDF pages');
    if (!quiet) toast(`Added ${added.length} page${added.length > 1 ? 's' : ''}.`, 'ok');
    return added.length;
  } finally { t?.remove(); }
}
export async function openMergeDialog() {
  const files = await pick('application/pdf,.pdf');
  if (!files.length) return;
  const where = selectOf([['end', 'At the end of this document'], ['after', 'After the current page'], ['start', 'At the start']], 'end');
  const list = h('ul', { class: 'filelist' }, files.map(f => h('li', {}, f.name)));
  const ok = await showModal({ title: `Merge ${files.length} PDF${files.length > 1 ? 's' : ''}`, content: [list, field('Insert', where), h('p', { class: 'note' }, 'Pages keep their own size. You can reorder them afterwards in the page list.')],
    actions: [{ label: 'Cancel', value: false }, { label: 'Merge', value: true, primary: true }] }).promise;
  if (ok) await appendPdfFiles(files, { where: where.value });
}

/* ---------------- images -> pages ---------------- */
export async function openImagesToPagesDialog() {
  const files = await pick('image/png,image/jpeg,image/webp,image/gif,image/bmp');
  if (!files.length) return;
  const size = selectOf([['fit', 'Same size as the image'], ['a4', 'A4 (image fitted)'], ['letter', 'US Letter (image fitted)']], 'a4');
  const where = selectOf([['end', 'At the end'], ['after', 'After the current page'], ['start', 'At the start']], 'end');
  const ok = await showModal({ title: `Add ${files.length} image${files.length > 1 ? 's' : ''} as pages`, content: [field('Page size', size), field('Insert', where)],
    actions: [{ label: 'Cancel', value: false }, { label: 'Add pages', value: true, primary: true }] }).promise;
  if (!ok) return;
  const models = [];
  for (const f of files) {
    try {
      const id = await fileToAsset(f), a = state.doc.assets.get(id);
      let pw, ph, x, y, w, hh;
      if (size.value === 'fit') {
        const k = Math.min(1, 1200 / Math.max(a.w, a.h)) * 0.75; pw = w = a.w * k; ph = hh = a.h * k; x = y = 0;
      } else {
        const s = PAGE_SIZES[size.value], land = a.w > a.h; [pw, ph] = land ? [s.h, s.w] : [s.w, s.h];
        const m = 18, k = Math.min((pw - 2 * m) / a.w, (ph - 2 * m) / a.h); w = a.w * k; hh = a.h * k; x = (pw - w) / 2; y = (ph - hh) / 2;
      }
      const page = ops.blankPage(pw, ph);
      page.objects.push(newImageObject('image', id, x, y, w, hh));
      models.push(page);
    } catch (e) { toast(`Could not add “${f.name}”: ${friendlyError(e, 'Unsupported image.')}`, 'error', 6000); }
  }
  if (!models.length) return;
  const at = { start: 0, end: state.doc.pages.length, after: state.currentPage + 1 }[where.value];
  ops.insertPages(models, at, 'Add image pages');
}

/* ---------------- extract / split ---------------- */
const rangeText = idx => {
  const out = []; let s = idx[0], p = idx[0];
  for (let i = 1; i <= idx.length; i++) { if (idx[i] === p + 1) { p = idx[i]; continue; } out.push(s === p ? `${s + 1}` : `${s + 1}-${p + 1}`); s = p = idx[i]; }
  return out.join(', ');
};
const subsetOf = idx => idx.map(i => state.doc.pages[i]);

export async function openExtractDialog() {
  const sel = ops.targetIds().map(ops.indexOf).sort((a, b) => a - b);
  const inp = h('input', { type: 'text', value: rangeText(sel), 'aria-label': 'Pages to extract' });
  const del = h('input', { type: 'checkbox' });
  const ok = await showModal({ title: 'Extract pages to a new PDF', content: [field(`Pages (1–${state.doc.pages.length}, e.g. 1-3, 7)`, inp), h('label', { class: 'checkline' }, del, ' Also remove these pages from this document'), h('p', { class: 'note' }, 'Your edits on those pages are included in the new file.')],
    actions: [{ label: 'Cancel', value: false }, { label: 'Extract & download', value: true, primary: true }] }).promise;
  if (!ok) return;
  const idx = ops.parseRange(inp.value, state.doc.pages.length);
  if (!idx) { toast('Please enter valid page numbers, e.g. 1-3, 7.', 'error'); return; }
  const pm = progressModal('Extracting pages…', { cancellable: false });
  const r = await runExport({ pages: subsetOf(idx), onProgress: p => pm.set(p) });
  pm.close();
  if (!r) return;
  downloadFile(r.bytes, `${safeFileName(state.doc.name)}-pages-${rangeText(idx).replace(/[, ]+/g, '_')}.pdf`, 'application/pdf');
  toast(`Extracted ${idx.length} page${idx.length > 1 ? 's' : ''}.`, 'ok');
  if (del.checked) ops.deletePages(idx.map(i => state.doc.pages[i].id));
}

export async function openSplitDialog() {
  const total = state.doc.pages.length;
  const mode = selectOf([['every', 'Every N pages'], ['ranges', 'Custom ranges (one file per range)'], ['single', 'One file per page']], 'every');
  const n = numInput(1, { min: 1, max: total, 'aria-label': 'Pages per file' });
  const rng = h('input', { type: 'text', placeholder: 'e.g. 1-3, 4-6, 7', 'aria-label': 'Ranges' });
  const nField = field('Pages per file', n), rField = field('Ranges (comma separated)', rng); rField.hidden = true;
  mode.addEventListener('change', () => { nField.hidden = mode.value !== 'every'; rField.hidden = mode.value !== 'ranges'; });
  const ok = await showModal({ title: `Split this PDF (${total} pages)`, content: [field('Split by', mode), nField, rField, h('p', { class: 'note' }, 'Result is a ZIP file containing the parts (or a single PDF if there is only one part).')],
    actions: [{ label: 'Cancel', value: false }, { label: 'Split & download', value: true, primary: true }] }).promise;
  if (!ok) return;
  let parts = [];
  if (mode.value === 'every') { const k = Math.max(1, parseInt(n.value, 10) || 1); for (let i = 0; i < total; i += k) parts.push(Array.from({ length: Math.min(k, total - i) }, (_, j) => i + j)); }
  else if (mode.value === 'single') parts = Array.from({ length: total }, (_, i) => [i]);
  else { for (const seg of rng.value.split(',').map(s => s.trim()).filter(Boolean)) { const idx = ops.parseRange(seg, total); if (!idx) { toast(`Invalid range: “${seg}”.`, 'error'); return; } parts.push(idx); } }
  if (!parts.length) { toast('Nothing to split.', 'error'); return; }
  if (parts.length > 200) { toast('That would create more than 200 files. Please choose larger parts.', 'error'); return; }
  const base = safeFileName(state.doc.name), pm = progressModal('Splitting…');
  const files = [];
  for (let i = 0; i < parts.length; i++) {
    if (pm.cancelled) { pm.close(); toast('Split cancelled.', 'info'); return; }
    pm.set(i / parts.length, `Creating part ${i + 1} of ${parts.length}…`);
    const r = await runExport({ pages: subsetOf(parts[i]) });
    if (!r) { pm.close(); return; }
    files.push({ name: `${base}_part${String(i + 1).padStart(String(parts.length).length, '0')}_p${rangeText(parts[i]).replace(/[, ]+/g, '_')}.pdf`, bytes: r.bytes });
    await new Promise(r2 => setTimeout(r2, 0));
  }
  pm.close();
  if (files.length === 1) { downloadFile(files[0].bytes, files[0].name, 'application/pdf'); return; }
  if (!window.JSZip) { toast('The ZIP library is missing, so the parts cannot be bundled.', 'error'); return; }
  const zip = new window.JSZip();
  for (const f of files) zip.file(f.name, f.bytes);
  const out = await zip.generateAsync({ type: 'uint8array', compression: 'STORE' });
  downloadFile(out, `${base}-split.zip`, 'application/zip');
  toast(`Created ${files.length} files.`, 'ok');
}

/* ---------------- crop ---------------- */
async function detectMargins(m) {
  const S = 0.5, c = await renderPageToCanvas(m, S);
  const { width: W, height: H } = c, d = c.getContext('2d').getImageData(0, 0, W, H).data;
  let x0 = W, y0 = H, x1 = -1, y1 = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) { const i = (y * W + x) * 4; if ((d[i] + d[i + 1] + d[i + 2]) / 3 < 240 && d[i + 3] > 0) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; } }
  c.width = c.height = 0;
  if (x1 < 0) return null;
  const pad = 10;
  return { l: Math.max(0, x0 / S - pad), t: Math.max(0, y0 / S - pad), r: Math.max(0, (W - x1) / S - pad), b: Math.max(0, (H - y1) / S - pad) };
}
export async function openCropDialog() {
  const page = cur(), inputs = {};
  const mk = (k, l) => { inputs[k] = numInput(0, { min: 0, step: 0.5, 'aria-label': `${l} margin (mm)` }); return field(`${l} (mm)`, inputs[k]); };
  const grid = h('div', { class: 'grid2' }, mk('t', 'Top'), mk('b', 'Bottom'), mk('l', 'Left'), mk('r', 'Right'));
  const scope = selectOf([['current', 'Current page only'], ['selected', 'Selected pages'], ['all', 'All pages'], ['odd', 'Odd pages'], ['even', 'Even pages']], state.selectedPages.size > 1 ? 'selected' : 'current');
  const auto = h('button', { class: 'btn', type: 'button' }, 'Detect margins automatically');
  const wrap = h('div', { class: 'crop-wrap' }, h('div', { class: 'crop-rect' }));
  const overlay = state.overlays.get(page.id);
  overlay?.append(wrap);
  const mg = () => ({ t: (+inputs.t.value || 0) * MM, r: (+inputs.r.value || 0) * MM, b: (+inputs.b.value || 0) * MM, l: (+inputs.l.value || 0) * MM });
  const draw = () => { const g = mg(); Object.assign(wrap.firstChild.style, { left: `${g.l}px`, top: `${g.t}px`, width: `${Math.max(0, page.w - g.l - g.r)}px`, height: `${Math.max(0, page.h - g.t - g.b)}px` }); };
  Object.values(inputs).forEach(i => i.addEventListener('input', draw)); draw();
  auto.addEventListener('click', async () => {
    auto.disabled = true;
    try { const g = await detectMargins(page); if (!g) toast('This page looks blank.', 'info'); else { inputs.t.value = (g.t / MM).toFixed(1); inputs.b.value = (g.b / MM).toFixed(1); inputs.l.value = (g.l / MM).toFixed(1); inputs.r.value = (g.r / MM).toFixed(1); draw(); } }
    catch { toast('Could not analyse this page.', 'error'); }
    auto.disabled = false;
  });
  const ok = await showModal({ title: 'Crop pages', content: [h('p', {}, 'Set how much to trim from each edge. The dashed box on the page shows what will remain.'), grid, auto, field('Apply to', scope), h('p', { class: 'note' }, 'Cropping hides the outside area (it sets the PDF CropBox); the hidden content is not deleted from the file.')],
    actions: [{ label: 'Cancel', value: false }, { label: 'Crop', value: true, primary: true }] }).promise;
  wrap.remove();
  if (!ok) return;
  const pages = state.doc.pages;
  const ids = { current: [page.id], selected: ops.targetIds(), all: pages.map(p => p.id), odd: pages.filter((_, i) => i % 2 === 0).map(p => p.id), even: pages.filter((_, i) => i % 2 === 1).map(p => p.id) }[scope.value];
  const g = mg();
  if (!(g.t || g.r || g.b || g.l)) { toast('Enter at least one margin.', 'info'); return; }
  const n = ops.cropPages(ids, g);
  toast(n ? `Cropped ${n} page${n > 1 ? 's' : ''}.` : 'The margins are too large for these pages.', n ? 'ok' : 'error');
}

/* ---------------- header / footer / page numbers ---------------- */
let measureCtx = null;
export function measureTextWidth(text, font, size, bold = false, italic = false) {
  measureCtx ||= document.createElement('canvas').getContext('2d');
  measureCtx.font = `${italic ? 'italic ' : ''}${bold ? '700 ' : '400 '}${size}px ${FONT_CSS[font] || FONT_CSS.helvetica}`;
  return measureCtx.measureText(text).width;
}
function textObj(text, o = {}) {
  const t = newTextObject(0, 0);
  Object.assign(t, { text, font: 'helvetica', size: 10, color: '#444444', align: 'left', bg: null, ...o });
  const w = measureTextWidth(text, t.font, t.size, t.bold, t.italic);
  t.w = Math.ceil(w * 1.08) + 8; t.h = t.size * 1.2;
  return t;
}
function stripFlag(flag) {
  let n = 0;
  for (const p of state.doc.pages) { const k = p.objects.length; p.objects = p.objects.filter(o => !o[flag]); if (p.objects.length !== k) { n += k - p.objects.length; renderPageObjects(p.id); } }
  if (n) history.commit('Remove items');
  return n;
}
function pageSet(scope, rangeInput) {
  const total = state.doc.pages.length;
  if (scope === 'all') return state.doc.pages.map((_, i) => i);
  if (scope === 'current') return [state.currentPage];
  return ops.parseRange(rangeInput, total);
}
const fmtDate = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };

export async function openHeaderFooterDialog() {
  const total = state.doc.pages.length;
  const preset = selectOf([['Page {n}', 'Page 1'], ['{n}', '1'], ['Page {n} of {N}', 'Page 1 of 10'], ['{n} / {N}', '1 / 10'], ['', 'Custom text…']], 'Page {n} of {N}', { 'aria-label': 'Format' });
  const text = h('input', { type: 'text', value: 'Page {n} of {N}', maxlength: '200', 'aria-label': 'Text' });
  preset.addEventListener('change', () => { if (preset.value) text.value = preset.value; else text.focus(); });
  const pos = selectOf([['b-c', 'Footer — centre'], ['b-r', 'Footer — right'], ['b-l', 'Footer — left'], ['t-c', 'Header — centre'], ['t-r', 'Header — right'], ['t-l', 'Header — left']], 'b-c');
  const size = numInput(10, { min: 6, max: 48 }), col = h('input', { type: 'color', value: '#444444' }), mar = numInput(28, { min: 0, max: 200 }), start = numInput(1, { min: -999, max: 99999 });
  const scope = selectOf([['all', 'All pages'], ['current', 'Current page'], ['range', 'Page range…']], 'all');
  const rng = h('input', { type: 'text', placeholder: `e.g. 2-${total}`, hidden: true });
  scope.addEventListener('change', () => { rng.hidden = scope.value !== 'range'; });
  const skip = h('input', { type: 'checkbox' });
  const res = await showModal({ title: 'Header, footer & page numbers', wide: true,
    content: [field('Format', preset), field('Text — use {n} page number, {N} total pages, {date}, {file}', text), h('div', { class: 'grid2' }, field('Position', pos), field('Font size (pt)', size), field('Colour', col), field('Margin from edge (pt)', mar), field('First page number', start), field('Pages', scope)), rng, h('label', { class: 'checkline' }, skip, ' Skip the first page of the selection')],
    actions: [{ label: 'Cancel', value: 'x' }, { label: 'Remove existing', value: 'rm', danger: true }, { label: 'Add', value: 'ok', primary: true }] }).promise;
  if (res === 'rm') { const n = stripFlag('hf'); toast(n ? `Removed ${n} header/footer item${n > 1 ? 's' : ''}.` : 'No headers or footers to remove.', n ? 'ok' : 'info'); return; }
  if (res !== 'ok') return;
  let idx = pageSet(scope.value, rng.value);
  if (!idx) { toast('Please enter a valid page range.', 'error'); return; }
  if (skip.checked) idx = idx.slice(1);
  const [vert, hor] = pos.value.split('-'), s = Math.max(6, +size.value || 10), margin = Math.max(0, +mar.value || 0), first = parseInt(start.value, 10) || 1;
  for (const i of idx) {
    const p = state.doc.pages[i];
    const t = text.value.replace(/\{n\}/g, String(i + first)).replace(/\{N\}/g, String(total)).replace(/\{date\}/g, fmtDate()).replace(/\{file\}/g, state.doc.name);
    if (!t.trim()) continue;
    const o = textObj(t, { size: s, color: col.value, align: hor === 'l' ? 'left' : hor === 'r' ? 'right' : 'center' });
    o.hf = true;
    o.x = hor === 'l' ? margin : hor === 'r' ? p.w - margin - o.w : (p.w - o.w) / 2;
    o.y = vert === 't' ? margin : p.h - margin - o.h;
    insertObject(p.id, o);
  }
  history.commit('Header / footer');
  toast(`Added to ${idx.length} page${idx.length > 1 ? 's' : ''}. Click an item to move or edit it.`, 'ok');
}

/* ---------------- watermark ---------------- */
export async function openWatermarkDialog() {
  const total = state.doc.pages.length;
  let imgAsset = null, tab = 'text';
  const text = h('input', { type: 'text', value: 'CONFIDENTIAL', maxlength: '60', 'aria-label': 'Watermark text' });
  const size = numInput(72, { min: 10, max: 300 }), col = h('input', { type: 'color', value: '#9aa3b2' });
  const angle = selectOf([['-45', 'Diagonal ↗'], ['0', 'Horizontal'], ['45', 'Diagonal ↘']], '-45');
  const op = h('input', { type: 'range', min: 5, max: 100, value: 25 });
  const scope = selectOf([['all', 'All pages'], ['current', 'Current page'], ['range', 'Page range…']], 'all');
  const rng = h('input', { type: 'text', placeholder: `e.g. 1-${total}`, hidden: true });
  scope.addEventListener('change', () => { rng.hidden = scope.value !== 'range'; });
  const imgBtn = h('button', { class: 'btn', type: 'button' }, 'Choose image…'), imgName = h('span', { class: 'note' }, ' none selected');
  imgBtn.addEventListener('click', async () => { const [f] = await pick('image/png,image/jpeg,image/webp', false); if (!f) return; try { imgAsset = await fileToAsset(f); imgName.textContent = ` ${f.name}`; } catch (e) { toast(friendlyError(e, 'Unsupported image.'), 'error'); } });
  const tTab = h('button', { type: 'button', class: 'on' }, 'Text'), iTab = h('button', { type: 'button' }, 'Image');
  const tBox = h('div', {}, field('Text', text), h('div', { class: 'grid2' }, field('Font size (pt)', size), field('Colour', col)), field('Angle', angle));
  const iBox = h('div', { hidden: true }, field('Image', h('div', {}, imgBtn, imgName)));
  const setTab = t => { tab = t; tTab.classList.toggle('on', t === 'text'); iTab.classList.toggle('on', t === 'image'); tBox.hidden = t !== 'text'; iBox.hidden = t !== 'image'; };
  tTab.addEventListener('click', () => setTab('text')); iTab.addEventListener('click', () => setTab('image'));
  const res = await showModal({ title: 'Watermark', wide: true,
    content: [h('div', { class: 'tabs' }, tTab, iTab), tBox, iBox, field('Opacity', op), field('Pages', scope), rng],
    actions: [{ label: 'Cancel', value: 'x' }, { label: 'Remove watermarks', value: 'rm', danger: true }, { label: 'Add watermark', value: 'ok', primary: true }] }).promise;
  if (res === 'rm') { const n = stripFlag('wm'); toast(n ? `Removed ${n} watermark${n > 1 ? 's' : ''}.` : 'No watermarks to remove.', n ? 'ok' : 'info'); return; }
  if (res !== 'ok') return;
  const idx = pageSet(scope.value, rng.value);
  if (!idx) { toast('Please enter a valid page range.', 'error'); return; }
  if (tab === 'image' && !imgAsset) { toast('Choose an image first.', 'error'); return; }
  if (tab === 'text' && !text.value.trim()) { toast('Enter the watermark text.', 'error'); return; }
  for (const i of idx) {
    const p = state.doc.pages[i];
    let o;
    if (tab === 'text') {
      o = textObj(text.value.trim(), { size: Math.max(10, +size.value || 72), color: col.value, align: 'center', bold: true });
      o.rot = +angle.value;
    } else {
      const a = state.doc.assets.get(imgAsset), w = Math.min(p.w * 0.6, a.w), hh = w * a.h / a.w;
      o = newImageObject('image', imgAsset, 0, 0, w, hh);
    }
    o.opacity = (+op.value || 25) / 100; o.wm = true;
    o.x = (p.w - o.w) / 2; o.y = (p.h - o.h) / 2;
    insertObject(p.id, o);
  }
  history.commit('Watermark');
  toast(`Watermark added to ${idx.length} page${idx.length > 1 ? 's' : ''}.`, 'ok');
}
void uid; void $; void confirmDialog; void FONT_LABEL;
