// Shared helpers: DOM builder, toasts, modal dialogs, formatting, friendly errors.
export const PT_PX = 96 / 72; // CSS pixels per PDF point at "100%" zoom
export const uid = (p = 'o') => p + Math.random().toString(36).slice(2, 9) + Date.now().toString(36).slice(-3);
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const $ = (sel, root = document) => root.querySelector(sel);

export function formatBytes(n) {
  if (!Number.isFinite(n)) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(n < 10 ? 2 : 1)} ${units[i]}`;
}

export function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.cancel = () => clearTimeout(t);
  return d;
}

/** Tiny DOM builder. Never uses innerHTML, so file names / text can't inject markup. */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
export function icon(name, cls = 'ic') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

export function toast(msg, type = 'info', ms = 3800) {
  const root = $('#toasts');
  const el = h('div', { class: `toast ${type}`, role: type === 'error' ? 'alert' : 'status' }, msg);
  root.append(el);
  while (root.children.length > 3) root.firstChild.remove();
  setTimeout(() => el.remove(), ms);
  return el;
}

/**
 * Modal dialog. Returns { el, body, close(value), promise }.
 * actions: [{label, value, primary, danger, keepOpen, onClick}]
 */
export function showModal({ title, content, actions = [{ label: 'OK', value: true, primary: true }], wide = false, dismissable = true }) {
  const root = $('#modal-root');
  const prevFocus = document.activeElement;
  let resolve;
  const promise = new Promise(r => (resolve = r));
  const body = h('div', { class: 'body' }, content);
  const footer = h('footer');
  const dlg = h('div', { class: `modal${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title }, h('header', {}, title), body, footer);
  const back = h('div', { class: 'modal-back' }, dlg);
  let closed = false;
  const close = (value = null) => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    back.remove();
    try { prevFocus && prevFocus.focus && prevFocus.focus(); } catch { /* ignore */ }
    resolve(value);
  };
  const buttons = actions.map(a => {
    const b = h('button', { type: 'button', class: `btn${a.primary ? ' btn-primary' : ''}${a.danger ? ' btn-danger' : ''}` }, a.label);
    b.addEventListener('click', async () => {
      if (a.onClick) { const r = await a.onClick(close); if (r === false) return; }
      if (!a.keepOpen) close(a.value);
    });
    b.dataset.value = String(a.value);
    footer.append(b);
    return b;
  });
  function onKey(e) {
    if (e.key === 'Escape' && dismissable) { e.stopPropagation(); e.preventDefault(); close(null); }
    else if (e.key === 'Tab') { // simple focus trap
      const f = [...dlg.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(x => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  document.addEventListener('keydown', onKey, true);
  back.addEventListener('mousedown', e => { if (e.target === back && dismissable) close(null); });
  root.append(back);
  const focusEl = dlg.querySelector('input:not([type=hidden]), select') || buttons.find((_, i) => actions[i].primary) || buttons[0];
  focusEl && focusEl.focus();
  return { el: dlg, body, footer, buttons, close, promise };
}

export function confirmDialog(title, message, okLabel = 'OK', danger = false) {
  return showModal({
    title, content: h('p', {}, message),
    actions: [{ label: 'Cancel', value: false }, { label: okLabel, value: true, primary: !danger, danger }],
  }).promise.then(v => v === true);
}

export function alertDialog(title, message) {
  return showModal({ title, content: h('p', {}, message), actions: [{ label: 'OK', value: true, primary: true }] }).promise;
}

/** Turn any thrown error into a friendly sentence. Never includes file contents. */
export function friendlyError(e, fallback) {
  const name = (e && (e.name || e.constructor?.name)) || '';
  const msg = String((e && e.message) || '');
  if (name === 'PasswordException') return 'This PDF is password-protected and the password was not accepted.';
  if (name === 'InvalidPDFException' || /invalid pdf|not a pdf|bad xref|missing pdf header/i.test(msg)) return 'This file looks damaged or is not a valid PDF, so it cannot be opened.';
  if (name === 'MissingPDFException') return 'The PDF file could not be found.';
  if (name === 'EncryptedPDFError' || /encrypted/i.test(msg)) return 'This PDF is encrypted, so the browser editor cannot modify its structure. You can still view and annotate it.';
  if (e instanceof RangeError || /memory|allocation|too large/i.test(msg)) return 'This browser ran out of memory while handling the PDF. Try a smaller file or close other tabs.';
  return fallback || 'Something went wrong while processing this PDF. Your original file has not been changed.';
}

/* ---------- small dialog helpers ---------- */
export const field = (label, ...kids) => h('div', { class: 'field' }, h('label', {}, label), ...kids);
export const selectOf = (opts, value, attrs = {}) => h('select', attrs, opts.map(([v, l]) => h('option', { value: v, selected: v === value }, l)));
export const numInput = (value, attrs = {}) => h('input', { type: 'number', value, ...attrs });

/** Non-dismissable progress dialog with a Cancel button. */
export function progressModal(title, { cancellable = true } = {}) {
  const bar = h('i'), msg = h('p', {}, 'Working…');
  let cancelled = false;
  const m = showModal({
    title, content: [msg, h('div', { class: 'progress', role: 'progressbar' }, bar)], dismissable: false,
    actions: cancellable ? [{ label: 'Cancel', value: false, onClick: () => { cancelled = true; } }] : [],
  });
  return { set(p, text) { bar.style.width = `${Math.round(p * 100)}%`; if (text) msg.textContent = text; }, get cancelled() { return cancelled; }, close: () => m.close(true) };
}

/** Save arbitrary bytes through the browser's download mechanism. */
export function downloadFile(bytes, filename, mime = 'application/octet-stream') {
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  const a = h('a', { href: url, download: filename });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 20000);
}
export const safeFileName = n => (n || 'document').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').replace(/\.(pdf|zip|txt)$/i, '').trim() || 'document';
