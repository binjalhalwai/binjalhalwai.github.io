// Bootstrap: wires modules together, keyboard shortcuts, drag & drop and global error handling.
import { state, on, emit, setTool, setPending, findObject } from './state.js';
import * as history from './history.js';
import * as viewer from './viewer.js';
import * as thumbs from './thumbnails.js';
import * as interaction from './interaction.js';
import * as props from './properties.js';
import * as sidebar from './sidebar-tools.js';
import * as toolbar from './toolbar.js';
import { select, deleteObject, duplicateObject, refreshObject } from './objects.js';
import { isEditing } from './tools-text.js';
import { openFiles } from './pdf-import.js';
import { openDownloadDialog, printPdf } from './export-ui.js';
import { handleImageFile, dropImageOnPage } from './tools-image.js';
import * as forms from './forms.js';
import * as findmod from './find.js';
import { openFind, closeFind, isFindOpen } from './find.js';
import { openHelp } from './help.js';
import { $, toast, friendlyError, debounce } from './utils.js';

// Must be registered first: the app has to be visible (laid out) before the viewer measures it to fit the page.
on('doc:open', () => {
  document.body.classList.remove('no-doc');
  $('#landing').hidden = true;
  $('#app').hidden = false;
  document.body.classList.remove('show-left', 'show-right');
  setTool('select');
});

// Friendly warning for very old browsers (the editor needs a 2022+ browser).
if (typeof structuredClone !== 'function' || typeof createImageBitmap !== 'function' || !window.IntersectionObserver) {
  toast('Your browser is too old for this editor. Please update Chrome, Edge or Firefox.', 'error', 20000);
}
viewer.init(); thumbs.init(); interaction.init(); props.init(); sidebar.init(); toolbar.init(); forms.init(); findmod.init();


$('#file-input').addEventListener('change', e => { if (e.target.files.length) openFiles(e.target.files); });
$('#image-input').addEventListener('change', e => { const f = e.target.files[0]; if (f) handleImageFile(f); });

/* ---------- drag & drop ---------- */
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
const dz = $('#dropzone');
window.addEventListener('dragover', e => { if (!hasFiles(e)) return; e.preventDefault(); if (!state.doc) dz.classList.add('drag'); });
window.addEventListener('dragleave', e => { if (e.relatedTarget === null) dz.classList.remove('drag'); });
window.addEventListener('drop', async e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); dz.classList.remove('drag');
  const files = [...e.dataTransfer.files];
  if (state.doc) {
    const img = files.find(f => f.type.startsWith('image/'));
    if (img) {
      let overlay = e.target.closest?.('.overlay'), x, y, pageId;
      if (overlay) { const r = overlay.getBoundingClientRect(); x = (e.clientX - r.left) / state.zoom; y = (e.clientY - r.top) / state.zoom; pageId = overlay.dataset.pageId; }
      else { const pg = state.doc.pages[state.currentPage]; pageId = pg.id; x = pg.w / 2; y = pg.h / 2; }
      dropImageOnPage(img, pageId, x, y); return;
    }
  }
  openFiles(files);
});

/* ---------- keyboard ---------- */
const nudgeCommit = debounce(() => history.commit('Nudge'), 450);
document.addEventListener('keydown', e => {
  if (!state.doc || document.querySelector('.modal-back')) return;
  const t = e.target, typing = t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName);
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if (mod && k === 's') { e.preventDefault(); if (isEditing()) document.activeElement.blur(); openDownloadDialog(); return; }
  if (mod && k === 'o') { e.preventDefault(); toolbar.openFilePicker(); return; }
  if (mod && k === 'f') { e.preventDefault(); openFind('find'); return; }
  if (mod && k === 'p') { e.preventDefault(); printPdf(); return; }
  if (mod && (k === '=' || k === '+')) { e.preventDefault(); viewer.zoomStep(1); return; }
  if (mod && k === '-') { e.preventDefault(); viewer.zoomStep(-1); return; }
  if (mod && k === '0') { e.preventDefault(); viewer.fit('width'); return; }
  if (typing) { if (e.key === 'Escape' && isFindOpen()) closeFind(); return; }                                              // let inputs / text editing keep native undo, delete, etc.
  if (mod && k === 'z' && !e.shiftKey) { e.preventDefault(); history.undo(); return; }
  if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); history.redo(); return; }
  if (mod && k === 'd' && state.selection) { e.preventDefault(); duplicateObject(state.selection); return; }
  if (mod || e.altKey) return;
  if (e.key === '?' ) { e.preventDefault(); openHelp(); return; }
  if (e.key === 'Escape') {
    if (isFindOpen()) closeFind();
    else if (state.pending) setPending(null);
    else if (state.tool !== 'select') setTool('select');
    else select(null);
    return;
  }
  if ((e.key === 'Delete' || e.key === 'Backspace') && state.selection) { e.preventDefault(); deleteObject(state.selection); return; }
  if (e.key === 'Enter' && state.selection) { const f = findObject(state.selection); if (f?.obj.type === 'text') { e.preventDefault(); import('./tools-text.js').then(m => m.beginEdit(state.selection)); return; } }
  if (state.selection && e.key.startsWith('Arrow')) {
    e.preventDefault();
    const f = findObject(state.selection); if (!f) return;
    const d = e.shiftKey ? 10 : 1;
    if (e.key === 'ArrowLeft') f.obj.x -= d; if (e.key === 'ArrowRight') f.obj.x += d;
    if (e.key === 'ArrowUp') f.obj.y -= d; if (e.key === 'ArrowDown') f.obj.y += d;
    refreshObject(f.obj); nudgeCommit(); return;
  }
  const map = { v: 'select', t: 'text', d: 'draw', h: 'highlight', i: 'image', s: 'signature' };
  if (map[k]) { e.preventDefault(); sidebar.activate(map[k]); }
});

/* ---------- safety nets ---------- */
window.addEventListener('beforeunload', e => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });
let lastErr = 0;
const report = err => { const now = Date.now(); if (now - lastErr < 2000) return; lastErr = now; console.warn('unexpected error:', err && err.name); toast(friendlyError(err, 'Something unexpected happened. Your work is still here — try the action again.'), 'error', 6000); };
window.addEventListener('error', e => { if (/ResizeObserver/.test(e.message || '')) return; report(e.error); });
window.addEventListener('unhandledrejection', e => report(e.reason));
