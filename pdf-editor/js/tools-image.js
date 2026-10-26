// Image insertion: file -> normalized asset -> click-to-place (or drag & drop onto a page).
import { state, addAsset, setPending } from './state.js';
import { addObject, newImageObject } from './objects.js';
import { toast } from './utils.js';

const MAX_DIM = 2400, MAX_FILE = 30 * 1024 * 1024;

export function pickImage() { const i = document.getElementById('image-input'); i.value = ''; i.click(); }

/** Decode any browser-supported image, honour EXIF rotation, cap its size and store as JPEG/PNG. */
export async function fileToAsset(file) {
  if (!/^image\/(png|jpe?g|webp|gif|bmp)$/i.test(file.type)) throw new Error('Unsupported image type. Please use JPG, PNG or WebP.');
  if (file.size > MAX_FILE) throw new Error('That image is larger than 30 MB. Please use a smaller image.');
  const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const s = Math.min(1, MAX_DIM / Math.max(bmp.width, bmp.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bmp.width * s)); canvas.height = Math.max(1, Math.round(bmp.height * s));
  canvas.getContext('2d').drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close && bmp.close();
  return canvasToAsset(canvas, /jpe?g/i.test(file.type) ? 'image/jpeg' : 'image/png');
}

export async function canvasToAsset(canvas, mime = 'image/png') {
  const blob = await new Promise(res => canvas.toBlob(res, mime, 0.92));
  if (!blob) throw new Error('Could not process the image.');
  const bytes = new Uint8Array(await blob.arrayBuffer());
  return addAsset({ bytes, mime, w: canvas.width, h: canvas.height });
}

let placeToast = null;
export function beginPlacement(type, assetId) {
  const a = state.doc.assets.get(assetId);
  setPending({ type, assetId, w: a.w, h: a.h });
  placeToast?.remove();
  placeToast = toast('Click on the page where you want to place it. Press Esc to cancel.', 'info', 4500);
}

function sizeFor(type, a, page) {
  let w = type === 'signature' ? 160 : Math.min(240, page.w * 0.45);
  let h = w * a.h / a.w;
  const maxH = page.h * 0.6;
  if (h > maxH) { h = maxH; w = h * a.w / a.h; }
  return [w, h];
}

export function placeAsset(pageId, type, assetId, cx, cy) {
  const page = state.doc.pages.find(p => p.id === pageId);
  const a = state.doc.assets.get(assetId);
  const [w, h] = sizeFor(type, a, page);
  const x = Math.min(Math.max(0, cx - w / 2), Math.max(0, page.w - w));
  const y = Math.min(Math.max(0, cy - h / 2), Math.max(0, page.h - h));
  addObject(pageId, newImageObject(type, assetId, x, y, w, h), type === 'signature' ? 'Add signature' : 'Add image');
}

export function placePending(pageId, x, y) {
  const p = state.pending; if (!p) return;
  placeToast?.remove();
  setPending(null);
  placeAsset(pageId, p.type, p.assetId, x, y);
}

export async function handleImageFile(file) {
  try {
    const id = await fileToAsset(file);
    beginPlacement('image', id);
  } catch (e) { toast(e.message || 'This image could not be used.', 'error', 6000); }
}

export async function dropImageOnPage(file, pageId, x, y) {
  try { const id = await fileToAsset(file); placeAsset(pageId, 'image', id, x, y); }
  catch (e) { toast(e.message || 'This image could not be used.', 'error', 6000); }
}
