// Signature dialog: draw (mouse/touch/pen), type, or upload. Produces a transparent PNG
// that is placed like an image. This is a VISUAL e-signature, not a certificate-based digital signature.
import { state } from './state.js';
import { h, showModal, toast } from './utils.js';
import { canvasToAsset, beginPlacement } from './tools-image.js';

const COLORS = ['#111111', '#1d4ed8', '#0f766e', '#b91c1c'];
const TYPE_FONTS = [
  { label: 'Script', css: '"Segoe Script", "Bradley Hand", "Comic Sans MS", cursive' },
  { label: 'Brush', css: '"Brush Script MT", "Lucida Handwriting", "Apple Chancery", cursive' },
  { label: 'Classic', css: '"Snell Roundhand", "Lucida Calligraphy", Georgia, serif' },
];

function trim(canvas, pad = 6) {
  const ctx = canvas.getContext('2d');
  const { width: W, height: H } = canvas;
  const d = ctx.getImageData(0, 0, W, H).data;
  let minX = W, minY = H, maxX = -1, maxY = -1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    if (d[(y * W + x) * 4 + 3] > 8) { if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y; }
  }
  if (maxX < 0) return null;
  const w = maxX - minX + 1 + pad * 2, hh = maxY - minY + 1 + pad * 2;
  const out = document.createElement('canvas'); out.width = w; out.height = hh;
  out.getContext('2d').drawImage(canvas, minX, minY, w - pad * 2, hh - pad * 2, pad, pad, w - pad * 2, hh - pad * 2);
  return out;
}

function makeSwatches(onPick) {
  let cur = COLORS[0];
  const wrap = h('div', { class: 'swatches', role: 'radiogroup', 'aria-label': 'Ink colour' });
  COLORS.forEach(c => {
    const b = h('button', { type: 'button', style: { background: c }, 'aria-label': `Ink ${c}`, class: c === cur ? 'on' : '' });
    b.addEventListener('click', () => { cur = c; [...wrap.children].forEach(x => x.classList.toggle('on', x === b)); onPick(c); });
    wrap.append(b);
  });
  return wrap;
}

export function openSignatureDialog() {
  let tab = 'draw', color = COLORS[0], hasInk = false;
  const dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 3));

  /* --- draw tab --- */
  const pad = h('canvas', { class: 'sig-pad', 'aria-label': 'Draw your signature here' });
  const pctx = () => pad.getContext('2d');
  function sizePad() { pad.width = Math.max(10, pad.clientWidth * dpr); pad.height = 180 * dpr; const c = pctx(); c.lineCap = 'round'; c.lineJoin = 'round'; c.lineWidth = 2.6 * dpr; }
  let drawing = false, last = null;
  let prevMid = null;
  pad.addEventListener('pointerdown', e => { e.preventDefault(); pad.setPointerCapture(e.pointerId); drawing = true; last = pt(e); prevMid = last; const c = pctx(); c.strokeStyle = color; c.beginPath(); c.moveTo(...last); c.lineTo(last[0] + 0.01, last[1]); c.stroke(); hasInk = true; });
  pad.addEventListener('pointermove', e => {
    if (!drawing) return;
    const p = pt(e), c = pctx(), mid = [(last[0] + p[0]) / 2, (last[1] + p[1]) / 2];
    c.strokeStyle = color; c.beginPath(); c.moveTo(...prevMid); c.quadraticCurveTo(last[0], last[1], mid[0], mid[1]); c.stroke();   // continuous smoothed stroke
    prevMid = mid; last = p;
  });
  const end = () => { if (drawing && last && prevMid) { const c = pctx(); c.strokeStyle = color; c.beginPath(); c.moveTo(...prevMid); c.lineTo(...last); c.stroke(); } drawing = false; };
  pad.addEventListener('pointerup', end); pad.addEventListener('pointercancel', end);
  const pt = e => { const r = pad.getBoundingClientRect(); return [(e.clientX - r.left) * pad.width / r.width, (e.clientY - r.top) * pad.height / r.height]; };
  const clearBtn = h('button', { type: 'button', class: 'btn', onclick: () => { sizePad(); hasInk = false; } }, 'Clear');
  const drawPane = h('div', {}, pad, h('div', { class: 'prow' }, makeSwatches(c => { color = c; }), h('span', { style: { flex: 1 } }), clearBtn));

  /* --- type tab --- */
  const nameIn = h('input', { type: 'text', placeholder: 'Type your name', 'aria-label': 'Typed signature text', maxlength: '60' });
  const fontSel = h('select', { 'aria-label': 'Signature style' }, TYPE_FONTS.map((f, i) => h('option', { value: i }, f.label)));
  const typePreview = h('div', { class: 'sig-preview' });
  function typeCanvas() {
    const text = nameIn.value.trim(); if (!text) return null;
    const px = 120 * dpr / 2, c = document.createElement('canvas'), x = c.getContext('2d');
    const font = `${px}px ${TYPE_FONTS[+fontSel.value].css}`;
    x.font = font; const w = Math.ceil(x.measureText(text).width) + 40 * dpr;
    c.width = Math.min(w, 4000); c.height = Math.ceil(px * 1.6);
    x.font = font; x.fillStyle = color; x.textBaseline = 'middle'; x.fillText(text, 20 * dpr, c.height / 2);
    return trim(c);
  }
  function refreshType() { typePreview.textContent = ''; const c = typeCanvas(); if (c) typePreview.append(c); else typePreview.append(h('span', { style: { color: '#98a2b3' } }, 'Preview')); }
  nameIn.addEventListener('input', refreshType); fontSel.addEventListener('change', refreshType);
  const typePane = h('div', {}, h('div', { class: 'field' }, nameIn), h('div', { class: 'prow' }, h('label', {}, 'Style'), fontSel, makeSwatches(c => { color = c; refreshType(); })), typePreview);
  refreshType();

  /* --- upload tab --- */
  const fileIn = h('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp', 'aria-label': 'Signature image file' });
  const removeBg = h('input', { type: 'checkbox', checked: true, id: 'sig-rmbg' });
  const upPreview = h('div', { class: 'sig-preview' }, h('span', { style: { color: '#98a2b3' } }, 'Choose an image of your signature'));
  let upSource = null;
  function upCanvas() {
    if (!upSource) return null;
    const s = Math.min(1, 1200 / Math.max(upSource.width, upSource.height));
    const c = document.createElement('canvas'); c.width = Math.round(upSource.width * s); c.height = Math.round(upSource.height * s);
    const x = c.getContext('2d'); x.drawImage(upSource, 0, 0, c.width, c.height);
    if (removeBg.checked) {
      const img = x.getImageData(0, 0, c.width, c.height), d = img.data;
      for (let i = 0; i < d.length; i += 4) { const lum = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; const a = Math.max(0, Math.min(1, (245 - lum) / 90)); d[i + 3] = Math.round(d[i + 3] * a); }
      x.putImageData(img, 0, 0);
    }
    return trim(c);
  }
  function refreshUp() { const c = upCanvas(); upPreview.textContent = ''; if (c) upPreview.append(c); }
  fileIn.addEventListener('change', async () => {
    const f = fileIn.files[0]; if (!f) return;
    try { upSource = await createImageBitmap(f, { imageOrientation: 'from-image' }); refreshUp(); } catch { toast('That image could not be read.', 'error'); }
  });
  removeBg.addEventListener('change', refreshUp);
  const upPane = h('div', {}, h('div', { class: 'field' }, fileIn), h('div', { class: 'prow' }, removeBg, h('label', { for: 'sig-rmbg', style: { flex: 1 } }, 'Make white background transparent')), upPreview);

  /* --- tabs & saved list --- */
  const panes = { draw: drawPane, type: typePane, upload: upPane };
  const tabBar = h('div', { class: 'tabs', role: 'tablist' });
  const holder = h('div');
  const tabBtns = {};
  for (const [id, label] of [['draw', 'Draw'], ['type', 'Type'], ['upload', 'Upload']]) {
    const b = h('button', { type: 'button', role: 'tab', class: id === tab ? 'on' : '' }, label);
    b.addEventListener('click', () => show(id)); tabBtns[id] = b; tabBar.append(b);
  }
  function show(id) { tab = id; holder.textContent = ''; holder.append(panes[id]); for (const k in tabBtns) tabBtns[k].classList.toggle('on', k === id); if (id === 'draw') { requestAnimationFrame(() => { if (!hasInk) sizePad(); }); } }

  const saved = h('div', { class: 'saved-sigs' });
  const savedIds = (state.doc.sigAssets || []).filter(id => state.doc.assets.has(id));
  if (savedIds.length) saved.append(h('div', { class: 'note', style: { width: '100%' } }, 'Used in this session (click to reuse):'));
  savedIds.forEach(id => { const a = state.doc.assets.get(id); const b = h('button', { type: 'button', 'aria-label': 'Reuse signature' }, h('img', { src: a.url, alt: 'Saved signature' })); b.addEventListener('click', () => { m.close(); beginPlacement('signature', id); }); saved.append(b); });

  const m = showModal({
    title: 'Add your signature', wide: true,
    content: [
      h('div', { class: 'callout info' }, 'This adds a visual (electronic) signature image to your PDF. It is not a certificate-based digital signature and does not cryptographically sign or lock the document.'),
      tabBar, holder, saved,
    ],
    actions: [{ label: 'Cancel', value: false }, {
      label: 'Use signature', value: true, primary: true, keepOpen: true,
      onClick: async close => {
        let c = null;
        if (tab === 'draw') c = hasInk ? trim(pad) : null; else if (tab === 'type') c = typeCanvas(); else c = upCanvas();
        if (!c) { toast(tab === 'draw' ? 'Please draw your signature first.' : tab === 'type' ? 'Please type your name first.' : 'Please choose an image first.', 'error'); return false; }
        const id = await canvasToAsset(c, 'image/png');
        (state.doc.sigAssets ||= []).push(id);
        close(true); beginPlacement('signature', id);
      },
    }],
  });
  show('draw');
}
