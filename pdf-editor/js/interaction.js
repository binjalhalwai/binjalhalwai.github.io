// Pointer interaction on page overlays: select/move/resize/rotate, tool dispatch, placement.
import { state, findObject, setTool } from './state.js';
import * as history from './history.js';
import { select, refreshObject, isSelBox, CAPS, pagePoint } from './objects.js';
import * as text from './tools-text.js';
import * as annotate from './tools-annotate.js';
import * as image from './tools-image.js';
import * as find from './find.js';
import { $ } from './utils.js';

export function init() {
  const pages = $('#pages');
  pages.addEventListener('pointerdown', onDown);
  pages.addEventListener('dblclick', e => {
    const el = e.target.closest('.obj-text');
    if (el && !el.classList.contains('editing')) text.beginEdit(el.dataset.id, { selectAll: false });
  });
}

function drag(e, move, up) {
  const id = e.pointerId;
  const mv = ev => { if (ev.pointerId === id) move(ev); };
  const end = ev => {
    if (ev.pointerId !== id) return;
    window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', end); window.removeEventListener('pointercancel', end);
    up(ev);
  };
  window.addEventListener('pointermove', mv); window.addEventListener('pointerup', end); window.addEventListener('pointercancel', end);
}

function onDown(e) {
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const overlay = e.target.closest('.overlay');
  if (!overlay) return;
  const pageId = overlay.dataset.pageId;

  if (state.pending) { e.preventDefault(); const [x, y] = pagePoint(e, overlay); image.placePending(pageId, x, y); return; }

  const handle = e.target.closest('.handle');
  if (handle && isSelBox(handle)) {
    const f = findObject(state.selection); if (!f) return;
    e.preventDefault(); e.stopPropagation();
    if (handle.dataset.h === 'rot') startRotate(e, f, overlay); else startResize(e, f, overlay, handle.dataset.h);
    return;
  }
  const tool = state.tool;
  if (text.isEditing() && !e.target.closest('.obj-text.editing')) text.stopEditing();

  if (tool === 'draw') { e.preventDefault(); annotate.startInk(e, overlay, pageId); return; }
  if (annotate.RECT_TOOLS[tool]) { e.preventDefault(); annotate.startRectTool(e, overlay, pageId, tool); return; }
  if (tool === 'line' || tool === 'arrow') { e.preventDefault(); annotate.startLine(e, overlay, pageId, tool === 'arrow'); return; }
  if (tool === 'note') { e.preventDefault(); annotate.placeNote(e, overlay, pageId); return; }
  if (tool === 'eraser') { e.preventDefault(); annotate.startErase(e); return; }
  if (tool === 'edittext') {
    const hit = e.target.closest('.tx-hit');
    if (hit && !hit.classList.contains('done')) { e.preventDefault(); find.editHit(hit); }
    return;
  }

  const objEl = e.target.closest('.obj');
  if (objEl) {
    if (objEl.classList.contains('editing')) return;       // let the caret work
    const id = objEl.dataset.id;
    const wasSelected = state.selection === id;
    select(id);
    e.preventDefault();
    startMove(e, id, wasSelected);
    return;
  }
  if (tool === 'text') {
    e.preventDefault();
    const [x, y] = pagePoint(e, overlay);
    text.createTextAt(pageId, x, y);
    return;
  }
  select(null);
}

function startMove(e, id, wasSelected) {
  const f = findObject(id); if (!f) return;
  const o = f.obj, z = state.zoom, sx = e.clientX, sy = e.clientY, ox = o.x, oy = o.y;
  let moved = false;
  drag(e, ev => {
    const dx = (ev.clientX - sx) / z, dy = (ev.clientY - sy) / z;
    if (!moved && Math.hypot(dx, dy) * z < 3) return;
    moved = true;
    o.x = ox + dx; o.y = oy + dy;
    refreshObject(o);
  }, () => {
    if (moved) history.commit('Move');
    else if (wasSelected && o.type === 'text') text.beginEdit(id);
  });
}

function startResize(e, f, overlay, hd) {
  const o = f.obj, caps = CAPS[o.type], z = state.zoom;
  const st = { x: o.x, y: o.y, w: o.w, h: o.h, pts: o.pts && o.pts.map(p => [p[0], p[1]]) };
  const rect = overlay.getBoundingClientRect();
  const hx = hd.includes('e') ? 1 : hd.includes('w') ? -1 : 0;
  const hy = hd.includes('s') ? 1 : hd.includes('n') ? -1 : 0;
  const th = (o.rot || 0) * Math.PI / 180, cos = Math.cos(th), sin = Math.sin(th);
  const c0 = { x: st.x + st.w / 2, y: st.y + st.h / 2 };
  const minW = o.type === 'text' ? 24 : 8, minH = 8;
  let changed = false;
  drag(e, ev => {
    const px = (ev.clientX - rect.left) / z, py = (ev.clientY - rect.top) / z;
    const dx = px - c0.x, dy = py - c0.y;
    const lx = dx * cos + dy * sin, ly = -dx * sin + dy * cos;          // pointer in the object's local frame
    const ax = -hx * st.w / 2, ay = -hy * st.h / 2;                     // fixed anchor (opposite side)
    let nw = hx ? hx * (lx - ax) : st.w, nh = hy ? hy * (ly - ay) : st.h;
    nw = Math.max(nw, minW); nh = Math.max(nh, minH);
    if (caps.aspect && hx && hy) { const s = Math.max(nw / st.w, nh / st.h); nw = st.w * s; nh = st.h * s; }
    const cl = { x: ax + hx * nw / 2, y: ay + hy * nh / 2 };
    const cx = c0.x + cl.x * cos - cl.y * sin, cy = c0.y + cl.x * sin + cl.y * cos;
    o.w = nw; o.h = nh; o.x = cx - nw / 2; o.y = cy - nh / 2;
    if (o.pts) { const sx = nw / st.w, sy = nh / st.h; o.pts = st.pts.map(p => [p[0] * sx, p[1] * sy]); }
    changed = true;
    refreshObject(o);
  }, () => { if (changed) history.commit('Resize'); });
}

function startRotate(e, f, overlay) {
  const o = f.obj, z = state.zoom, rect = overlay.getBoundingClientRect();
  const c0 = { x: o.x + o.w / 2, y: o.y + o.h / 2 };
  let changed = false;
  drag(e, ev => {
    const px = (ev.clientX - rect.left) / z, py = (ev.clientY - rect.top) / z;
    let a = Math.atan2(py - c0.y, px - c0.x) * 180 / Math.PI + 90;
    if (ev.shiftKey) a = Math.round(a / 15) * 15;
    else { const n = Math.round(a / 90) * 90; if (Math.abs(a - n) < 3) a = n; }   // gentle snap to right angles
    a = ((a + 540) % 360) - 180;
    o.rot = Math.round(a * 10) / 10;
    changed = true;
    refreshObject(o);
  }, () => { if (changed) history.commit('Rotate'); });
}
