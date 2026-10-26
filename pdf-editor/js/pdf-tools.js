// "PDF tools": compress, PDF -> images, PDF -> text, flatten forms (see download dialog). Everything runs in the browser.
import { state } from './state.js';
import pdfjsLib, { DOC_OPTS } from './pdfjs.js';
import { h, field, selectOf, showModal, toast, progressModal, downloadFile, safeFileName, formatBytes } from './utils.js';
import { runExport } from './export-ui.js';
import * as ops from './page-ops.js';
import { getPageText } from './text-index.js';

const tick = () => new Promise(r => setTimeout(r, 0));
const scopePages = (scope, rangeIn) => {
  const total = state.doc.pages.length;
  if (scope === 'all') return state.doc.pages.map((_, i) => i);
  if (scope === 'current') return [state.currentPage];
  if (scope === 'selected') return ops.targetIds().map(ops.indexOf).sort((a, b) => a - b);
  return ops.parseRange(rangeIn, total);
};
const scopeFields = () => {
  const scope = selectOf([['all', 'All pages'], ['current', 'Current page'], ['selected', 'Selected pages'], ['range', 'Page range…']], 'all');
  const rng = h('input', { type: 'text', placeholder: `e.g. 1-3, 7 (1–${state.doc.pages.length})`, hidden: true });
  scope.addEventListener('change', () => { rng.hidden = scope.value !== 'range'; });
  return { scope, rng, nodes: [field('Pages', scope), rng] };
};

/* ---------------- compress ---------------- */
export async function openCompressDialog() {
  const orig = state.doc.size;
  const mode = selectOf([['light', 'Light — keeps text selectable (re-saves the file efficiently)'], ['strong', 'Strong — converts pages to images (smallest, text not selectable)']], 'light');
  const q = selectOf([['72|0.55', 'Small (72 dpi, quality 55%)'], ['110|0.65', 'Balanced (110 dpi, quality 65%)'], ['150|0.75', 'Sharper (150 dpi, quality 75%)']], '110|0.65');
  const qField = field('Image quality (strong mode)', q); qField.hidden = true;
  mode.addEventListener('change', () => { qField.hidden = mode.value !== 'strong'; });
  const info = h('div', { class: 'callout info' }, 'Strong compression redraws every page as an image: forms, links and selectable text are lost, and your edits are flattened in. Use it for scans or when you must meet an upload size limit.');
  info.hidden = true; mode.addEventListener('change', () => { info.hidden = mode.value !== 'strong'; });
  const ok = await showModal({ title: 'Compress PDF', content: [field('Method', mode), qField, info, h('p', { class: 'note' }, orig ? `Current file size: ${formatBytes(orig)}` : '')], actions: [{ label: 'Cancel', value: false }, { label: 'Compress', value: true, primary: true }] }).promise;
  if (!ok) return;
  const pm = progressModal('Compressing…', { cancellable: false });
  let extra = {};
  if (mode.value === 'strong') { const [dpi, quality] = q.value.split('|'); extra = { rasterAll: true, rasterScale: +dpi / 72, rasterQuality: +quality }; }
  const r = await runExport({ onProgress: p => pm.set(p), extra });
  pm.close();
  if (!r) return;
  const saved = orig ? 1 - r.bytes.length / orig : null;
  const msg = orig ? (saved > 0.005 ? `Reduced from ${formatBytes(orig)} to ${formatBytes(r.bytes.length)} (${Math.round(saved * 100)}% smaller).` : `The result (${formatBytes(r.bytes.length)}) is not smaller than the original (${formatBytes(orig)}). ${mode.value === 'light' ? 'Try “Strong”.' : ''}`) : `Result size: ${formatBytes(r.bytes.length)}.`;
  const go = await showModal({ title: 'Compression result', content: [h('p', {}, msg)], actions: [{ label: 'Close', value: false }, { label: 'Download', value: true, primary: true }] }).promise;
  if (go) downloadFile(r.bytes, `${safeFileName(state.doc.name)}-compressed.pdf`, 'application/pdf');
}

/* ---------------- PDF -> images ---------------- */
export async function openPdfToImagesDialog() {
  const fmt = selectOf([['png', 'PNG (lossless)'], ['jpeg', 'JPG (smaller)']], 'jpeg');
  const dpi = selectOf([['72', '72 dpi (screen)'], ['150', '150 dpi (good)'], ['200', '200 dpi'], ['300', '300 dpi (print)']], '150');
  const sf = scopeFields();
  const ok = await showModal({ title: 'Save pages as images', content: [field('Format', fmt), field('Resolution', dpi), ...sf.nodes, h('p', { class: 'note' }, 'Your edits are included. Multiple pages are saved in one ZIP file.')], actions: [{ label: 'Cancel', value: false }, { label: 'Save images', value: true, primary: true }] }).promise;
  if (!ok) return;
  const idx = scopePages(sf.scope.value, sf.rng.value);
  if (!idx) { toast('Please enter a valid page range.', 'error'); return; }
  const pm = progressModal('Rendering pages…');
  const r = await runExport({ pages: idx.map(i => state.doc.pages[i]), onProgress: p => pm.set(p * 0.3, 'Preparing pages…') });
  if (!r) { pm.close(); return; }
  let pdf;
  try {
    pdf = await pdfjsLib.getDocument({ data: r.bytes.slice(), ...DOC_OPTS }).promise;
    const files = [], ext = fmt.value === 'png' ? 'png' : 'jpg', base = safeFileName(state.doc.name);
    for (let i = 1; i <= pdf.numPages; i++) {
      if (pm.cancelled) { pm.close(); toast('Cancelled.', 'info'); return; }
      pm.set(0.3 + 0.7 * (i - 1) / pdf.numPages, `Page ${i} of ${pdf.numPages}…`);
      const pg = await pdf.getPage(i);
      let scale = +dpi.value / 72; const base1 = pg.getViewport({ scale: 1 });
      if (base1.width * base1.height * scale * scale > 40e6) scale = Math.sqrt(40e6 / (base1.width * base1.height));
      const vp = pg.getViewport({ scale });
      const c = document.createElement('canvas'); c.width = Math.ceil(vp.width); c.height = Math.ceil(vp.height);
      await pg.render({ canvasContext: c.getContext('2d', { alpha: false }), viewport: vp, background: '#fff', annotationMode: 1 }).promise;
      const blob = await new Promise(res => c.toBlob(res, fmt.value === 'png' ? 'image/png' : 'image/jpeg', 0.92));
      files.push({ name: `${base}_page${idx[i - 1] + 1}.${ext}`, bytes: new Uint8Array(await blob.arrayBuffer()) });
      c.width = c.height = 0; pg.cleanup(); await tick();
    }
    pm.close();
    if (files.length === 1) { downloadFile(files[0].bytes, files[0].name, `image/${fmt.value}`); return; }
    if (!window.JSZip) { toast('The ZIP library is missing.', 'error'); return; }
    const zip = new window.JSZip(); files.forEach(f => zip.file(f.name, f.bytes));
    downloadFile(await zip.generateAsync({ type: 'uint8array', compression: 'STORE' }), `${base}-images.zip`, 'application/zip');
    toast(`Saved ${files.length} images.`, 'ok');
  } catch (e) { pm.close(); console.warn('pdf->image failed:', e && e.name); toast('Could not render the pages to images.', 'error'); }
  finally { try { pdf && pdf.destroy(); } catch { /* ignore */ } }
}

/* ---------------- PDF -> text ---------------- */
export async function openPdfToTextDialog() {
  const pm = progressModal('Extracting text…');
  const out = []; let withText = 0;
  for (let i = 0; i < state.doc.pages.length; i++) {
    if (pm.cancelled) { pm.close(); return; }
    pm.set(i / state.doc.pages.length, `Page ${i + 1} of ${state.doc.pages.length}…`);
    const m = state.doc.pages[i];
    let text = '';
    try { text = (await getPageText(m)).text.replace(/ \n/g, '\n').replace(/[ \t]+$/gm, '').trim(); } catch { /* skip */ }
    if (!text && m.ocr?.words?.length) text = m.ocr.words.map(w => w[4]).join(' ');
    if (text) withText++;
    out.push(`--- Page ${i + 1} ---\n${text}`);
    await tick();
  }
  pm.close();
  if (!withText) { toast('No text found. If this is a scan, run OCR first.', 'error', 6000); return; }
  downloadFile(new TextEncoder().encode(out.join('\n\n')), `${safeFileName(state.doc.name)}.txt`, 'text/plain;charset=utf-8');
  toast(`Text from ${withText} page${withText > 1 ? 's' : ''} saved.`, 'ok');
}
