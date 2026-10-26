// Text tool: create text boxes and edit them in place (contenteditable).
import { state, findObject, setTool, emit } from './state.js';
import * as history from './history.js';
import { insertObject, newTextObject, elOf, measureText, refreshSelection, select } from './objects.js';

const PLAIN = (() => { try { const d = document.createElement('div'); d.contentEditable = 'plaintext-only'; return d.contentEditable === 'plaintext-only'; } catch { return false; } })();

export const isEditing = () => !!document.querySelector('.obj-text.editing');
export function stopEditing() { document.querySelector('.obj-text.editing')?.blur(); }

function readText(el) {
  let t = el.innerText.replace(/ /g, ' ');
  if (t.endsWith('\n')) t = t.slice(0, -1);
  return t;
}

export function createTextAt(pageId, x, y) {
  const obj = newTextObject(x, y);
  const page = state.doc.pages.find(p => p.id === pageId);
  obj.x = Math.max(0, Math.min(x, page.w - 40));
  obj.w = Math.min(obj.w, page.w - obj.x);
  insertObject(pageId, obj);
  select(obj.id);
  setTool('select');
  setTimeout(() => beginEdit(obj.id, { isNew: true }), 30);
  return obj;
}

export function beginEdit(id, { isNew = false, selectAll = false } = {}) {
  const f = findObject(id); const el = elOf(id);
  if (!f || !el || el.classList.contains('editing')) return;
  select(id);
  el.contentEditable = PLAIN ? 'plaintext-only' : 'true';
  el.classList.add('editing');
  const orig = f.obj.text;
  el.focus();
  const sel = getSelection(); const range = document.createRange();
  range.selectNodeContents(el); if (!selectAll) range.collapse(false);
  sel.removeAllRanges(); sel.addRange(range);

  const onInput = () => { const cur = findObject(id); if (!cur) return; cur.obj.text = readText(el); measureText(cur.obj, el); refreshSelection(); };
  const onPaste = e => { e.preventDefault(); const t = (e.clipboardData || window.clipboardData).getData('text/plain'); document.execCommand('insertText', false, t); };
  const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); el.blur(); } };
  const onBlur = () => {
    el.removeEventListener('input', onInput); el.removeEventListener('paste', onPaste); el.removeEventListener('keydown', onKey);
    el.contentEditable = 'false'; el.classList.remove('editing');
    getSelection().removeAllRanges();
    const cur = findObject(id);
    if (!cur) return;                       // object vanished (e.g. undo)
    const text = readText(el);
    cur.obj.text = text;
    if (!text.trim()) {                     // empty text box: remove it
      cur.page.objects.splice(cur.page.objects.indexOf(cur.obj), 1);
      el.remove(); state.selection = null; refreshSelection();
      emit('selection', null);
      if (!isNew) history.commit('Delete text');
      return;
    }
    measureText(cur.obj, el);
    refreshSelection();
    if (isNew || text !== orig) history.commit(isNew ? 'Add text' : 'Edit text');
  };
  el.addEventListener('input', onInput); el.addEventListener('paste', onPaste); el.addEventListener('keydown', onKey);
  el.addEventListener('blur', onBlur, { once: true });
}
