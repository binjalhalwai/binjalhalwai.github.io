// Builds the edited PDF with pdf-lib (loaded as window.PDFLib from the local vendor folder).
// Editor coordinates (points, y-down, rotated "view" space) are mapped into each page's real user space,
// honouring /Rotate and non-zero MediaBox/CropBox origins.
import { state } from './state.js';
import { FONT_CSS } from './objects.js';
import { wrapLines, BASELINE, LINE_H } from './text-layout.js';
import { renderPageToJpeg } from './pdf-renderer.js';
import { viewToUserMatrix, viewRectToUserBox, sameBox } from './page-geom.js';
import { getWidgets, initialValue } from './forms.js';

export { viewToUserMatrix };
export class ExportError extends Error { constructor(code, msg) { super(msg || code); this.code = code; } }
/** Human-readable notes from the last export (e.g. a form field that could not be written). */
export const exportNotes = [];

const FONT_MAP = {
  helvetica: ['Helvetica', 'HelveticaBold', 'HelveticaOblique', 'HelveticaBoldOblique'],
  times: ['TimesRoman', 'TimesRomanBold', 'TimesRomanItalic', 'TimesRomanBoldItalic'],
  courier: ['Courier', 'CourierBold', 'CourierOblique', 'CourierBoldOblique'],
};
const hexToRgb = (hex, { rgb }) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '#000000') || [0, '00', '00', '00'];
  return rgb(parseInt(m[1], 16) / 255, parseInt(m[2], 16) / 255, parseInt(m[3], 16) / 255);
};
const tick = () => new Promise(r => setTimeout(r, 0));
const SAFE_URL = /^(https?:\/\/|mailto:|tel:)/i;
export const isSafeUrl = u => SAFE_URL.test(String(u || '').trim());

async function getSourceBytes(src) {
  if (src.bytes) return src.bytes;
  return new Uint8Array(await src.file.arrayBuffer());
}

const objNum = ref => (ref && typeof ref.objectNumber === 'number' ? ref.objectNumber : -1);

/** Remove annotations of a given /Subtype (optionally only specific object numbers) from a pdf-lib page. */
function removeAnnots(L, out, page, subtype, onlyNums = null) {
  const { PDFName, PDFArray } = L;
  const annots = page.node.Annots();
  if (!annots) return;
  const keep = [];
  for (let i = 0; i < annots.size(); i++) {
    const ref = annots.get(i), d = out.context.lookup(annots.get(i));
    const st = d && d.get && d.get(PDFName.of('Subtype'));
    const hit = st && st.toString() === `/${subtype}` && (!onlyNums || onlyNums.has(objNum(ref)));
    if (!hit) keep.push(ref);
  }
  if (keep.length === annots.size()) return;
  if (keep.length) page.node.set(PDFName.of('Annots'), out.context.obj(keep)); else page.node.delete(PDFName.of('Annots'));
  void PDFArray;
}
function pushAnnot(L, out, page, dict) {
  const { PDFName, PDFArray } = L;
  const ref = out.context.register(out.context.obj(dict));
  let annots = page.node.Annots();
  if (!annots) { annots = out.context.obj([]); page.node.set(PDFName.of('Annots'), annots); }
  if (annots instanceof PDFArray) annots.push(ref);
  return ref;
}

export async function exportPdf({ onProgress = () => {}, allowRaster = false, pages: subset = null, flattenForms = false, rasterAll = false, rasterScale = 2.5, rasterQuality = 0.9 } = {}) {
  const L = window.PDFLib;
  if (!L) throw new ExportError('NOLIB', 'PDF library failed to load.');
  const { PDFDocument, StandardFonts, degrees, pushGraphicsState, popGraphicsState, concatTransformationMatrix, setTextRenderingMode, TextRenderingMode } = L;
  const doc = state.doc;
  const pages = subset || doc.pages;
  exportNotes.length = 0;

  /* 1. load the sources that are actually used (may fail for encrypted / unusual files) */
  if (rasterAll) allowRaster = true;
  const usedSrc = [...new Set(pages.filter(p => p.kind === 'pdf').map(p => p.src))];
  const loaded = new Map();
  let needRaster = false;
  for (const id of usedSrc) {
    const src = doc.sources.get(id);
    if (rasterAll) { loaded.set(id, null); needRaster = true; continue; }
    try {
      loaded.set(id, await PDFDocument.load(await getSourceBytes(src), { updateMetadata: false }));
    } catch (e) {
      const enc = e && (e.name === 'EncryptedPDFError' || /encrypt/i.test(e.message || ''));
      if (!allowRaster) throw new ExportError(enc ? 'ENCRYPTED' : 'UNSUPPORTED', e && e.message);
      loaded.set(id, null); needRaster = true;
    }
  }
  await tick();

  /* 2. choose build strategy: edit the original in place when possible (keeps forms, outlines, metadata) */
  const redactPage = m => m.objects.some(o => o.type === 'redact');
  const hasRedact = pages.some(redactPage);
  const inc = pages.every((p, i) => p.kind === 'pdf' && (i === 0 || p.srcIndex > pages[i - 1].srcIndex));
  const inPlace = !needRaster && !hasRedact && usedSrc.length === 1 && inc && loaded.get(usedSrc[0]) && pages.every(p => p.src === usedSrc[0]);
  let out, pdfPages;
  if (inPlace) {
    out = loaded.get(usedSrc[0]);
    const keep = new Set(pages.map(p => p.srcIndex));
    for (let i = out.getPageCount() - 1; i >= 0; i--) if (!keep.has(i)) out.removePage(i);
    pdfPages = Array.from({ length: out.getPageCount() }, (_, i) => out.getPage(i));   // getPages() can be stale after removePage
    if (pdfPages.length !== pages.length) throw new ExportError('VERIFY', 'page mapping mismatch');
  } else { out = await PDFDocument.create(); pdfPages = []; }

  const fonts = new Map(), images = new Map();
  const getFont = async o => {
    const k = `${o.font}|${o.bold ? 1 : 0}|${o.italic ? 1 : 0}`;
    if (!fonts.has(k)) fonts.set(k, await out.embedFont(StandardFonts[FONT_MAP[o.font || 'helvetica'][(o.bold ? 1 : 0) + (o.italic ? 2 : 0)]]));
    return fonts.get(k);
  };
  const getImage = async assetId => {
    if (!images.has(assetId)) {
      const a = doc.assets.get(assetId);
      images.set(assetId, a.mime === 'image/jpeg' ? await out.embedJpg(a.bytes) : await out.embedPng(a.bytes));
    }
    return images.get(assetId);
  };

  const ctx = { L, out, getFont, getImage };
  const outPages = [], outViews = [];
  for (let i = 0; i < pages.length; i++) {
    const m = pages[i];
    let page, view = m.view, rot = m.rot, raster = false;
    if (inPlace) {
      page = pdfPages[i];
      if (m.rot !== m.rot0) page.setRotation(degrees(m.rot));
    } else if (m.kind === 'pdf' && loaded.get(m.src) && !redactPage(m)) {
      [page] = await out.copyPages(loaded.get(m.src), [m.srcIndex]);
      out.addPage(page);
      if (m.rot !== m.rot0) page.setRotation(degrees(m.rot));
      removeAnnots(L, out, page, 'Widget');                          // field tree is not copied; filled values are drawn visually below
    } else if (m.kind === 'pdf') {                                  // flattened page (unreadable source, or secure redaction)
      page = out.addPage([m.w, m.h]);
      const rects = m.objects.filter(o => o.type === 'redact');
      const img = await out.embedJpg(await renderPageToJpeg(m, rasterScale, rasterQuality, rects));
      page.drawImage(img, { x: 0, y: 0, width: m.w, height: m.h });
      view = [0, 0, m.w, m.h]; rot = 0; raster = true;
    } else {
      page = out.addPage([m.w, m.h]); view = [0, 0, m.w, m.h]; rot = 0;
    }
    if (m.kind === 'pdf' && !raster && m.view0 && !sameBox(m.view, m.view0)) page.setCropBox(m.view[0], m.view[1], m.view[2] - m.view[0], m.view[3] - m.view[1]);
    if (m.kind === 'pdf' && !raster && m.linkNums && m.linkNums.length) removeAnnots(L, out, page, 'Link', new Set(m.linkNums));
    outPages.push(page); outViews.push({ view, rot, raster });

    const drawable = m.objects.filter(o => !(raster && o.type === 'redact'));
    if (drawable.length) {
      const mat = viewToUserMatrix(view, rot);
      const identityM = mat[0] === 1 && mat[1] === 0 && mat[2] === 0 && mat[3] === 1 && mat[4] === 0 && mat[5] === 0;
      if (!identityM) page.pushOperators(pushGraphicsState(), concatTransformationMatrix(...mat));
      for (const o of drawable) await drawObject(page, m, o, ctx);
      if (!identityM) page.pushOperators(popGraphicsState());
    }
    if (m.ocr && m.ocr.words && m.ocr.words.length) await drawOcr(page, m, view, rot, ctx, { pushGraphicsState, popGraphicsState, concatTransformationMatrix, setTextRenderingMode, TextRenderingMode });
    onProgress((i + 1) / pages.length * 0.9);
    if (i % 3 === 2) await tick();
  }

  /* 3. annotations (links, notes), form fields */
  const refs = outPages.map(p => p.ref);
  const idToIndex = new Map(pages.map((p, i) => [p.id, i]));
  let form = null;
  const getForm = () => (form ||= out.getForm());
  const radioGroups = new Map(), usedNames = new Set();
  if (inPlace) { try { getForm().getFields().forEach(f => usedNames.add(f.getName())); } catch { /* no AcroForm */ } }
  for (let i = 0; i < pages.length; i++) {
    const m = pages[i], { view, rot } = outViews[i], page = outPages[i];
    for (const o of m.objects) {
      if (o.type === 'link') addLink(L, out, page, m, o, view, rot, refs, idToIndex);
      else if (o.type === 'note') addNote(L, out, page, m, o, view, rot);
      else if (o.type === 'field') addField(L, out, getForm(), page, m, o, view, rot, { radioGroups, usedNames });
    }
  }
  /* existing form fields: write values (in place) or draw them visually (rebuilt document) */
  for (let i = 0; i < pages.length; i++) {
    const m = pages[i];
    if (!m.fv || !Object.keys(m.fv).length || m.kind !== 'pdf') continue;
    if (inPlace) applyFormValues(L, getForm(), m.fv);
    else if (!outViews[i].raster) await drawFormValues(L, ctx, outPages[i], m, outViews[i]);
  }
  if (flattenForms && form) { try { form.flatten(); } catch (e) { exportNotes.push('Some form fields could not be flattened.'); } }

  try { out.setProducer('PDF Editor (browser, local)'); out.setModificationDate(new Date()); if (!inPlace) out.setTitle(doc.name); } catch { /* ignore */ }
  const bytes = await out.save();
  onProgress(1);

  /* 4. validate output: re-open and compare page count */
  try {
    const check = await PDFDocument.load(bytes, { updateMetadata: false });
    if (check.getPageCount() !== pages.length) throw new Error('page count mismatch');
  } catch (e) { throw new ExportError('VERIFY', 'The exported file failed validation.'); }
  return bytes;
}

/* ---------- annotations ---------- */
function userRect(m, view, rot, o) {
  return viewRectToUserBox(view, rot, m.h, o.x, o.y, o.w, o.h);
}
function addLink(L, out, page, m, o, view, rot, refs, idToIndex) {
  const rect = userRect(m, view, rot, o);
  const base = { Type: 'Annot', Subtype: 'Link', Rect: rect, Border: [0, 0, 0], F: 4 };
  if (o.kind === 'page') {
    const idx = idToIndex.get(o.destPage);
    if (idx === undefined) { exportNotes.push('A link pointed to a page that is not part of this file and was skipped.'); return; }
    pushAnnot(L, out, page, { ...base, Dest: [refs[idx], 'Fit'] });
  } else {
    if (!isSafeUrl(o.url)) { exportNotes.push(`A link with an unsupported address was skipped: ${String(o.url).slice(0, 40)}`); return; }
    pushAnnot(L, out, page, { ...base, A: { Type: 'Action', S: 'URI', URI: L.PDFString.of(o.url.trim()) } });
  }
}
function addNote(L, out, page, m, o, view, rot) {
  const [x0, y0, x1, y1] = userRect(m, view, rot, o);
  const c = hexToRgb(o.color, L);
  pushAnnot(L, out, page, { Type: 'Annot', Subtype: 'Text', Rect: [x0, y0, x1, y1], Contents: L.PDFHexString.fromText(o.text || ''), Name: 'Comment', C: [c.red, c.green, c.blue], F: 4, Open: false, T: L.PDFHexString.fromText('Note') });
}
function addField(L, out, form, page, m, o, view, rot, st) {
  const { rgb } = L;
  const [x0, y0, x1, y1] = userRect(m, view, rot, o);
  const box = { x: x0, y: y0, width: x1 - x0, height: y1 - y0, borderWidth: 1, borderColor: rgb(0.35, 0.4, 0.5), backgroundColor: rgb(0.96, 0.97, 1) };
  let name = (o.name || 'field').replace(/[.\s]+/g, '_') || 'field';
  const isRadio = o.fkind === 'radio';
  if (!isRadio && st.usedNames.has(name)) { let k = 2; while (st.usedNames.has(`${name}_${k}`)) k++; exportNotes.push(`Field name "${name}" already existed; saved as "${name}_${k}".`); name = `${name}_${k}`; }
  try {
    if (o.fkind === 'text' || o.fkind === 'date') {
      const f = form.createTextField(name);
      if (o.value) f.setText(o.value);
      f.setFontSize(Math.min(o.size || 11, Math.max(6, box.height - 3)));
      f.addToPage(page, box);
      if (o.readonly) f.enableReadOnly(); if (o.required) f.enableRequired();
    } else if (o.fkind === 'checkbox') {
      const f = form.createCheckBox(name);
      f.addToPage(page, box);
      if (o.checked) f.check();
      if (o.readonly) f.enableReadOnly(); if (o.required) f.enableRequired();
    } else if (isRadio) {
      let g = st.radioGroups.get(name);
      if (!g) { g = form.createRadioGroup(name); st.radioGroups.set(name, g); st.usedNames.add(name); }
      g.addOptionToPage(o.exportValue || 'Yes', page, box);
      if (o.checked) g.select(o.exportValue || 'Yes');
      if (o.readonly) g.enableReadOnly();
    } else if (o.fkind === 'dropdown') {
      const f = form.createDropdown(name);
      const opts = (o.options || []).filter(Boolean);
      f.addOptions(opts.length ? opts : ['—']);
      if (o.value && opts.includes(o.value)) f.select(o.value);
      f.setFontSize(Math.min(o.size || 11, Math.max(6, box.height - 3)));
      f.addToPage(page, box);
      if (o.readonly) f.enableReadOnly();
    }
    if (!isRadio) st.usedNames.add(name);
  } catch (e) {
    exportNotes.push(`Field "${o.name}" could not be created${/encod|WinAnsi/i.test(e.message || '') ? ' (it contains characters the standard fonts cannot encode)' : ''}.`);
  }
}
function applyFormValues(L, form, fv) {
  const { PDFTextField, PDFCheckBox, PDFRadioGroup, PDFDropdown, PDFOptionList } = L;
  for (const [name, val] of Object.entries(fv)) {
    try {
      const f = form.getField(name);
      if (f instanceof PDFTextField) f.setText(String(val));
      else if (f instanceof PDFCheckBox) { if (val) f.check(); else f.uncheck(); }
      else if (f instanceof PDFRadioGroup) f.select(String(val));
      else if (f instanceof PDFDropdown || f instanceof PDFOptionList) f.select(String(val));
    } catch (e) { exportNotes.push(`Field "${name}" could not be filled${/encod|WinAnsi/i.test(e.message || '') ? ' (unsupported characters)' : ''}.`); }
  }
}
/** Rebuilt documents lose the AcroForm, so typed values are drawn onto the page as plain content. */
async function drawFormValues(L, ctx, page, m, ov) {
  const widgets = await getWidgets(m);
  const font = await ctx.getFont({ font: 'helvetica' });
  const mat = viewToUserMatrix(ov.view, ov.rot);
  page.pushOperators(L.pushGraphicsState(), L.concatTransformationMatrix(...mat));
  const H = m.h, black = L.rgb(0, 0, 0);
  for (const w of widgets) {
    const v = m.fv[w.name]; if (v === undefined) continue;
    if (w.type === 'text' || w.type === 'dropdown') {
      const text = String(v); if (!text) continue;
      let ok = true; try { font.encodeText(text.replace(/[\r\n]/g, ' ')); } catch { ok = false; }
      if (!ok) { exportNotes.push(`Field "${w.name}" contains characters that cannot be drawn and was left empty.`); continue; }
      const size = Math.max(6, Math.min(w.fontSize || w.h * 0.62, 14, w.h * 0.8));
      const lines = w.multiline ? wrapLines(text, w.w - 4, s => font.widthOfTextAtSize(s, size)) : [text];
      lines.forEach((ln, k) => page.drawText(ln, { x: w.x + 2, y: H - (w.y + (w.multiline ? size * (k + 1) * 1.15 : w.h / 2 + size * 0.34)), size, font, color: black }));
    } else if (w.type === 'checkbox' && v) {
      const p = Math.min(w.w, w.h) * 0.2;
      page.drawLine({ start: { x: w.x + p, y: H - (w.y + w.h * 0.55) }, end: { x: w.x + w.w * 0.42, y: H - (w.y + w.h - p) }, thickness: 1.4, color: black });
      page.drawLine({ start: { x: w.x + w.w * 0.42, y: H - (w.y + w.h - p) }, end: { x: w.x + w.w - p, y: H - (w.y + p) }, thickness: 1.4, color: black });
    } else if (w.type === 'radio' && v === w.buttonValue) {
      page.drawCircle({ x: w.x + w.w / 2, y: H - (w.y + w.h / 2), size: Math.min(w.w, w.h) * 0.28, color: black });
    }
  }
  page.pushOperators(L.popGraphicsState());
  void initialValue;
}

/* ---------- OCR text layer (invisible, selectable/searchable) ---------- */
async function drawOcr(page, m, view, rot, ctx, ops) {
  const font = await ctx.getFont({ font: 'helvetica' });
  const mat = viewToUserMatrix(view, rot);
  page.pushOperators(ops.pushGraphicsState(), ops.concatTransformationMatrix(...mat), ops.setTextRenderingMode(ops.TextRenderingMode.Invisible));
  for (const [x, y, w, h, t] of m.ocr.words) {
    let ok = true; try { font.encodeText(t); } catch { ok = false; }
    if (!ok || !t.trim() || w <= 0) continue;
    const natural = font.widthOfTextAtSize(t, 1) || 1;
    const size = Math.max(1, Math.min(h * 1.1, w / natural));
    page.drawText(t, { x, y: m.h - y - h * 0.8, size, font });
  }
  page.pushOperators(ops.popGraphicsState());
}

/* ---------- object drawing (everything is in y-up view space after the matrix) ---------- */
async function drawObject(page, m, o, ctx) {
  const { L } = ctx;
  const { degrees, LineCapStyle } = L;
  const H = m.h;
  const rotPt = (lx, ly) => {                                      // local (centre-relative, y-down) -> view y-up
    const cx = o.x + o.w / 2, cy = o.y + o.h / 2, t = (o.rot || 0) * Math.PI / 180, c = Math.cos(t), s = Math.sin(t);
    return { x: cx + lx * c - ly * s, y: H - (cy + lx * s + ly * c) };
  };
  const rotate = degrees(-(o.rot || 0));

  switch (o.type) {
    case 'highlight':
      page.drawRectangle({ x: o.x, y: H - o.y - o.h, width: o.w, height: o.h, color: hexToRgb(o.color, L), opacity: o.opacity, borderWidth: 0 });
      break;
    case 'cover':
      page.drawRectangle({ x: o.x, y: H - o.y - o.h, width: o.w, height: o.h, color: hexToRgb(o.color, L), borderWidth: 0 });
      break;
    case 'shape': {
      const sw = o.width, col = hexToRgb(o.stroke, L);
      const fill = o.fill ? hexToRgb(o.fill, L) : undefined;
      if (o.kind === 'ellipse') {
        page.drawEllipse({ x: o.x + o.w / 2, y: H - (o.y + o.h / 2), xScale: Math.max(0.1, o.w / 2 - sw / 2), yScale: Math.max(0.1, o.h / 2 - sw / 2), borderColor: col, borderWidth: sw, color: fill, opacity: fill ? o.opacity : undefined, borderOpacity: o.opacity });
      } else {
        page.drawRectangle({ x: o.x + sw / 2, y: H - o.y - o.h + sw / 2, width: Math.max(0.1, o.w - sw), height: Math.max(0.1, o.h - sw), borderColor: col, borderWidth: sw, color: fill, opacity: fill ? o.opacity : undefined, borderOpacity: o.opacity });
      }
      break;
    }
    case 'line': {
      const col = hexToRgb(o.color, L), [a, b] = o.pts;
      const P = p => ({ x: o.x + p[0], y: H - (o.y + p[1]) });
      const line = (p, q) => page.drawLine({ start: P(p), end: P(q), thickness: o.width, color: col, opacity: o.opacity, lineCap: LineCapStyle.Round });
      line(a, b);
      if (o.arrow) {
        const ang = Math.atan2(b[1] - a[1], b[0] - a[0]), len = Math.max(8, o.width * 4.5);
        for (const k of [1, -1]) line(b, [b[0] - len * Math.cos(ang + k * 0.45), b[1] - len * Math.sin(ang + k * 0.45)]);
      }
      break;
    }
    case 'ink': {
      const col = hexToRgb(o.color, L);
      if (o.pts.length === 1 || (o.w <= 1 && o.h <= 1 && o.pts.length < 3)) {
        page.drawCircle({ x: o.x + o.pts[0][0], y: H - (o.y + o.pts[0][1]), size: o.width / 2, color: col, opacity: o.opacity });
      } else {
        const d = 'M' + o.pts.map(p => `${+p[0].toFixed(2)} ${+p[1].toFixed(2)}`).join('L');
        page.drawSvgPath(d, { x: o.x, y: H - o.y, borderColor: col, borderWidth: o.width, borderOpacity: o.opacity, borderLineCap: LineCapStyle.Round });
      }
      break;
    }
    case 'image': case 'signature': {
      const img = await ctx.getImage(o.assetId);
      const p = rotPt(-o.w / 2, o.h / 2);
      page.drawImage(img, { x: p.x, y: p.y, width: o.w, height: o.h, rotate, opacity: o.opacity ?? 1 });
      break;
    }
    case 'text': await drawText(page, o, ctx, rotPt, rotate); break;
    default: break;                                                // link / note / field / redact are handled elsewhere
  }
}

async function drawText(page, o, ctx, rotPt, rotate) {
  if (!o.text || !o.text.trim()) return;
  const { L } = ctx;
  const font = await ctx.getFont(o);
  const opacity = o.opacity ?? 1;
  let encodable = true;
  try { font.encodeText(o.text.replace(/[\r\n\t]/g, ' ')); } catch { encodable = false; }

  if (o.bg) {
    const p = rotPt(-o.w / 2, o.h / 2);
    page.drawRectangle({ x: p.x, y: p.y, width: o.w, height: o.h, color: hexToRgb(o.bg, L), rotate, borderWidth: 0 });
  }
  if (!encodable) {                                                // e.g. Devanagari / CJK: standard fonts can't encode it -> raster
    const png = await rasterText(o);
    const img = await ctx.out.embedPng(png);
    const p = rotPt(-o.w / 2, o.h / 2);
    page.drawImage(img, { x: p.x, y: p.y, width: o.w, height: o.h, rotate, opacity });
    return;
  }
  const lines = wrapLines(o.text, o.w, s => font.widthOfTextAtSize(s, o.size));
  const lh = o.size * LINE_H, base = BASELINE[o.font || 'helvetica'] * o.size, col = hexToRgb(o.color, L);
  lines.forEach((line, i) => {
    if (!line) return;
    const lw = font.widthOfTextAtSize(line, o.size);
    const off = o.align === 'center' ? (o.w - lw) / 2 : o.align === 'right' ? o.w - lw : 0;
    const lx = -o.w / 2 + off, ly = -o.h / 2 + i * lh + base;
    const p = rotPt(lx, ly);
    page.drawText(line, { x: p.x, y: p.y, size: o.size, font, color: col, rotate, opacity });
    if (o.underline) {
      const a = rotPt(lx, ly + o.size * 0.12), b = rotPt(lx + lw, ly + o.size * 0.12);
      page.drawLine({ start: a, end: b, thickness: Math.max(0.5, o.size / 16), color: col, opacity });
    }
  });
}

/** Render a text object to a transparent PNG (used only when the text contains characters the standard PDF fonts cannot encode). */
async function rasterText(o) {
  const S = Math.min(4, Math.max(2, 3000 / Math.max(o.w, 1)));
  const c = document.createElement('canvas');
  c.width = Math.ceil(o.w * S); c.height = Math.ceil(o.h * S);
  const x = c.getContext('2d');
  x.font = `${o.italic ? 'italic ' : ''}${o.bold ? '700 ' : '400 '}${o.size * S}px ${FONT_CSS[o.font] || FONT_CSS.helvetica}`;
  const lines = wrapLines(o.text, o.w, s => x.measureText(s).width / S);
  x.fillStyle = o.color; x.textBaseline = 'alphabetic';
  const lh = o.size * LINE_H, base = BASELINE[o.font || 'helvetica'] * o.size;
  lines.forEach((line, i) => {
    if (!line) return;
    const lw = x.measureText(line).width / S;
    const off = o.align === 'center' ? (o.w - lw) / 2 : o.align === 'right' ? o.w - lw : 0;
    x.fillText(line, off * S, (i * lh + base) * S);
    if (o.underline) x.fillRect(off * S, (i * lh + base + o.size * 0.12) * S, lw * S, Math.max(1, o.size / 16 * S));
  });
  const blob = await new Promise(res => c.toBlob(res, 'image/png'));
  return new Uint8Array(await blob.arrayBuffer());
}
