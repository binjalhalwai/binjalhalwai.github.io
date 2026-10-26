// Object layer: DOM rendering of text / image / signature / ink / highlight objects,
// the selection box (move/resize/rotate handles) and object CRUD helpers.
// All coordinates are PDF points, origin top-left of the (rotated) page view; rotation in degrees, clockwise.
import { state, emit, findObject } from './state.js';
import * as history from './history.js';
import { uid } from './utils.js';

export const FONT_CSS = {
  helvetica: 'Helvetica, Arial, "Liberation Sans", "Noto Sans", sans-serif',
  times: '"Times New Roman", Times, "Liberation Serif", "Noto Serif", serif',
  courier: '"Courier New", Courier, "Liberation Mono", monospace',
};
export const FONT_LABEL = { helvetica: 'Helvetica (Sans)', times: 'Times (Serif)', courier: 'Courier (Mono)' };
export const LINE_H = 1.2;
export const CAPS = {
  text: { resize: 'we', rotate: true },
  image: { resize: 'all', aspect: true, rotate: true },
  signature: { resize: 'all', aspect: true, rotate: true },
  ink: { resize: 'all', aspect: false, rotate: false },
  highlight: { resize: 'all', aspect: false, rotate: false },
  shape: { resize: 'all', aspect: false, rotate: false },
  line: { resize: 'all', aspect: false, rotate: false },
  cover: { resize: 'all', aspect: false, rotate: false },
  redact: { resize: 'all', aspect: false, rotate: false },
  link: { resize: 'all', aspect: false, rotate: false },
  note: { resize: 'none', aspect: false, rotate: false },
  field: { resize: 'all', aspect: false, rotate: false },
};
const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const SVG = 'http://www.w3.org/2000/svg';

export const overlayOf = pageId => state.overlays.get(pageId) || null;
export const elOf = id => {
  const f = findObject(id); if (!f) return null;
  return overlayOf(f.page.id)?.querySelector(`:scope > .obj[data-id="${id}"]`) || null;
};

/* ---------- construction ---------- */
export function newTextObject(x, y) {
  const p = state.props.text;
  return { id: uid('t'), type: 'text', x, y, w: 200, h: p.size * LINE_H, rot: 0, text: '', font: p.font, size: p.size, bold: p.bold, italic: p.italic, underline: p.underline, color: p.color, align: p.align, bg: p.bg };
}
export function newImageObject(type, assetId, x, y, w, h) {
  return { id: uid('i'), type, x, y, w, h, rot: 0, assetId, opacity: state.props.image.opacity };
}
export function newHighlightObject(x, y, w, h) {
  const p = state.props.highlight;
  return { id: uid('h'), type: 'highlight', x, y, w, h, rot: 0, color: p.color, opacity: p.opacity };
}
export function newInkObject(absPts) {
  const p = state.props.draw;
  const xs = absPts.map(q => q[0]), ys = absPts.map(q => q[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  const w = Math.max(1, Math.max(...xs) - x), hh = Math.max(1, Math.max(...ys) - y);
  return { id: uid('d'), type: 'ink', x, y, w, h: hh, rot: 0, pts: absPts.map(q => [q[0] - x, q[1] - y]), color: p.color, width: p.width, opacity: p.opacity };
}

export function newShapeObject(kind, x, y, w, h) {
  const p = state.props.shape;
  return { id: uid('s'), type: 'shape', kind, x, y, w, h, rot: 0, stroke: p.stroke, fill: p.hasFill ? p.fill : null, width: p.width, opacity: p.opacity };
}
/** straight line / arrow between two absolute points. */
export function newLineObject(a, b, arrow = false, over = {}) {
  const p = state.props.shape;
  const x = Math.min(a[0], b[0]), y = Math.min(a[1], b[1]);
  return { id: uid('l'), type: 'line', x, y, w: Math.max(1, Math.abs(a[0] - b[0])), h: Math.max(1, Math.abs(a[1] - b[1])), rot: 0, arrow, pts: [[a[0] - x, a[1] - y], [b[0] - x, b[1] - y]], color: p.stroke, width: p.width, opacity: p.opacity, ...over };
}
export function newCoverObject(subtype, x, y, w, h) {
  return { id: uid('c'), type: 'cover', subtype, x, y, w, h, rot: 0, color: subtype === 'blackout' ? state.props.cover.blackout : state.props.cover.whiteout };
}
export function newRedactObject(x, y, w, h) { return { id: uid('r'), type: 'redact', x, y, w, h, rot: 0 }; }
export function newLinkObject(x, y, w, h) { return { id: uid('k'), type: 'link', x, y, w, h, rot: 0, kind: 'url', url: 'https://', destPage: null }; }
export function newNoteObject(x, y) { return { id: uid('n'), type: 'note', x, y, w: 22, h: 22, rot: 0, text: '', color: state.props.note.color }; }
let fieldCounter = 0;
export function newFieldObject(fkind, x, y, w, h) {
  fieldCounter++;
  const base = { id: uid('f'), type: 'field', fkind, x, y, w, h, rot: 0, name: `${fkind}_${fieldCounter}`, value: '', options: ['Option 1', 'Option 2', 'Option 3'], size: 11, required: false, readonly: false, checked: false, group: '', exportValue: 'Yes' };
  if (fkind === 'radio') { base.name = `group_${Math.ceil(fieldCounter / 1)}`; base.exportValue = `Choice${fieldCounter}`; }
  return base;
}

/* ---------- DOM ---------- */
export function createObjectEl(obj) {
  const el = document.createElement('div');
  el.className = `obj obj-${obj.type}`;
  el.dataset.id = obj.id;
  if (obj.type === 'image' || obj.type === 'signature') {
    const img = new Image();
    img.alt = obj.type === 'signature' ? 'Signature' : 'Image';
    img.draggable = false;
    const a = state.doc.assets.get(obj.assetId);
    if (a) img.src = a.url;
    el.append(img);
  } else if (obj.type === 'ink') {
    const svg = document.createElementNS(SVG, 'svg');
    svg.append(document.createElementNS(SVG, 'path'));
    el.append(svg);
  } else if (obj.type === 'line') {
    const svg = document.createElementNS(SVG, 'svg');
    svg.append(document.createElementNS(SVG, 'path'), document.createElementNS(SVG, 'path'));
    el.append(svg);
  } else if (obj.type === 'shape') {
    const svg = document.createElementNS(SVG, 'svg');
    svg.append(document.createElementNS(SVG, obj.kind === 'ellipse' ? 'ellipse' : 'rect'));
    el.append(svg);
  } else if (obj.type === 'redact' || obj.type === 'link' || obj.type === 'field' || obj.type === 'note') {
    const lab = document.createElement('span'); lab.className = 'olabel'; el.append(lab);
  }
  updateObjectEl(el, obj);
  return el;
}

export function arrowHeadPath(a, b, width) {
  const ang = Math.atan2(b[1] - a[1], b[0] - a[0]), len = Math.max(8, width * 4.5), spread = 0.45;
  const p = k => `${+(b[0] - len * Math.cos(ang + k * spread)).toFixed(2)} ${+(b[1] - len * Math.sin(ang + k * spread)).toFixed(2)}`;
  return `M${p(1)}L${+b[0].toFixed(2)} ${+b[1].toFixed(2)}L${p(-1)}`;
}
export function inkPath(pts) {
  if (pts.length === 1) return `M${pts[0][0]} ${pts[0][1]} l0.01 0`;
  return 'M' + pts.map(p => `${+p[0].toFixed(2)} ${+p[1].toFixed(2)}`).join('L');
}

export function updateObjectEl(el, obj) {
  const s = el.style;
  s.left = `${obj.x}px`; s.top = `${obj.y}px`; s.width = `${obj.w}px`;
  s.transform = obj.rot ? `rotate(${obj.rot}deg)` : '';
  switch (obj.type) {
    case 'text':
      s.height = 'auto';
      s.fontFamily = FONT_CSS[obj.font] || FONT_CSS.helvetica;
      s.fontSize = `${obj.size}px`;
      s.fontWeight = obj.bold ? '700' : '400';
      s.fontStyle = obj.italic ? 'italic' : 'normal';
      s.textDecoration = obj.underline ? 'underline' : 'none';
      s.color = obj.color; s.textAlign = obj.align;
      s.background = obj.bg || 'transparent'; s.opacity = obj.opacity ?? 1;
      if (!el.classList.contains('editing') && el.textContent !== obj.text) el.textContent = obj.text;
      break;
    case 'image': case 'signature':
      s.height = `${obj.h}px`; s.opacity = obj.opacity ?? 1; break;
    case 'highlight':
      s.height = `${obj.h}px`; s.background = obj.color; s.opacity = obj.opacity; break;
    case 'shape': {
      s.height = `${obj.h}px`; s.opacity = obj.opacity;
      const svg = el.firstChild, g = svg.firstChild, sw = obj.width;
      svg.setAttribute('width', obj.w); svg.setAttribute('height', obj.h);
      g.setAttribute('fill', obj.fill || 'none'); g.setAttribute('stroke', obj.stroke); g.setAttribute('stroke-width', sw);
      if (obj.kind === 'ellipse') { g.setAttribute('cx', obj.w / 2); g.setAttribute('cy', obj.h / 2); g.setAttribute('rx', Math.max(0, obj.w / 2 - sw / 2)); g.setAttribute('ry', Math.max(0, obj.h / 2 - sw / 2)); }
      else { g.setAttribute('x', sw / 2); g.setAttribute('y', sw / 2); g.setAttribute('width', Math.max(0, obj.w - sw)); g.setAttribute('height', Math.max(0, obj.h - sw)); }
      break;
    }
    case 'line': {
      s.height = `${obj.h}px`; s.opacity = obj.opacity;
      const svg = el.firstChild, [p1, p2] = svg.children, [a, b] = obj.pts;
      svg.setAttribute('width', obj.w); svg.setAttribute('height', obj.h);
      p1.setAttribute('d', `M${a[0]} ${a[1]}L${b[0]} ${b[1]}`);
      for (const p of [p1, p2]) { p.setAttribute('fill', 'none'); p.setAttribute('stroke', obj.color); p.setAttribute('stroke-width', obj.width); p.setAttribute('stroke-linecap', 'round'); p.setAttribute('stroke-linejoin', 'round'); }
      p2.setAttribute('d', obj.arrow ? arrowHeadPath(a, b, obj.width) : '');
      break;
    }
    case 'cover':
      s.height = `${obj.h}px`; s.background = obj.color; break;
    case 'redact':
      s.height = `${obj.h}px`; el.firstChild.textContent = obj.h > 14 && obj.w > 60 ? 'REDACT' : ''; break;
    case 'link':
      s.height = `${obj.h}px`; el.firstChild.textContent = obj.kind === 'page' ? `→ page` : '🔗'; el.title = obj.kind === 'page' ? 'Link to a page' : obj.url; break;
    case 'note':
      s.height = `${obj.h}px`; s.background = obj.color; el.firstChild.textContent = '✎'; el.title = obj.text || 'Note'; break;
    case 'field': {
      s.height = `${obj.h}px`; s.fontSize = `${Math.min(obj.size, Math.max(6, obj.h - 3))}px`;
      const lab = el.firstChild;
      el.dataset.fkind = obj.fkind;
      lab.textContent = obj.fkind === 'checkbox' ? (obj.checked ? '✓' : '') : obj.fkind === 'radio' ? (obj.checked ? '●' : '') : obj.fkind === 'dropdown' ? `${obj.value || obj.name}  ▾` : (obj.value || (obj.fkind === 'date' ? `${obj.name} (date)` : obj.name));
      el.title = `Form field: ${obj.name}`;
      break;
    }
    case 'ink': {
      s.height = `${obj.h}px`; s.opacity = obj.opacity;
      const svg = el.firstChild, path = svg.firstChild;
      svg.setAttribute('width', obj.w); svg.setAttribute('height', obj.h);
      path.setAttribute('d', inkPath(obj.pts));
      path.setAttribute('fill', 'none'); path.setAttribute('stroke', obj.color);
      path.setAttribute('stroke-width', obj.width); path.setAttribute('stroke-linecap', 'round'); path.setAttribute('stroke-linejoin', 'round');
      break;
    }
  }
}

/** Keep text objects' stored height in sync with the laid-out DOM height. */
export function measureText(obj, el) {
  if (obj.type !== 'text' || !el || !el.isConnected) return;
  const hh = el.offsetHeight;
  if (hh > 0) obj.h = hh;
}

export function renderPageObjects(pageId) {
  const overlay = overlayOf(pageId); if (!overlay) return;
  const page = state.doc.pages.find(p => p.id === pageId); if (!page) return;
  overlay.querySelectorAll(':scope > .obj').forEach(n => n.remove());
  for (const obj of page.objects) {
    const el = createObjectEl(obj);
    overlay.insertBefore(el, selBox && selBox.parentNode === overlay ? selBox : null);
    measureText(obj, el);
  }
  refreshSelection();
}

export function refreshObject(obj) {
  const el = elOf(obj.id);
  if (el) { updateObjectEl(el, obj); measureText(obj, el); }
  refreshSelection();
}

/* ---------- selection box ---------- */
let selBox = null;
function buildSelBox() {
  const box = document.createElement('div');
  box.className = 'sel-box';
  const stem = document.createElement('div'); stem.className = 'stem'; box.append(stem);
  for (const hd of [...HANDLES, 'rot']) {
    const d = document.createElement('div'); d.className = 'handle'; d.dataset.h = hd; box.append(d);
  }
  return box;
}
export function isSelBox(node) { return !!selBox && selBox.contains(node); }

export function refreshSelection() {
  const f = findObject(state.selection);
  if (!f) { selBox?.remove(); return; }
  const overlay = overlayOf(f.page.id);
  if (!overlay) { selBox?.remove(); return; }
  if (!selBox) selBox = buildSelBox();
  if (selBox.parentNode !== overlay) overlay.append(selBox);
  const o = f.obj, s = selBox.style, caps = CAPS[o.type];
  s.left = `${o.x}px`; s.top = `${o.y}px`; s.width = `${o.w}px`; s.height = `${o.h}px`;
  s.transform = o.rot ? `rotate(${o.rot}deg)` : '';
  selBox.dataset.resize = caps.resize;
  selBox.dataset.rotate = String(caps.rotate);
}

export function select(id) {
  if (state.selection === id) { refreshSelection(); return; }
  state.selection = id;
  refreshSelection();
  emit('selection', id);
}

/* ---------- CRUD ---------- */
/** Insert an object into the model + DOM without touching history (caller commits). */
export function insertObject(pageId, obj) {
  const page = state.doc.pages.find(p => p.id === pageId);
  page.objects.push(obj);
  const overlay = overlayOf(pageId);
  const el = createObjectEl(obj);
  overlay.insertBefore(el, selBox && selBox.parentNode === overlay ? selBox : null);
  measureText(obj, el);
  return el;
}
export function addObject(pageId, obj, label = 'Add object', { select: doSelect = true } = {}) {
  insertObject(pageId, obj);
  history.commit(label);
  if (doSelect) select(obj.id);
}

/** Pointer position in page points for a given overlay element. */
export function pagePoint(e, overlay) {
  const r = overlay.getBoundingClientRect();
  return [(e.clientX - r.left) / state.zoom, (e.clientY - r.top) / state.zoom];
}
export function deleteObject(id, label = 'Delete') {
  const f = findObject(id); if (!f) return;
  f.page.objects.splice(f.page.objects.indexOf(f.obj), 1);
  elOf(id)?.remove();
  if (state.selection === id) { state.selection = null; refreshSelection(); emit('selection', null); }
  history.commit(label);
}
export function duplicateObject(id) {
  const f = findObject(id); if (!f) return;
  const copy = structuredClone(f.obj);
  copy.id = uid(copy.type[0]); copy.x += 12; copy.y += 12;
  addObject(f.page.id, copy, 'Duplicate');
}
