// Right-sidebar "Properties" card: edits the selected object, or the defaults of the active tool.
import { state, on, findObject } from './state.js';
import * as history from './history.js';
import { h, debounce, $, icon } from './utils.js';
import { refreshObject, deleteObject, duplicateObject, FONT_LABEL } from './objects.js';
import { isSafeUrl } from './pdf-export.js';
import { beginEdit } from './tools-text.js';
const FIELD_TOOLS = ['f-text', 'f-checkbox', 'f-radio', 'f-dropdown', 'f-date'];

let root;
const commitSoon = debounce(() => history.commit('Change style'), 350);

export function init() {
  root = $('#props');
  on('selection', render); on('tool', render); on('doc:open', render); on('doc:restore', render);
  render();
}

function context() {
  const f = findObject(state.selection);
  if (f) return { kind: f.obj.type, obj: f.obj };
  if (state.tool === 'text') return { kind: 'text', obj: null };
  if (state.tool === 'draw') return { kind: 'ink', obj: null };
  if (state.tool === 'highlight') return { kind: 'highlight', obj: null };
  if (['rect', 'ellipse'].includes(state.tool)) return { kind: 'shape', obj: null };
  if (['line', 'arrow', 'underline', 'strike'].includes(state.tool)) return { kind: 'line', obj: null };
  if (state.tool === 'whiteout' || state.tool === 'blackout') return { kind: 'cover', obj: null };
  if (state.tool === 'redact') return { kind: 'redact', obj: null };
  if (state.tool === 'note') return { kind: 'note', obj: null };
  if (state.tool === 'link') return { kind: 'link', obj: null };
  if (state.tool === 'edittext') return { kind: 'edittext', obj: null };
  if (FIELD_TOOLS.includes(state.tool)) return { kind: 'fieldtool', obj: null };
  return { kind: null };
}

const propKey = kind => (kind === 'ink' ? 'draw' : kind === 'signature' ? 'image' : kind);

function set(kind, key, value, live = false) {
  const ctx = context();
  state.props[propKey(kind)][key] = value;
  if (ctx.obj && ctx.obj.type === kind) {
    ctx.obj[key] = value;
    refreshObject(ctx.obj);
    live ? commitSoon() : history.commit('Change style');
  }
}
const val = (kind, key, obj) => (obj && key in obj ? obj[key] : state.props[propKey(kind)][key]);

function row(label, ...kids) { return h('div', { class: 'prow' }, h('label', {}, label), ...kids); }
function colorInput(kind, key, obj) {
  const i = h('input', { type: 'color', value: val(kind, key, obj) || '#000000', 'aria-label': `${key} colour` });
  i.addEventListener('input', () => set(kind, key, i.value, true));
  return i;
}
function range(kind, key, obj, min, max, step, fmt = v => v) {
  const v = h('span', { class: 'val' }, fmt(val(kind, key, obj)));
  const i = h('input', { type: 'range', min, max, step, value: val(kind, key, obj), 'aria-label': key });
  i.addEventListener('input', () => { v.textContent = fmt(+i.value); set(kind, key, +i.value, true); });
  return [i, v];
}
function toggle(kind, key, obj, label, text, style) {
  const b = h('button', { type: 'button', 'aria-pressed': String(!!val(kind, key, obj)), 'aria-label': label, title: label, class: val(kind, key, obj) ? 'on' : '', style }, text);
  b.addEventListener('mousedown', e => e.preventDefault());
  b.addEventListener('click', () => { const nv = !val(kind, key, obj); set(kind, key, nv); render(); });
  return b;
}

/* ---- generic bound controls for the newer object types (object when selected, tool defaults otherwise) ---- */
const DEF = { shape: () => state.props.shape, line: () => state.props.shape, cover: () => state.props.cover, note: () => state.props.note };
const DKEY = { line: { color: 'stroke' }, cover: { color: null } };
function getv(kind, obj, key, dkey) { if (obj) return obj[key]; const d = DEF[kind]?.(); return d ? d[dkey ?? DKEY[kind]?.[key] ?? key] : undefined; }
function setv(kind, obj, key, v, live, dkey) {
  const d = DEF[kind]?.();
  if (d) { const k = dkey ?? DKEY[kind]?.[key] ?? key; if (k) d[k] = v; if (kind === 'cover' && !obj) d[state.tool === 'blackout' ? 'blackout' : 'whiteout'] = v; }
  if (obj) { obj[key] = v; refreshObject(obj); live ? commitSoon() : history.commit('Change'); }
}
const bcolor = (kind, obj, key, label = 'Colour') => { const i = h('input', { type: 'color', value: (kind === 'cover' && !obj ? (state.tool === 'blackout' ? state.props.cover.blackout : state.props.cover.whiteout) : getv(kind, obj, key)) || '#000000', 'aria-label': label }); i.addEventListener('input', () => setv(kind, obj, key, i.value, true)); return row(label, i); };
const brange = (kind, obj, key, label, min, max, step, fmt = v => v) => { const val_ = getv(kind, obj, key) ?? max; const v = h('span', { class: 'val' }, fmt(val_)); const i = h('input', { type: 'range', min, max, step, value: val_, 'aria-label': label }); i.addEventListener('input', () => { v.textContent = fmt(+i.value); setv(kind, obj, key, +i.value, true); }); return row(label, i, v); };
const btext = (obj, key, label, opts = {}) => { const i = opts.area ? h('textarea', { rows: 3, 'aria-label': label, style: { width: '100%' } }) : h('input', { type: 'text', 'aria-label': label, style: { flex: 1, minWidth: 0 } }); i.value = Array.isArray(obj[key]) ? obj[key].join('\n') : (obj[key] ?? ''); i.addEventListener('input', () => { obj[key] = opts.area && key === 'options' ? i.value.split('\n').map(s => s.trim()).filter(Boolean) : i.value; refreshObject(obj); commitSoon(); }); return opts.area ? h('div', { class: 'prow' }, h('label', {}, label), i) : row(label, i); };
const bcheck = (obj, key, label, rerender = false) => { const i = h('input', { type: 'checkbox', checked: !!obj[key], 'aria-label': label }); i.addEventListener('change', () => { obj[key] = i.checked; refreshObject(obj); history.commit('Change'); if (rerender) render(); }); return row(label, i); };

function extra(kind, obj) {
  const warn = t => root.append(h('p', { class: 'note warn' }, t));
  if (kind === 'shape') {
    const hasFill = obj ? !!obj.fill : state.props.shape.hasFill;
    const fc = h('input', { type: 'color', value: (obj ? obj.fill : state.props.shape.fill) || '#fff3b0', disabled: !hasFill, 'aria-label': 'Fill colour' });
    const fk = h('input', { type: 'checkbox', checked: hasFill, 'aria-label': 'Fill shape' });
    fk.addEventListener('change', () => { state.props.shape.hasFill = fk.checked; if (obj) { obj.fill = fk.checked ? fc.value : null; refreshObject(obj); history.commit('Change'); } render(); });
    fc.addEventListener('input', () => { state.props.shape.fill = fc.value; if (obj) { obj.fill = fc.value; refreshObject(obj); commitSoon(); } });
    root.append(bcolor('shape', obj, 'stroke', 'Outline'), row('Fill', fk, fc), brange('shape', obj, 'width', 'Width', 0.5, 16, 0.5), brange('shape', obj, 'opacity', 'Opacity', 0.1, 1, 0.05, v => `${Math.round(v * 100)}%`));
    if (!obj) root.append(h('p', { class: 'note' }, 'Drag on the page to draw. Hold Shift for a square/circle.'));
  } else if (kind === 'line') {
    root.append(bcolor('line', obj, 'color'), brange('line', obj, 'width', 'Width', 0.5, 16, 0.5), brange('line', obj, 'opacity', 'Opacity', 0.1, 1, 0.05, v => `${Math.round(v * 100)}%`));
    if (obj && obj.pts.length === 2) root.append(bcheck(obj, 'arrow', 'Arrow head'));
    if (!obj) root.append(h('p', { class: 'note' }, state.tool === 'underline' || state.tool === 'strike' ? 'Drag a box over the text to underline / strike it through.' : 'Drag from start to end. Hold Shift for 45° steps.'));
  } else if (kind === 'cover') {
    root.append(bcolor('cover', obj, 'color', 'Colour'));
    warn('Visual cover only — NOT secure redaction. The original text and images stay in the PDF file underneath and can be recovered. Use “Redact (secure)” to remove content for real.');
  } else if (kind === 'redact') {
    warn('Secure redaction: pages that contain a redaction box are saved as flat images with the box burned in black, so the covered text and images are removed from those pages. Those pages lose selectable text, links and form fields.');
    if (!obj) root.append(h('p', { class: 'note' }, 'Drag a rectangle over the content to remove.'));
  } else if (kind === 'note') {
    if (obj) root.append(btext(obj, 'text', 'Text', { area: true }));
    root.append(bcolor('note', obj, 'color', 'Colour'));
    root.append(h('p', { class: 'note' }, obj ? 'Saved as a standard PDF note (comment) annotation.' : 'Click on the page to drop a note.'));
  } else if (kind === 'link') {
    if (!obj) { root.append(h('p', { class: 'note' }, 'Drag a rectangle (or click) where the link should be, then set its target here.')); return; }
    const k = h('select', { 'aria-label': 'Link type' }, h('option', { value: 'url' }, 'Web address (URL)'), h('option', { value: 'page' }, 'Page in this document'));
    k.value = obj.kind; k.addEventListener('change', () => { obj.kind = k.value; if (obj.kind === 'page' && !obj.destPage) obj.destPage = state.doc.pages[0].id; refreshObject(obj); history.commit('Change link'); render(); });
    root.append(row('Type', k));
    if (obj.kind === 'url') root.append(btext(obj, 'url', 'URL'));
    else {
      const p = h('select', { 'aria-label': 'Target page' }, state.doc.pages.map((pg, i) => h('option', { value: pg.id }, `Page ${i + 1}`)));
      p.value = obj.destPage || state.doc.pages[0].id; p.addEventListener('change', () => { obj.destPage = p.value; history.commit('Change link'); });
      root.append(row('Go to', p));
    }
    root.append(h('p', { class: 'note' }, 'The link area is invisible in the saved PDF.'));
  } else if (kind === 'edittext') {
    root.append(h('p', { class: 'note' }, 'Click any line of text on the page to edit it. The original is covered with a background-coloured patch and your new text is placed on top. The original characters remain in the file underneath.'));
  } else if (kind === 'fieldtool') {
    root.append(h('p', { class: 'note' }, 'Click or drag on the page to add a fillable form field. Select it afterwards to set its name, value and options.'));
  } else if (kind === 'field' && obj) {
    root.append(btext(obj, 'name', obj.fkind === 'radio' ? 'Group' : 'Name'));
    if (obj.fkind === 'text' || obj.fkind === 'date') root.append(btext(obj, 'value', 'Default'));
    if (obj.fkind === 'dropdown') { root.append(btext(obj, 'options', 'Options', { area: true }), btext(obj, 'value', 'Selected')); }
    if (obj.fkind === 'checkbox' || obj.fkind === 'radio') root.append(bcheck(obj, 'checked', obj.fkind === 'radio' ? 'Selected' : 'Checked'));
    if (obj.fkind === 'radio') root.append(btext(obj, 'exportValue', 'Choice value'));
    if (obj.fkind !== 'checkbox' && obj.fkind !== 'radio') { const sz = h('input', { type: 'number', min: 6, max: 40, value: obj.size, 'aria-label': 'Font size' }); sz.addEventListener('input', () => { obj.size = Math.max(6, Math.min(40, +sz.value || 11)); refreshObject(obj); commitSoon(); }); root.append(row('Font size', sz)); }
    root.append(bcheck(obj, 'required', 'Required'), bcheck(obj, 'readonly', 'Read-only'));
    root.append(h('p', { class: 'note' }, 'Saved as a real fillable AcroForm field.'));
  }
}

function render() {
  root.textContent = '';
  if (!state.doc) return;
  const { kind, obj } = context();
  if (!kind) {
    root.append(h('h3', {}, 'Properties'), h('p', { class: 'empty' }, 'Choose a tool below, or click an item on the page to edit it. Drag the handles to resize or rotate.'));
    return;
  }
  const titles = { text: 'Text', ink: 'Drawing', highlight: 'Highlight', image: 'Image', signature: 'Signature', shape: 'Shape', line: 'Line / arrow', cover: 'Cover', redact: 'Secure redaction', note: 'Note', link: 'Link', field: 'Form field', edittext: 'Edit existing text', fieldtool: 'Form field' };
  root.append(h('h3', {}, `${titles[kind]}${obj ? '' : ' — defaults'}`));
  if (kind === 'text') {
    const fs = h('select', { 'aria-label': 'Font family' }, Object.entries(FONT_LABEL).map(([k, l]) => h('option', { value: k, selected: val(kind, 'font', obj) === k }, l)));
    fs.addEventListener('change', () => set(kind, 'font', fs.value));
    const sz = h('input', { type: 'number', min: 6, max: 200, step: 1, value: val(kind, 'size', obj), 'aria-label': 'Font size' });
    sz.addEventListener('input', () => { const v = Math.min(200, Math.max(6, +sz.value || 12)); set(kind, 'size', v, true); });
    root.append(row('Font', fs), row('Size', sz),
      row('Style', h('span', { class: 'seg' }, toggle(kind, 'bold', obj, 'Bold', 'B', { fontWeight: 700 }), toggle(kind, 'italic', obj, 'Italic', 'I', { fontStyle: 'italic' }), toggle(kind, 'underline', obj, 'Underline', 'U', { textDecoration: 'underline' }))));
    const al = h('span', { class: 'seg' });
    for (const [v, t] of [['left', '⇤'], ['center', '↔'], ['right', '⇥']]) {
      const b = h('button', { type: 'button', class: val(kind, 'align', obj) === v ? 'on' : '', 'aria-label': `Align ${v}`, title: `Align ${v}` }, t);
      b.addEventListener('mousedown', e => e.preventDefault());
      b.addEventListener('click', () => { set(kind, 'align', v); render(); });
      al.append(b);
    }
    const hasBg = !!val(kind, 'bg', obj);
    const bgChk = h('input', { type: 'checkbox', checked: hasBg, 'aria-label': 'Fill background' });
    const bgCol = h('input', { type: 'color', value: val(kind, 'bg', obj) || '#ffffff', 'aria-label': 'Background colour', disabled: !hasBg });
    bgChk.addEventListener('change', () => { set(kind, 'bg', bgChk.checked ? bgCol.value : null); render(); });
    bgCol.addEventListener('input', () => set(kind, 'bg', bgCol.value, true));
    root.append(row('Colour', colorInput(kind, 'color', obj)), row('Align', al), row('Fill', bgChk, bgCol));
    if (obj) root.append(row('Opacity', ...range(kind, 'opacity', obj, 0.05, 1, 0.05, v => `${Math.round(v * 100)}%`)));
    if (obj) root.append(h('p', { class: 'note' }, 'Double-click the text to edit it. Drag the side handles to change the width.'));
  } else if (kind === 'ink') {
    root.append(row('Colour', colorInput(kind, 'color', obj)), row('Width', ...range(kind, 'width', obj, 1, 24, 0.5, v => `${v}`)), row('Opacity', ...range(kind, 'opacity', obj, 0.1, 1, 0.05, v => `${Math.round(v * 100)}%`)));
    if (!obj) root.append(h('p', { class: 'note' }, 'Drag on the page to draw. Press Esc when finished.'));
  } else if (kind === 'highlight') {
    root.append(row('Colour', colorInput(kind, 'color', obj)), row('Opacity', ...range(kind, 'opacity', obj, 0.1, 1, 0.05, v => `${Math.round(v * 100)}%`)));
    if (!obj) root.append(h('p', { class: 'note' }, 'Drag a rectangle over the text you want to highlight. Press Esc when finished.'));
  } else if (kind === 'image' || kind === 'signature') {
    root.append(row('Opacity', ...range(kind, 'opacity', obj, 0.1, 1, 0.05, v => `${Math.round(v * 100)}%`)));
    if (kind === 'signature') root.append(h('p', { class: 'note' }, 'Visual e-signature only — not a certificate-based digital signature.'));
  }
  extra(kind, obj);
  if (obj && obj.type === 'field') { /* handled above */ }
  if (obj) {
    const acts = h('div', { class: 'pactions' });
    if (obj.type === 'text') acts.append(h('button', { class: 'btn', type: 'button', onclick: () => beginEdit(obj.id, { selectAll: false }) }, 'Edit text'));
    acts.append(
      h('button', { class: 'btn', type: 'button', onclick: () => duplicateObject(obj.id) }, icon('copy'), 'Duplicate'),
      h('button', { class: 'btn btn-danger', type: 'button', onclick: () => deleteObject(obj.id) }, icon('trash'), 'Delete'));
    root.append(acts);
  }
}
