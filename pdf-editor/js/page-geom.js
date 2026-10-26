// Page geometry helpers shared by the renderer, page operations and the exporter.
// A page model has: view = visible box in PDF user space [x0,y0,x1,y1] (the CropBox), rot = total rotation,
// w/h = size of the *rotated* visible box in points. Editor ("view space") coordinates are y-down, origin top-left.

/** matrix mapping y-up "view" space (x, H - y) into the page's user space, for a given total rotation. */
export function viewToUserMatrix(view, rot) {
  const [x0, y0, x1, y1] = view;
  switch (((rot % 360) + 360) % 360) {
    case 90: return [0, 1, -1, 0, x1, y0];
    case 180: return [-1, 0, 0, -1, x1, y1];
    case 270: return [0, -1, 1, 0, x0, y1];
    default: return [1, 0, 0, 1, x0, y0];
  }
}

/** user-space point -> y-down view-space point inside `view` for rotation `rot`. */
export function userToView(view, rot, ux, uy) {
  const [x0, y0, x1, y1] = view;
  switch (((rot % 360) + 360) % 360) {
    case 90: return [uy - y0, ux - x0];
    case 180: return [x1 - ux, uy - y0];
    case 270: return [y1 - uy, x1 - ux];
    default: return [ux - x0, y1 - uy];
  }
}

/** y-down view-space point -> user-space point. */
export function viewToUser(view, rot, vx, vy, H) {
  const m = viewToUserMatrix(view, rot), yv = H - vy;
  return [m[0] * vx + m[2] * yv + m[4], m[1] * vx + m[3] * yv + m[5]];
}

/** bounding box (user space) of a view-space rectangle. */
export function viewRectToUserBox(view, rot, H, x, y, w, h) {
  const pts = [[x, y], [x + w, y], [x, y + h], [x + w, y + h]].map(p => viewToUser(view, rot, p[0], p[1], H));
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

export const sameBox = (a, b) => !!a && !!b && a.length === 4 && b.length === 4 && a.every((v, i) => Math.abs(v - b[i]) < 0.001);

/** offset (points) of the current visible box inside the page's original (uncropped) rotated view. */
export function cropOffset(m) {
  if (!m.view0 || sameBox(m.view, m.view0)) return [0, 0];
  const c = [[m.view[0], m.view[1]], [m.view[2], m.view[1]], [m.view[0], m.view[3]], [m.view[2], m.view[3]]].map(p => userToView(m.view0, m.rot, p[0], p[1]));
  return [Math.min(...c.map(p => p[0])), Math.min(...c.map(p => p[1]))];
}

/** Rotate a page model 90° clockwise `steps` times, carrying objects (and OCR words) with it. Mutates the model. */
export function rotatePageModel(m, steps) {
  steps = ((steps % 4) + 4) % 4;
  for (let s = 0; s < steps; s++) {
    const W = m.w, H = m.h;
    for (const o of m.objects) rotateObject90(o, W, H);
    if (m.ocr) m.ocr.words = m.ocr.words.map(([x, y, w, h, t]) => [H - y - h, x, h, w, t]);
    m.w = H; m.h = W;
    m.rot = (m.rot + 90) % 360;
  }
}

/** One 90° clockwise step of an object living in a W×H page view: point (x,y) -> (H - y, x). */
function rotateObject90(o, W, H) {
  if (!ROT_FREE.has(o.type)) {
    // boxes that cannot be rotated individually: rotate the box itself
    const nx = H - (o.y + o.h), ny = o.x;
    const w = o.w, h = o.h;
    o.x = nx; o.y = ny; o.w = h; o.h = w;
    if (o.pts) o.pts = o.pts.map(p => [h - p[1], p[0]]);
    return;
  }
  const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
  const ncx = H - cy, ncy = cx;
  o.x = ncx - o.w / 2; o.y = ncy - o.h / 2;
  o.rot = ((((o.rot || 0) + 90 + 180) % 360) + 360) % 360 - 180;
}
// object types that carry their own rotation angle (everything else is rotated geometrically)
const ROT_FREE = new Set(['text', 'image', 'signature']);

/** Crop a page model: (cx,cy,cw,ch) is the new visible rectangle in the page's current view space. */
export function cropPageModel(m, cx, cy, cw, ch) {
  const nv = viewRectToUserBox(m.view, m.rot, m.h, cx, cy, cw, ch);
  for (const o of m.objects) { o.x -= cx; o.y -= cy; }
  if (m.ocr) m.ocr.words = m.ocr.words.map(([x, y, w, h, t]) => [x - cx, y - cy, w, h, t]);
  m.view = nv; m.w = cw; m.h = ch;
}
