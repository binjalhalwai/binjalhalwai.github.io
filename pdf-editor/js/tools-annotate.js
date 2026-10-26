// Freehand drawing and rectangle highlight.
import { state } from './state.js';
import { addObject, deleteObject, newInkObject, newHighlightObject, newShapeObject, newLineObject, newCoverObject, newRedactObject, newLinkObject, newNoteObject, newFieldObject, pagePoint, inkPath, arrowHeadPath } from './objects.js';
import { toast } from './utils.js';

const SVGNS = 'http://www.w3.org/2000/svg';

function track(e, onMove, onUp) {
  const id = e.pointerId;
  const mv = ev => { if (ev.pointerId === id) onMove(ev); };
  const up = ev => {
    if (ev.pointerId !== id) return;
    window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', up);
    onUp(ev);
  };
  window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
}

export function startInk(e, overlay, pageId) {
  const p = state.props.draw;
  const pts = [pagePoint(e, overlay)];
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'ink-live');
  const path = document.createElementNS(SVGNS, 'path');
  for (const [k, v] of Object.entries({ fill: 'none', stroke: p.color, 'stroke-width': p.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: p.opacity })) path.setAttribute(k, v);
  svg.append(path); overlay.append(svg);
  path.setAttribute('d', inkPath(pts));
  track(e, ev => {
    const q = pagePoint(ev, overlay), last = pts[pts.length - 1];
    if (Math.hypot(q[0] - last[0], q[1] - last[1]) < 0.6) return;
    pts.push(q);
    path.setAttribute('d', inkPath(pts));
  }, () => {
    svg.remove();
    addObject(pageId, newInkObject(pts), 'Draw', { select: false });
  });
}

/* Tools that create an object by dragging a rectangle (or clicking, for form fields). */
const FIELD_SIZES = { text: [170, 22], checkbox: [16, 16], radio: [16, 16], dropdown: [150, 22], date: [110, 22] };
export const RECT_TOOLS = {
  highlight: { make: (x, y, w, h) => newHighlightObject(x, y, w, h), label: 'Highlight', select: false, color: () => state.props.highlight.color, alpha: () => state.props.highlight.opacity },
  rect: { make: (x, y, w, h) => newShapeObject('rect', x, y, w, h), label: 'Rectangle', outline: true },
  ellipse: { make: (x, y, w, h) => newShapeObject('ellipse', x, y, w, h), label: 'Ellipse', outline: true, round: true },
  underline: { make: (x, y, w, h) => newLineObject([x, y + h], [x + w, y + h], false, { width: 1.2 }), label: 'Underline', select: false, outline: true },
  strike: { make: (x, y, w, h) => newLineObject([x, y + h / 2], [x + w, y + h / 2], false, { width: 1.2 }), label: 'Strikethrough', select: false, outline: true },
  whiteout: { make: (x, y, w, h) => newCoverObject('whiteout', x, y, w, h), label: 'Whiteout', color: () => state.props.cover.whiteout, alpha: () => 1, outline: true },
  blackout: { make: (x, y, w, h) => newCoverObject('blackout', x, y, w, h), label: 'Blackout', color: () => state.props.cover.blackout, alpha: () => 1 },
  redact: { make: (x, y, w, h) => newRedactObject(x, y, w, h), label: 'Redact', color: () => '#000', alpha: () => 0.55 },
  link: { make: (x, y, w, h) => newLinkObject(x, y, w, h), label: 'Link', outline: true },
  ...Object.fromEntries(Object.entries(FIELD_SIZES).map(([k, sz]) => [`f-${k}`, { make: (x, y, w, h) => newFieldObject(k, x, y, w, h), label: `${k} field`, outline: true, clickSize: sz }])),
};

export function startRectTool(e, overlay, pageId, tool) {
  const cfg = RECT_TOOLS[tool];
  const [sx, sy] = pagePoint(e, overlay);
  const box = document.createElement('div');
  Object.assign(box.style, { position: 'absolute', left: `${sx}px`, top: `${sy}px`, width: '0px', height: '0px', pointerEvents: 'none',
    ...(cfg.outline ? { border: `${1.5 / state.zoom}px dashed #2563eb`, background: 'rgba(37,99,235,.08)' } : { background: cfg.color(), opacity: cfg.alpha() }), ...(cfg.round ? { borderRadius: '50%' } : {}) });
  overlay.append(box);
  let cur = [sx, sy];
  track(e, ev => {
    cur = pagePoint(ev, overlay);
    Object.assign(box.style, { left: `${Math.min(sx, cur[0])}px`, top: `${Math.min(sy, cur[1])}px`, width: `${Math.abs(cur[0] - sx)}px`, height: `${Math.abs(cur[1] - sy)}px` });
  }, () => {
    box.remove();
    let w = Math.abs(cur[0] - sx), hh = Math.abs(cur[1] - sy), x = Math.min(sx, cur[0]), y = Math.min(sy, cur[1]);
    if (w < 3 || hh < 3) {
      if (!cfg.clickSize) return;
      [w, hh] = cfg.clickSize; x = sx; y = sy;
    }
    const page = state.doc.pages.find(p => p.id === pageId);
    const o = cfg.make(x, y, w, hh);
    if (tool === 'redact' || tool === 'blackout' || tool === 'whiteout') { /* keep as drawn */ }
    if (page) addObject(pageId, o, cfg.label, { select: cfg.select !== false });
  });
}
export const startHighlight = (e, overlay, pageId) => startRectTool(e, overlay, pageId, 'highlight');

export function startLine(e, overlay, pageId, arrow) {
  const p = state.props.shape, a = pagePoint(e, overlay);
  const svg = document.createElementNS(SVGNS, 'svg'); svg.setAttribute('class', 'ink-live');
  const path = document.createElementNS(SVGNS, 'path'), head = document.createElementNS(SVGNS, 'path');
  for (const n of [path, head]) for (const [k, v] of Object.entries({ fill: 'none', stroke: p.stroke, 'stroke-width': p.width, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: p.opacity })) n.setAttribute(k, v);
  svg.append(path, head); overlay.append(svg);
  let b = a;
  track(e, ev => {
    b = pagePoint(ev, overlay);
    if (ev.shiftKey) { const ang = Math.round(Math.atan2(b[1] - a[1], b[0] - a[0]) / (Math.PI / 12)) * (Math.PI / 12), d = Math.hypot(b[0] - a[0], b[1] - a[1]); b = [a[0] + d * Math.cos(ang), a[1] + d * Math.sin(ang)]; }
    path.setAttribute('d', `M${a[0]} ${a[1]}L${b[0]} ${b[1]}`);
    head.setAttribute('d', arrow ? arrowHeadPath(a, b, p.width) : '');
  }, () => {
    svg.remove();
    if (Math.hypot(b[0] - a[0], b[1] - a[1]) < 4) return;
    addObject(pageId, newLineObject(a, b, arrow), arrow ? 'Arrow' : 'Line');
  });
}

export function placeNote(e, overlay, pageId) {
  const [x, y] = pagePoint(e, overlay);
  addObject(pageId, newNoteObject(x, y), 'Add note');
  toast('Type the note text in the panel on the right. It is saved as a PDF comment.', 'info', 4500);
}

export function startErase(e) {
  const hit = new Set();
  const grab = ev => { const t = document.elementFromPoint(ev.clientX, ev.clientY)?.closest?.('.obj'); if (t) hit.add(t.dataset.id); };
  grab(e);
  track(e, grab, () => { for (const id of hit) deleteObject(id, 'Erase'); });
}
