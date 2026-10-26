// Existing AcroForm fields: reading widgets with pdf.js, an interactive overlay for filling them in,
// and helpers used by the exporter. Values typed by the user live in page.fv = { fieldName: value } (part of undo history).
import { state, on, emit } from './state.js';
import * as history from './history.js';
import { getPageProxy } from './pdf-renderer.js';
import { userToView } from './page-geom.js';
import { h, debounce } from './utils.js';

const rawCache = new Map(); // "src:index" -> Promise<annotation[]>

function loadRaw(model) {
  const key = `${model.src}:${model.srcIndex}`;
  if (!rawCache.has(key)) {
    rawCache.set(key, getPageProxy(model).then(pg => pg.getAnnotations({ intent: 'display' }))
      .then(list => list.filter(a => a.subtype === 'Widget' && a.rect).map(a => ({
        name: a.fieldName || '', ft: a.fieldType, rect: a.rect, readOnly: !!a.readOnly || !!(a.fieldFlags & 1), multiline: !!a.multiLine, password: !!a.password,
        checkBox: !!a.checkBox, radio: !!a.radioButton, push: !!a.pushButton, combo: !!a.combo, maxLen: a.maxLen || 0,
        value: a.fieldValue, buttonValue: a.buttonValue ?? a.exportValue ?? 'Yes', options: (a.options || []).map(o => ({ v: o.exportValue, d: o.displayValue })), hidden: !!a.hidden || a.annotationFlags & 2,
        fontSize: a.defaultAppearanceData?.fontSize || 0,
      })))
      .catch(() => []));
  }
  return rawCache.get(key);
}
export const clearFormCache = () => rawCache.clear();

/** Fillable widgets of a page, with rects converted to the page's current editor view space (points, y-down). */
export async function getWidgets(model) {
  if (model.kind !== 'pdf') return [];
  const raw = await loadRaw(model);
  return raw.filter(r => !r.hidden && r.name && !r.push && r.ft !== 'Sig').map((r, i) => {
    const a = userToView(model.view, model.rot, r.rect[0], r.rect[1]), b = userToView(model.view, model.rot, r.rect[2], r.rect[3]);
    const x = Math.min(a[0], b[0]), y = Math.min(a[1], b[1]);
    const type = r.ft === 'Tx' ? 'text' : r.checkBox ? 'checkbox' : r.radio ? 'radio' : r.ft === 'Ch' ? (r.combo ? 'dropdown' : 'list') : 'other';
    return { ...r, i, type, x, y, w: Math.abs(a[0] - b[0]), h: Math.abs(a[1] - b[1]) };
  });
}

/** every raw widget (including push buttons / signatures) — used to decide whether a page carries form fields. */
export async function pageHasWidgets(model) { return model.kind === 'pdf' && (await loadRaw(model)).length > 0; }

export function initialValue(w) {
  if (w.type === 'checkbox') return !!w.value && w.value !== 'Off';
  if (w.type === 'radio') return w.value === w.buttonValue;
  if (Array.isArray(w.value)) return w.value[0] ?? '';
  return w.value == null ? '' : String(w.value);
}

/* ---------------- interactive layer ---------------- */
const layers = new Map(); // pageId -> {el, token}
const commitSoon = debounce(() => history.commit('Fill form'), 500);

export function init() {
  on('doc:open', () => { layers.clear(); clearFormCache(); });
}

function curModel(pageId) { return state.doc?.pages.find(p => p.id === pageId); }

function setValue(pageId, name, value, immediate = false) {
  const m = curModel(pageId); if (!m) return;
  (m.fv ||= {})[name] = value;
  immediate ? history.commit('Fill form') : commitSoon();
}

/** (Re)build the fill-in layer of one page. Called by the viewer once a page element exists. */
export async function buildFormLayer(pageId, pageEl) {
  const m = curModel(pageId); if (!m || m.kind !== 'pdf') return;
  const token = Symbol();
  let L = layers.get(pageId);
  if (!L || !pageEl.contains(L.el)) {
    L = { el: h('div', { class: 'formlayer' }), token };
    pageEl.append(L.el); layers.set(pageId, L);
  }
  L.token = token;
  const widgets = await getWidgets(m);
  if (L.token !== token) return;
  L.el.textContent = '';
  const fv = m.fv || {};
  for (const w of widgets) {
    const stored = fv[w.name];
    const el = makeInput(w, stored, pageId);
    if (!el) continue;
    Object.assign(el.style, { left: `${w.x}px`, top: `${w.y}px`, width: `${w.w}px`, height: `${w.h}px` });
    L.el.append(el);
  }
}

function makeInput(w, stored, pageId) {
  const lbl = `Form field ${w.name}`;
  const common = { 'aria-label': lbl, title: w.name, class: `ff ff-${w.type}` };
  if (w.type === 'text') {
    const v = stored !== undefined ? stored : initialValue(w);
    const el = w.multiline ? h('textarea', { ...common, spellcheck: 'false' }) : h('input', { ...common, type: w.password ? 'password' : 'text', spellcheck: 'false' });
    el.value = v; el.readOnly = w.readOnly; if (w.maxLen) el.maxLength = w.maxLen;
    el.style.fontSize = `${Math.max(6, Math.min(w.fontSize || w.h * 0.62, w.multiline ? 14 : w.h * 0.8))}px`;
    el.addEventListener('input', () => setValue(pageId, w.name, el.value));
    return el;
  }
  if (w.type === 'checkbox') {
    const el = h('input', { ...common, type: 'checkbox' });
    el.checked = stored !== undefined ? !!stored : initialValue(w); el.disabled = w.readOnly;
    el.addEventListener('change', () => setValue(pageId, w.name, el.checked, true));
    return el;
  }
  if (w.type === 'radio') {
    const el = h('input', { ...common, type: 'radio', name: `r-${pageId}-${w.name}` });
    el.checked = stored !== undefined ? stored === w.buttonValue : initialValue(w); el.disabled = w.readOnly;
    el.addEventListener('change', () => { if (el.checked) setValue(pageId, w.name, w.buttonValue, true); });
    return el;
  }
  if (w.type === 'dropdown' || w.type === 'list') {
    const el = h('select', { ...common, ...(w.type === 'list' ? { size: Math.max(2, Math.min(6, w.options.length)) } : {}) },
      w.options.map(o => h('option', { value: o.v }, o.d || o.v)));
    const v = stored !== undefined ? stored : initialValue(w);
    el.value = v; if (el.selectedIndex < 0 && w.type === 'dropdown') el.selectedIndex = -1;
    el.disabled = w.readOnly;
    el.addEventListener('change', () => setValue(pageId, w.name, el.value, true));
    return el;
  }
  return null;
}

/** Rebuild all existing layers (after undo/redo or page changes). */
export function refreshAllLayers() {
  for (const [id, L] of layers) {
    const pageEl = L.el.parentElement; if (!pageEl) { layers.delete(id); continue; }
    buildFormLayer(id, pageEl);
  }
}
void emit;
