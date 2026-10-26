// Renders PDF pages to canvases with cancellation, DPR/size caps and a page-proxy cache.
import { state } from './state.js';
import { cropOffset } from './page-geom.js';

let annotationMode = 2;                  // 2 = ENABLE_FORMS: widgets are drawn by the editor's own form layer
export const setAnnotationMode = m => { annotationMode = m; };

const proxyCache = new Map(); // "src:index" -> Promise<PDFPageProxy>

export function getPageProxy(model) {
  const key = `${model.src}:${model.srcIndex}`;
  if (!proxyCache.has(key)) {
    const src = state.doc.sources.get(model.src);
    proxyCache.set(key, src.pdf.getPage(model.srcIndex + 1));
  }
  return proxyCache.get(key);
}

export function clearRendererCache() { proxyCache.clear(); }

export function releasePageProxy(model) {
  const key = `${model.src}:${model.srcIndex}`;
  const p = proxyCache.get(key);
  if (p) p.then(pg => { try { pg.cleanup(); } catch { /* ignore */ } }).catch(() => {});
}

/**
 * Start rendering a page. Returns { promise, cancel }. promise resolves to a canvas,
 * or null if it was cancelled.
 * scale = CSS px per PDF point.  opts.dpr=false renders at 1 device px per CSS px (thumbnails).
 */
export function startRender(model, scale, opts = {}) {
  const { dpr = true, maxPixels = 16_000_000 } = opts;
  let cancelled = false, task = null;
  const promise = (async () => {
    const page = await getPageProxy(model);
    if (cancelled) return null;
    let out = dpr ? Math.min(window.devicePixelRatio || 1, 2.5) : 1;
    const cssW = model.w * scale, cssH = model.h * scale;
    if (cssW * cssH * out * out > maxPixels) out = Math.max(0.25, Math.sqrt(maxPixels / (cssW * cssH)));
    const s = scale * out;
    const viewport = page.getViewport({ scale: s, rotation: model.rot });
    const [ox, oy] = cropOffset(model);                              // cropped pages: shift the render so only the CropBox shows
    const cropped = ox !== 0 || oy !== 0 || Math.abs(model.w * s - viewport.width) > 1 || Math.abs(model.h * s - viewport.height) > 1;
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(cropped ? model.w * s : viewport.width));
    canvas.height = Math.max(1, Math.floor(cropped ? model.h * s : viewport.height));
    const ctx = canvas.getContext('2d', { alpha: false });
    task = page.render({ canvasContext: ctx, viewport, background: '#ffffff', annotationMode: opts.annotations ?? annotationMode, transform: cropped ? [1, 0, 0, 1, -ox * s, -oy * s] : undefined });
    try {
      await task.promise;
    } catch (e) {
      if (e && e.name === 'RenderingCancelledException') return null;
      throw e;
    }
    return cancelled ? null : canvas;
  })();
  return { promise, cancel() { cancelled = true; try { task && task.cancel(); } catch { /* ignore */ } } };
}

/** Render a page to a JPEG (flattened pages, secure redaction, compression, PDF->image). Black `rects` (view-space points) are burned into the pixels. */
export async function renderPageToJpeg(model, scale = 2, quality = 0.9, rects = []) {
  const canvas = await renderPageToCanvas(model, scale, rects);
  const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', quality));
  canvas.width = canvas.height = 0;
  return new Uint8Array(await blob.arrayBuffer());
}
export async function renderPageToCanvas(model, scale = 2, rects = []) {
  if (model.kind !== 'pdf') {                                      // blank page: white canvas
    const c = document.createElement('canvas'); c.width = Math.ceil(model.w * scale); c.height = Math.ceil(model.h * scale);
    const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, c.width, c.height); return c;
  }
  const job = startRender(model, scale, { dpr: false, maxPixels: 24_000_000, annotations: 1 });
  const canvas = await job.promise;
  const s = canvas.width / model.w;
  if (rects.length) { const x = canvas.getContext('2d'); x.fillStyle = '#000'; for (const r of rects) x.fillRect(Math.floor(r.x * s), Math.floor(r.y * s), Math.ceil(r.w * s) + 1, Math.ceil(r.h * s) + 1); }
  return canvas;
}
