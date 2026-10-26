// Central editor state + tiny event bus. Heavy binary data (PDF sources, image assets)
// lives outside the history snapshots; only `doc.pages` (small JSON) is versioned.
const listeners = new Map();
export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
  return () => listeners.get(evt).delete(fn);
}
export function emit(evt, data) { (listeners.get(evt) || []).forEach(fn => { try { fn(data); } catch (e) { console.error('listener error:', evt, e && e.name); } }); }

export const state = {
  doc: null,            // { name, size, pages:[], sources:Map, assets:Map }
  tool: 'select',       // select | text | draw | highlight | underline | strike | rect | ellipse | line | arrow | note | eraser | whiteout | blackout | redact | link | edittext | f-text …
  pending: null,        // placement of an image/signature: {type, assetId, w, h}
  zoom: 1.3333,         // CSS px per PDF point
  fitMode: 'width',     // 'width' | 'page' | null
  selection: null,      // selected object id
  currentPage: 0,
  selectedPages: new Set(), // page ids chosen in the thumbnail strip
  overlays: new Map(),  // pageId -> overlay element
  dirty: false,
  props: {
    text: { font: 'helvetica', size: 16, bold: false, italic: false, underline: false, color: '#000000', align: 'left', bg: null, opacity: 1 },
    draw: { color: '#d62839', width: 3, opacity: 1 },
    highlight: { color: '#ffe600', opacity: 0.4 },
    image: { opacity: 1 },
    shape: { stroke: '#d62839', fill: '#fff3b0', hasFill: false, width: 2, opacity: 1 },
    cover: { whiteout: '#ffffff', blackout: '#000000' },
    note: { color: '#ffd43b' },
  },
};

export function findObject(id) {
  if (!id || !state.doc) return null;
  for (let i = 0; i < state.doc.pages.length; i++) {
    const page = state.doc.pages[i];
    const obj = page.objects.find(o => o.id === id);
    if (obj) return { obj, page, pageIndex: i };
  }
  return null;
}
export const currentPage = () => state.doc?.pages[state.currentPage] || null;

export function addAsset({ bytes, mime, w, h }) {
  const id = 'a' + Math.random().toString(36).slice(2, 9);
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  state.doc.assets.set(id, { id, bytes, mime, w, h, url });
  return id;
}

export function setTool(t) {
  if (state.tool === t && !state.pending) return;
  state.tool = t;
  if (state.pending) { state.pending = null; emit('pending', null); }
  emit('tool', t);
}
export function setPending(p) { state.pending = p; if (p) state.tool = 'select'; emit('pending', p); emit('tool', state.tool); }
