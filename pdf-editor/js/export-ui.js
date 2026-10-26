// Download dialog (shows pages / size / status before saving) and printing.
import { state, on } from './state.js';
import { h, showModal, formatBytes, toast, confirmDialog, friendlyError, safeFileName, downloadFile } from './utils.js';
import { exportPdf, exportNotes } from './pdf-export.js';

let cache = null; // { bytes, raster, flatten }
on('changed', () => { cache = null; });
on('doc:open', () => { cache = null; });

const FLATTEN_MSG = 'This PDF cannot be modified structurally in the browser (it is encrypted or uses a feature the editor does not support). I can still save a flattened copy: every page becomes an image with your edits on top. Text in the saved copy will not be selectable. Continue?';

/** Core export with the "flattened copy" fallback prompt. opts: {pages, flattenForms, onProgress, setStatus}. Returns {bytes, raster, notes} or null. */
export async function runExport({ pages = null, flattenForms = false, onProgress = () => {}, setStatus = () => {}, extra = {} } = {}) {
  let allowRaster = false;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      setStatus('Processing…');
      const bytes = await exportPdf({ onProgress, allowRaster, pages, flattenForms, ...extra });
      return { bytes, raster: allowRaster, notes: [...exportNotes] };
    } catch (e) {
      if (e && (e.code === 'ENCRYPTED' || e.code === 'UNSUPPORTED') && !allowRaster) {
        if (await confirmDialog('Save a flattened copy?', FLATTEN_MSG, 'Save flattened copy')) { allowRaster = true; continue; }
        setStatus('Cancelled'); return null;
      }
      console.warn('export failed:', e && (e.code || e.name));
      setStatus('Failed');
      toast(e && e.code === 'VERIFY' ? 'The exported PDF failed a self-check, so it was not saved. Please try again.' : friendlyError(e, 'The PDF could not be exported. Your original file has not been changed.'), 'error', 7000);
      return null;
    }
  }
  return null;
}

/** Build (or reuse) the exported bytes for the whole document. */
export async function buildPdf(onProgress = () => {}, setStatus = () => {}, { flattenForms = false } = {}) {
  if (cache && cache.flatten === flattenForms) return cache;
  const r = await runExport({ flattenForms, onProgress, setStatus });
  if (!r) return null;
  cache = { ...r, flatten: flattenForms };
  return cache;
}

const safeName = safeFileName;

export const downloadBytes = (bytes, name) => downloadFile(bytes, `${safeName(name)}.pdf`, 'application/pdf');
export { safeName };

export async function openDownloadDialog() {
  if (!state.doc) return;
  const nameIn = h('input', { type: 'text', value: `${safeName(state.doc.name)}${state.doc.sources.size ? '-edited' : ''}`, 'aria-label': 'File name', maxlength: '120' });
  const ddPages = h('dd', {}, String(state.doc.pages.length));
  const ddSize = h('dd', {}, '…');
  const ddStatus = h('dd', {}, 'Preparing…');
  const bar = h('i'); const prog = h('div', { class: 'progress', role: 'progressbar' }, bar);
  const note = h('div', { class: 'callout info', hidden: true });
  const flat = h('input', { type: 'checkbox', id: 'dl-flatten' });
  const flatRow = h('label', { class: 'checkline', for: 'dl-flatten' }, flat, ' Flatten form fields (make filled values permanent and non-editable)');
  let result = null, seq = 0;
  const m = showModal({
    title: 'Download PDF',
    content: [h('div', { class: 'field' }, h('label', {}, 'File name'), h('div', { style: { display: 'flex', gap: '6px', alignItems: 'center' } }, nameIn, h('span', {}, '.pdf'))),
      h('dl', { class: 'stats' }, h('dt', {}, 'Pages'), ddPages, h('dt', {}, 'File size'), ddSize, h('dt', {}, 'Status'), ddStatus), prog, flatRow, note],
    actions: [{ label: 'Cancel', value: false }, { label: 'Download', value: true, primary: true, keepOpen: true, onClick: async close => { if (!result) return false; downloadBytes(result.bytes, nameIn.value); state.dirty = false; toast('Download started.', 'ok'); close(true); } }],
  });
  const dlBtn = m.buttons[1]; dlBtn.disabled = true;
  nameIn.addEventListener('keydown', e => { if (e.key === 'Enter' && !dlBtn.disabled) dlBtn.click(); });
  const prepare = async () => {
    const my = ++seq; dlBtn.disabled = true; result = null; bar.style.width = '0%'; ddSize.textContent = '…';
    const r = await buildPdf(p => { bar.style.width = `${Math.round(p * 100)}%`; }, s => { ddStatus.textContent = s; }, { flattenForms: flat.checked });
    if (my !== seq) return;
    if (!r) { m.close(false); return; }
    result = r;
    bar.style.width = '100%';
    ddSize.textContent = formatBytes(r.bytes.length);
    ddStatus.textContent = 'Ready to download';
    const msgs = [...(r.raster ? ['Saved as a flattened copy (pages are images). Text is not selectable.'] : []), ...(r.notes || [])];
    note.hidden = !msgs.length; note.textContent = msgs.join(' ');
    dlBtn.disabled = false; dlBtn.focus();
  };
  flat.addEventListener('change', prepare);
  await prepare();
}

export async function printPdf() {
  if (!state.doc) return;
  const t = toast('Preparing to print…', 'info', 60000);
  const r = await buildPdf();
  t.remove();
  if (!r) return;
  const url = URL.createObjectURL(new Blob([r.bytes], { type: 'application/pdf' }));
  const frame = h('iframe', { style: { position: 'fixed', right: '0', bottom: '0', width: '0', height: '0', border: '0', visibility: 'hidden' }, 'aria-hidden': 'true', title: 'print' });
  let done = false;
  const cleanup = () => { if (done) return; done = true; frame.remove(); URL.revokeObjectURL(url); };
  frame.addEventListener('load', () => {
    setTimeout(() => {
      try { frame.contentWindow.focus(); frame.contentWindow.print(); }
      catch { window.open(url, '_blank'); toast('Your browser blocked direct printing. The PDF opened in a new tab — print it from there.', 'info', 7000); return; }
    }, 400);
  });
  frame.src = url;
  document.body.append(frame);
  setTimeout(cleanup, 120000);
}
