// Text layer access: positions of the existing text of a page (via pdf.js), in editor view space (points, y-down).
import pdfjsLib from './pdfjs.js';
import { getPageProxy } from './pdf-renderer.js';
import { cropOffset } from './page-geom.js';
import { state } from './state.js';

const cache = new Map(); // "src:index:rot" -> Promise<raw items>

function guessFont(styleFamily, name = '') {
  const n = `${styleFamily} ${name}`;
  const font = /mono|courier|consolas|typewriter/i.test(n) ? 'courier' : /serif(?!.*sans)|times|roman|georgia|garamond|minion|palatino|cambria|book/i.test(n) && !/sans/i.test(n) ? 'times' : 'helvetica';
  return { font, bold: /bold|black|heavy|semibold|demi/i.test(name), italic: /italic|oblique/i.test(name) };
}

async function loadRaw(model) {
  const key = `${model.src}:${model.srcIndex}:${model.rot}`;
  if (!cache.has(key)) {
    cache.set(key, (async () => {
      const pg = await getPageProxy(model);
      const vp = pg.getViewport({ scale: 1, rotation: model.rot });
      const tc = await pg.getTextContent();
      const items = [];
      for (const it of tc.items) {
        if (!it.str || !it.str.trim() || !it.width) continue;
        const tx = pdfjsLib.Util.transform(vp.transform, it.transform);
        const fs = Math.hypot(tx[0], tx[1]), up = Math.hypot(tx[2], tx[3]);
        if (!fs || !up) continue;
        const ux = tx[0] / fs, uy = tx[1] / fs, vx = tx[2] / up, vy = tx[3] / up, W = it.width;
        const pts = [[tx[4], tx[5]], [tx[4] + ux * W, tx[5] + uy * W]];
        const withAsc = pts.map(p => [p[0] + vx * fs * 0.82, p[1] + vy * fs * 0.82]), withDesc = pts.map(p => [p[0] - vx * fs * 0.22, p[1] - vy * fs * 0.22]);
        const all = [...pts, ...withAsc, ...withDesc];
        const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
        let name = ''; try { name = pg.commonObjs.has(it.fontName) ? pg.commonObjs.get(it.fontName).name || '' : ''; } catch { /* font not loaded yet */ }
        const g = guessFont(tc.styles[it.fontName]?.fontFamily || '', name);
        items.push({ str: it.str, eol: !!it.hasEOL, x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys), fs, horizontal: Math.abs(uy) < 0.08, baseX: tx[4], baseY: tx[5], ...g });
      }
      return items;
    })().catch(() => []));
  }
  return cache.get(key);
}
export const clearTextCache = () => cache.clear();

/** Text items of a page in the page's current view space (after crop). */
export async function getTextItems(model) {
  if (model.kind !== 'pdf') return [];
  const raw = await loadRaw(model), [ox, oy] = cropOffset(model);
  return raw.map(r => ({ ...r, x: r.x - ox, y: r.y - oy, baseX: r.baseX - ox, baseY: r.baseY - oy }));
}

/** Full text of a page + offsets of each item inside it (for searching). */
export async function getPageText(model) {
  const items = await getTextItems(model);
  let text = ''; const starts = [];
  for (const it of items) { starts.push(text.length); text += it.str + (it.eol ? '\n' : ' '); }
  return { items, text, starts };
}

let mctx = null;
export function measure(str, font, size, bold, italic) {
  mctx ||= document.createElement('canvas').getContext('2d');
  mctx.font = `${italic ? 'italic ' : ''}${bold ? '700 ' : '400 '}${size}px ${font === 'times' ? 'serif' : font === 'courier' ? 'monospace' : 'sans-serif'}`;
  return mctx.measureText(str).width;
}
/** Rectangle covering characters [a,b) of one item (proportional to rendered character widths). */
export function subRect(it, a, b) {
  const total = measure(it.str, it.font, it.fs, it.bold, it.italic) || 1;
  const x0 = measure(it.str.slice(0, a), it.font, it.fs, it.bold, it.italic) / total, x1 = measure(it.str.slice(0, b), it.font, it.fs, it.bold, it.italic) / total;
  return { x: it.x + it.w * x0, y: it.y, w: Math.max(2, it.w * (x1 - x0)), h: it.h };
}
export const currentModelById = id => state.doc?.pages.find(p => p.id === id);
