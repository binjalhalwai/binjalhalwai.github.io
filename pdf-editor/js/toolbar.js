// Top bar wiring: open/new, undo/redo, zoom, fit, download, print, fullscreen, panels, page navigator.
import { state, on } from './state.js';
import * as history from './history.js';
import { $, PT_PX, formatBytes, toast, clamp } from './utils.js';
import { zoomStep, fit, goToPage } from './viewer.js';
import { openDownloadDialog, printPdf } from './export-ui.js';
import { createBlankDocument } from './pdf-import.js';
import { stopEditing } from './tools-text.js';

export function openFilePicker() { const i = $('#file-input'); i.value = ''; i.click(); }

export function init() {
  $('#btn-open').addEventListener('click', openFilePicker);
  $('#landing-open').addEventListener('click', openFilePicker);
  $('#btn-new').addEventListener('click', createBlankDocument);
  $('#landing-new').addEventListener('click', createBlankDocument);
  $('#btn-undo').addEventListener('click', () => { stopEditing(); history.undo(); });
  $('#btn-redo').addEventListener('click', () => { stopEditing(); history.redo(); });
  $('#btn-zoom-in').addEventListener('click', () => zoomStep(1));
  $('#btn-zoom-out').addEventListener('click', () => zoomStep(-1));
  $('#btn-fit-width').addEventListener('click', () => fit('width'));
  $('#btn-fit-page').addEventListener('click', () => fit('page'));
  $('#btn-download').addEventListener('click', () => { stopEditing(); openDownloadDialog(); });
  $('#btn-print').addEventListener('click', () => { stopEditing(); printPdf(); });
  $('#btn-fullscreen').addEventListener('click', async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch { toast('Full screen is not available in this browser.', 'error'); }
  });

  const narrow = () => window.matchMedia('(max-width: 900px)').matches;
  const togglePanel = side => {
    const cls = narrow() ? `show-${side}` : `hide-${side}`;
    const other = side === 'left' ? 'right' : 'left';
    if (narrow()) document.body.classList.remove(`show-${other}`);
    document.body.classList.toggle(cls);
  };
  $('#btn-toggle-left').addEventListener('click', () => togglePanel('left'));
  $('#btn-toggle-right').addEventListener('click', () => togglePanel('right'));
  $('#backdrop').addEventListener('click', () => document.body.classList.remove('show-left', 'show-right'));

  const upd = () => {
    $('#btn-undo').disabled = !history.canUndo();
    $('#btn-redo').disabled = !history.canRedo();
  };
  on('history', upd); on('doc:open', upd);
  on('zoom', z => { $('#zoom-label').textContent = `${Math.round(z / PT_PX * 100)}%`; });
  on('doc:open', () => {
    const d = state.doc;
    $('#fi-name').textContent = `${d.name}.pdf`;
    $('#fi-meta').textContent = `${d.pages.length} page${d.pages.length === 1 ? '' : 's'}${d.size ? ` · ${formatBytes(d.size)}` : ''}`;
    $('#file-info').title = `${d.name}.pdf`;
    $('#pg-total').textContent = d.pages.length;
    $('#pg-input').max = d.pages.length;
    document.title = `${d.name}.pdf — PDF Editor`;
  });

  // page navigator
  const inp = $('#pg-input');
  on('page', i => { inp.value = i + 1; });
  $('#pg-prev').addEventListener('click', () => goToPage(state.currentPage - 1, true));
  $('#pg-next').addEventListener('click', () => goToPage(state.currentPage + 1, true));
  inp.addEventListener('change', () => goToPage(clamp((parseInt(inp.value, 10) || 1) - 1, 0, state.doc.pages.length - 1), true));
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') inp.blur(); });
}
