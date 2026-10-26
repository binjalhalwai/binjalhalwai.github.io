// Opening PDFs (file picker / drop) and creating blank documents.
import pdfjsLib, { DOC_OPTS } from './pdfjs.js';
import { state, emit } from './state.js';
import * as history from './history.js';
import { clearRendererCache } from './pdf-renderer.js';
import { uid, h, showModal, friendlyError, toast, confirmDialog, formatBytes } from './utils.js';

const BIG_FILE = 150 * 1024 * 1024;
const MANY_PAGES = 1500;

export const PAGE_SIZES = {
  a4: { label: 'A4 (210 × 297 mm)', w: 595.28, h: 841.89 },
  letter: { label: 'US Letter (8.5 × 11 in)', w: 612, h: 792 },
  legal: { label: 'US Legal (8.5 × 14 in)', w: 612, h: 1008 },
  a5: { label: 'A5 (148 × 210 mm)', w: 419.53, h: 595.28 },
};

function askPassword(wrong) {
  const input = h('input', { type: 'password', autocomplete: 'off', 'aria-label': 'PDF password' });
  const m = showModal({
    title: 'Password required',
    content: [h('p', {}, wrong ? 'That password was incorrect. Please try again.' : 'This PDF is password-protected. Enter the password to open it. It is only used in your browser.'), h('div', { class: 'field' }, input)],
    actions: [{ label: 'Cancel', value: null }, { label: 'Open', value: 'ok', primary: true }],
  });
  input.addEventListener('keydown', e => { if (e.key === 'Enter') m.close('ok'); });
  return m.promise.then(v => (v === 'ok' ? input.value : null));
}

function disposeDoc() {
  const d = state.doc;
  if (!d) return;
  for (const a of d.assets.values()) URL.revokeObjectURL(a.url);
  for (const s of d.sources.values()) { try { s.pdf && s.pdf.destroy(); } catch { /* ignore */ } }
  clearRendererCache();
  state.doc = null;
  state.selection = null;
  state.pending = null;
}

async function readHeaderOk(file) {
  const head = new Uint8Array(await file.slice(0, 1024).arrayBuffer());
  const s = new TextDecoder('latin1').decode(head);
  return s.includes('%PDF-');
}

/** Parse a PDF file with pdf.js and build its page models. Throws on failure; returns null if the user cancelled the password prompt. */
export async function loadPdfSource(file, onProgress = () => {}) {
  let cancelled = false, usedPassword = false;
  const buf = new Uint8Array(await file.arrayBuffer());
  const task = pdfjsLib.getDocument({ data: buf, ...DOC_OPTS });
  task.onPassword = async (update, reason) => {
    usedPassword = true;
    const pw = await askPassword(reason === pdfjsLib.PasswordResponses.INCORRECT_PASSWORD);
    if (pw == null) { cancelled = true; task.destroy(); return; }
    update(pw);
  };
  let pdf;
  try { pdf = await task.promise; }
  catch (e) { if (cancelled) return null; throw e; }
  const srcId = uid('s');
  const pages = [];
  const CH = 40;
  for (let start = 1; start <= pdf.numPages; start += CH) {
    const idx = Array.from({ length: Math.min(CH, pdf.numPages - start + 1) }, (_, k) => start + k);
    const got = await Promise.all(idx.map(i => pdf.getPage(i)));
    got.forEach((pg, k) => {
      const vp = pg.getViewport({ scale: 1 });
      pages.push({ id: uid('p'), kind: 'pdf', src: srcId, srcIndex: idx[k] - 1, w: vp.width, h: vp.height, view: pg.view.slice(), view0: pg.view.slice(), rot: pg.rotate % 360, rot0: pg.rotate % 360, objects: [] });
    });
    onProgress(Math.min(start + CH - 1, pdf.numPages), pdf.numPages);
  }
  return { srcId, pdf, pages, encrypted: usedPassword, task };
}

export async function openPdfFile(file) {
  if (!file) return false;
  if (state.doc && state.dirty && !(await confirmDialog('Open another PDF?', 'You have unsaved changes. Opening another PDF will discard them.', 'Discard & open', true))) return false;
  if (!(await readHeaderOk(file))) { toast('That file does not look like a PDF.', 'error'); return false; }
  if (file.size > BIG_FILE && !(await confirmDialog('Very large PDF', `This file is ${formatBytes(file.size)}. Large files may be slow or run out of browser memory. Continue?`, 'Continue'))) return false;

  const progress = toast('Opening PDF…', 'info', 60000);
  try {
    const r = await loadPdfSource(file, (n, total) => { progress.textContent = `Opening PDF… ${n} / ${total} pages`; });
    if (!r) return false;
    if (r.pdf.numPages > MANY_PAGES) toast(`This PDF has ${r.pdf.numPages} pages. Pages load as you scroll; some actions may take longer.`, 'info', 6000);
    disposeDoc();
    state.doc = {
      name: file.name.replace(/\.pdf$/i, '') || 'document',
      size: file.size,
      pages: r.pages,
      sources: new Map([[r.srcId, { id: r.srcId, file, pdf: r.pdf, encrypted: r.encrypted }]]),
      assets: new Map(),
    };
    state.currentPage = 0;
    history.reset();
    emit('doc:open');
    if (r.encrypted) toast('Password accepted. Note: saving a password-protected PDF creates a flattened copy.', 'info', 6000);
    return true;
  } catch (e) {
    const detail = `${(e && e.name) || 'Error'}: ${String((e && e.message) || '').slice(0, 140)}`;   // technical hint only (no file contents)
    toast(`${friendlyError(e, 'This PDF could not be opened. It may be corrupted or use a feature this browser editor does not support.')} (${detail})`, 'error', 12000);
    console.warn('open failed:', e && e.name);
    return false;
  } finally {
    progress.remove();
  }
}

export async function openFiles(fileList) {
  const files = [...fileList].filter(f => f && (f.type === 'application/pdf' || /\.pdf$/i.test(f.name)));
  if (!files.length) { toast('Please choose a PDF file.', 'error'); return false; }
  if (files.length > 1) {
    if (!(await openPdfFile(files[0]))) return false;
    const { appendPdfFiles } = await import('./tools-pages.js');
    await appendPdfFiles(files.slice(1), { where: 'end', quiet: true });
    toast(`Opened ${files.length} PDFs and merged them into one document.`, 'ok', 5000);
    return true;
  }
  return openPdfFile(files[0]);
}

export async function createBlankDocument() {
  const sel = h('select', { 'aria-label': 'Page size' }, Object.entries(PAGE_SIZES).map(([k, v]) => h('option', { value: k }, v.label)));
  const orient = h('select', { 'aria-label': 'Orientation' }, h('option', { value: 'p' }, 'Portrait'), h('option', { value: 'l' }, 'Landscape'));
  const r = await showModal({
    title: 'Create a new PDF',
    content: [h('div', { class: 'field' }, h('label', {}, 'Page size'), sel), h('div', { class: 'field' }, h('label', {}, 'Orientation'), orient)],
    actions: [{ label: 'Cancel', value: false }, { label: 'Create', value: true, primary: true }],
  }).promise;
  if (!r) return false;
  if (state.doc && state.dirty && !(await confirmDialog('Discard current work?', 'You have unsaved changes that will be lost.', 'Discard', true))) return false;
  const sz = PAGE_SIZES[sel.value];
  const land = orient.value === 'l';
  disposeDoc();
  state.doc = {
    name: 'new-document', size: 0,
    pages: [{ id: uid('p'), kind: 'blank', w: land ? sz.h : sz.w, h: land ? sz.w : sz.h, view: [0, 0, land ? sz.h : sz.w, land ? sz.w : sz.h], view0: [0, 0, land ? sz.h : sz.w, land ? sz.w : sz.h], rot: 0, rot0: 0, objects: [] }],
    sources: new Map(), assets: new Map(),
  };
  state.currentPage = 0;
  history.reset();
  emit('doc:open');
  return true;
}
