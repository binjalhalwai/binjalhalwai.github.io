// OCR with Tesseract.js, fully local: the engine (WebAssembly) and language data are vendored in /vendor/tesseract.
// Output: an invisible text layer stored on the page (page.ocr.words) that makes scanned pages searchable/selectable after saving.
import { state } from './state.js';
import * as history from './history.js';
import * as ops from './page-ops.js';
import { h, field, selectOf, showModal, toast, progressModal } from './utils.js';
import { renderPageToCanvas } from './pdf-renderer.js';

/* global __STANDALONE__ */
const standalone = typeof __STANDALONE__ !== 'undefined';
const LANGS = [['eng', 'English'], ['nep', 'Nepali (नेपाली)'], ['hin', 'Hindi (हिन्दी)'], ['eng+nep', 'English + Nepali']];

export async function openOcrDialog() {
  if (standalone || !window.Tesseract) {
    await showModal({ title: 'OCR is available on the website version', content: [h('p', {}, 'OCR needs its engine and language files (about 12 MB), which are not bundled into the single-file test build.'), h('p', {}, 'Open the editor from your website (or a local web server) to use OCR.')], actions: [{ label: 'OK', value: true, primary: true }] }).promise;
    return;
  }
  const lang = selectOf(LANGS, 'eng', { 'aria-label': 'Language' });
  const scope = selectOf([['all', 'All pages'], ['current', 'Current page'], ['selected', 'Selected pages'], ['range', 'Page range…']], 'all');
  const rng = h('input', { type: 'text', placeholder: `e.g. 1-3, 7 (1–${state.doc.pages.length})`, hidden: true });
  scope.addEventListener('change', () => { rng.hidden = scope.value !== 'range'; });
  const ok = await showModal({ title: 'Recognize text (OCR)', content: [field('Language of the document', lang), field('Pages', scope), rng,
    h('p', { class: 'note' }, 'Runs on your computer; nothing is uploaded. It can take several seconds per page. Saved files get an invisible text layer for Latin text (searchable/selectable). Nepali / Hindi text is recognised and can be exported with “PDF to text”, but standard PDF fonts cannot embed Devanagari in the saved PDF.')],
    actions: [{ label: 'Cancel', value: false }, { label: 'Start OCR', value: true, primary: true }] }).promise;
  if (!ok) return;
  const total = state.doc.pages.length;
  const idx = scope.value === 'all' ? state.doc.pages.map((_, i) => i) : scope.value === 'current' ? [state.currentPage] : scope.value === 'selected' ? ops.targetIds().map(ops.indexOf).sort((a, b) => a - b) : ops.parseRange(rng.value, total);
  if (!idx) { toast('Please enter a valid page range.', 'error'); return; }
  await runOcr(idx, lang.value);
}

async function runOcr(idx, lang) {
  const pm = progressModal('Recognizing text…');
  let worker, done = 0, words = 0;
  const results = [];
  try {
    const base = new URL('../vendor/tesseract/', import.meta.url).href;
    let pageProgress = 0;
    pm.set(0, 'Loading OCR engine…');
    worker = await window.Tesseract.createWorker(lang, 1, {
      workerPath: `${base}worker.min.js`, corePath: base.replace(/\/$/, ''), langPath: `${base}lang`, workerBlobURL: false, gzip: true, cacheMethod: 'none',
      logger: m => { if (m.status === 'recognizing text') { pageProgress = m.progress; pm.set((done + pageProgress) / idx.length, `Page ${idx[Math.min(done, idx.length - 1)] + 1} (${done + 1} of ${idx.length})…`); } },
    });
    for (const i of idx) {
      if (pm.cancelled) break;
      const m = state.doc.pages[i], S = Math.min(200 / 72, Math.sqrt(16e6 / (m.w * m.h)));
      const canvas = await renderPageToCanvas(m, S);
      const { data } = await worker.recognize(canvas, {}, { blocks: true });
      canvas.width = canvas.height = 0;
      const ws = [];
      for (const b of data.blocks || []) for (const p of b.paragraphs || []) for (const l of p.lines || []) for (const w of l.words || []) {
        const t = (w.text || '').trim();
        if (!t || w.confidence < 30) continue;
        ws.push([w.bbox.x0 / S, w.bbox.y0 / S, (w.bbox.x1 - w.bbox.x0) / S, (w.bbox.y1 - w.bbox.y0) / S, t]);
      }
      results.push({ id: m.id, words: ws }); words += ws.length; done++;
    }
  } catch (e) {
    console.warn('ocr failed:', e && e.name);
    pm.close();
    toast('OCR could not run in this browser. Make sure the vendor/tesseract files are deployed with the editor.', 'error', 8000);
    try { worker && await worker.terminate(); } catch { /* ignore */ }
    return;
  }
  try { await worker.terminate(); } catch { /* ignore */ }
  pm.close();
  if (!results.length) { toast('OCR cancelled.', 'info'); return; }
  for (const r of results) { const m = state.doc.pages.find(p => p.id === r.id); if (m) m.ocr = { words: r.words, lang }; }
  history.commit('OCR');
  const text = results.map(r => r.words.map(w => w[4]).join(' ')).join('\n\n');
  const go = await showModal({ title: 'OCR finished', content: [h('p', {}, `Recognised ${words} words on ${results.length} page${results.length > 1 ? 's' : ''}. They are saved as an invisible text layer (Latin text) when you download.`), h('textarea', { class: 'ocr-out', readonly: true, rows: 8 }, text.slice(0, 20000))],
    actions: [{ label: 'Close', value: false }, { label: 'Copy text', value: true, primary: true }] }).promise;
  if (go) { try { await navigator.clipboard.writeText(text); toast('Text copied.', 'ok'); } catch { toast('Could not copy automatically; select the text and copy it.', 'error'); } }
}
