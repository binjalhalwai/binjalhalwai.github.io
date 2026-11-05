/* =====================================================================
   PDF Tools - shared helpers  (window.PT)
   Loaded on every page BEFORE the tool's own script.

   Everything here is plain browser JavaScript. Nothing is uploaded:
   files are read with the File API, processed in memory and offered
   back as a download. Nothing is stored (no localStorage for files).

   Main pieces:
     PT.h()            tiny DOM builder           PT.notify()   toast messages
     PT.busy()/run()   progress overlay           PT.dialog()   modal dialogs
     PT.dropzone()     drag & drop file picker    PT.result()   "done" panel + downloads
     PT.openPdf()      open a PDF with pdf.js (asks for the password if needed)
     PT.loadLib()      open the same PDF with pdf-lib (for editing)
     PT.Thumbs         lazy page thumbnails       PT.dragSort() drag-and-drop reordering
     PT.zip()          store-only ZIP writer      PT.parseRanges() "1-3,5,8-last"
     PT.readImage()/embedImage()  JPG/PNG/WebP/GIF/BMP -> pdf-lib image (EXIF aware)
   ===================================================================== */
(function () {
  'use strict';
  var PT = (window.PT = {});

  /* ---------- where the assets folder is (works at any depth / sub-path) ---------- */
  var ASSETS = (function () {
    var s = document.querySelector('script[src*="assets/js/common.js"]');
    var src = s ? s.getAttribute('src') : 'assets/js/common.js';
    var rel = src.replace(/js\/common\.js.*$/, '');           // e.g. "../assets/"
    try { return new URL(rel, location.href).href; } catch (e) { return ''; }                  // absolute URL
  })();
  PT.ASSETS = ASSETS;
  PT.MAX_MB = 200;                                            // per-file limit

  /* ---------- DOM helper ---------- */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === 'class') el.className = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
        else if (k === 'html') el.innerHTML = v;              // only ever used with our own static strings
        else if (k === 'text') el.textContent = v;
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected') el[k] = v;
        else el.setAttribute(k, v === true ? '' : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
  }
  PT.h = h;
  PT.$ = function (sel, root) { return (root || document).querySelector(sel); };
  PT.mount = function () { var m = document.getElementById('tool'); m.textContent = ''; return m; };
  PT.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  PT.tick = function () { return new Promise(function (r) { setTimeout(r, 0); }); };   // let the UI repaint
  PT.sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  /* ---------- small utilities ---------- */
  PT.fmtBytes = function (n) {
    if (!isFinite(n)) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1048576).toFixed(n < 10485760 ? 2 : 1) + ' MB';
  };
  PT.baseName = function (name) { return String(name || 'file').replace(/\.[^.\\/]+$/, ''); };
  PT.outName = function (name, suffix, ext) { return PT.baseName(name) + (suffix ? '-' + suffix : '') + '.' + (ext || 'pdf'); };
  PT.hexToRgb = function (hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
    var n = m ? parseInt(m[1], 16) : 0;
    return { r: ((n >> 16) & 255) / 255, g: ((n >> 8) & 255) / 255, b: (n & 255) / 255 };
  };
  PT.clamp = function (v, a, b) { return Math.min(b, Math.max(a, v)); };
  PT.uid = (function () { var i = 0; return function () { return 'i' + (++i); }; })();
  PT.CANCEL = function () { var e = new Error('Cancelled'); e.cancelled = true; return e; };

  /* ---------- theme toggle (same storage key as the main site) ---------- */
  (function () {
    var b = document.getElementById('bTheme');
    if (!b) return;
    b.addEventListener('click', function () {
      var el = document.documentElement;
      var next = el.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      el.setAttribute('data-theme', next);
      try { localStorage.setItem('aiph_theme', next); } catch (e) { /* private mode: ignore */ }
    });
  })();

  /* ---------- toasts ---------- */
  PT.notify = function (msg, type, ms) {
    var box = document.getElementById('toasts');
    if (!box) { box = h('div', { id: 'toasts', 'aria-live': 'polite' }); document.body.appendChild(box); }
    var t = h('div', { class: 'toast ' + (type || ''), role: type === 'err' ? 'alert' : 'status' }, msg);
    box.appendChild(t);
    var life = ms || (type === 'err' ? 7000 : 3800);
    var kill = function () { if (t.parentNode) t.parentNode.removeChild(t); };
    t.addEventListener('click', kill);
    setTimeout(kill, life);
  };

  /* ---------- friendly error text ---------- */
  PT.friendlyError = function (e) {
    var m = (e && e.message) ? String(e.message) : String(e || 'Unknown error');
    if (e && e.cancelled) return '';
    if (e && e.friendly) return m;
    if (/PasswordException|No password given|Incorrect password|encrypted/i.test((e && e.name) + ' ' + m))
      return 'This PDF is password-protected. Enter the correct password, or use the Unlock PDF tool.';
    if (/Invalid PDF|No PDF header|Failed to parse|Unexpected token|Invalid XRef|Missing PDF|Stream must have data|Expected instance of|bad XRef|FormatError/i.test(m))
      return 'This file does not look like a valid PDF, or it is damaged. Try the Repair PDF tool.';
    if (e instanceof RangeError || /allocation|out of memory|Invalid typed array length|Array buffer allocation/i.test(m))
      return 'Your device ran out of memory for this file. Try a smaller file, fewer pages, or a computer instead of a phone.';
    return m;
  };

  /* ---------- progress overlay ----------
     var b = PT.busy('Merging...', {cancel:true});
     b.set(0.4, 'File 2 of 5');   b.check();   b.close();       */
  PT.busy = function (title, opts) {
    opts = opts || {};
    var bar = h('i'), pb = h('div', { class: 'pbar' }, bar), txt = h('div', { class: 'ptxt', 'aria-live': 'polite' }, 'Working...');
    var st = { cancelled: false };
    var cancelBtn = opts.cancel ? h('button', { class: 'btn ghost sm', type: 'button', onclick: function () { st.cancelled = true; cancelBtn.disabled = true; txt.textContent = 'Cancelling...'; } }, 'Cancel') : null;
    var dlg = h('div', { class: 'dlg', role: 'alertdialog', 'aria-label': title || 'Working' },
      h('h3', null, title || 'Working...'), pb, txt, cancelBtn ? h('div', { class: 'row end' }, cancelBtn) : null);
    var ov = h('div', { class: 'ov' }, dlg);
    document.body.appendChild(ov);
    pb.classList.add('ind');
    return {
      get cancelled() { return st.cancelled; },
      set: function (frac, text) {
        if (frac != null) { pb.classList.remove('ind'); bar.style.width = Math.round(PT.clamp(frac, 0, 1) * 100) + '%'; }
        if (text != null) txt.textContent = text;
      },
      check: function () { if (st.cancelled) throw PT.CANCEL(); },
      close: function () { if (ov.parentNode) ov.parentNode.removeChild(ov); }
    };
  };
  /* run(title, fn(b)) : shows the overlay, catches errors, always closes. Returns fn's value (or undefined on error). */
  PT.run = async function (title, fn, opts) {
    var b = PT.busy(title, opts);
    try { await PT.tick(); return await fn(b); }
    catch (e) {
      console.error(e);
      var m = PT.friendlyError(e);
      if (m) PT.notify(m, 'err');
      else PT.notify('Cancelled.', 'warn', 2000);
    } finally { b.close(); }
  };

  /* ---------- dialogs ---------- */
  PT.dialog = function (o) {
    return new Promise(function (resolve) {
      var prev = document.activeElement;
      var done = function (v) { if (ov.parentNode) ov.parentNode.removeChild(ov); document.removeEventListener('keydown', key, true); if (prev && prev.focus) prev.focus(); resolve(v); };
      var btns = (o.buttons || [{ label: 'OK', value: true, primary: true }]).map(function (b) {
        return h('button', { type: 'button', class: 'btn ' + (b.primary ? '' : 'ghost') + (b.danger ? ' danger' : ''), onclick: function () { done(typeof b.value === 'function' ? b.value() : b.value); } }, b.label);
      });
      var dlg = h('form', { class: 'dlg' + (o.wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', onsubmit: function (e) { e.preventDefault(); var p = (o.buttons || [{ value: true, primary: true }]).filter(function (b) { return b.primary; })[0]; done(p ? (typeof p.value === 'function' ? p.value() : p.value) : true); } },
        o.title ? h('h3', null, o.title) : null, o.body || null, h('div', { class: 'row end', style: { marginTop: '14px' } }, btns));
      var ov = h('div', { class: 'ov' }, dlg);
      var key = function (e) { if (e.key === 'Escape') { e.stopPropagation(); done(o.cancelValue === undefined ? null : o.cancelValue); } };
      document.addEventListener('keydown', key, true);
      document.body.appendChild(ov);
      var f = dlg.querySelector('input,select,textarea') || dlg.querySelector('button.btn:not(.ghost)');
      if (f) setTimeout(function () { f.focus(); }, 20);
    });
  };
  PT.confirm = function (title, text, okLabel) {
    return PT.dialog({ title: title, body: h('p', null, text), cancelValue: false, buttons: [{ label: 'Cancel', value: false }, { label: okLabel || 'OK', value: true, primary: true }] });
  };
  /* Ask for a PDF password. Resolves to the string or null when cancelled. */
  PT.askPassword = function (fileName, wrong, what) {
    var inp = h('input', { type: 'password', class: 'in', autocomplete: 'off', 'aria-label': 'Password', style: { marginTop: '8px' } });
    var show = h('input', { type: 'checkbox', onchange: function () { inp.type = show.checked ? 'text' : 'password'; } });
    return PT.dialog({
      title: 'Password required',
      cancelValue: null,
      body: h('div', null,
        h('p', { class: 'note', style: { margin: 0 } }, '"' + fileName + '" ' + (what || 'is protected. Type its password to open it here. The password stays in your browser.')),
        wrong ? h('p', { class: 'msg err' }, 'That password is not correct. Try again.') : null,
        inp, h('label', { class: 'chk', style: { marginTop: '8px' } }, show, 'Show password')),
      buttons: [{ label: 'Cancel', value: null }, { label: 'Open', value: function () { return inp.value; }, primary: true }]
    });
  };

  /* ---------- file validation + drop zone ---------- */
  var IMG_EXT = /\.(jpe?g|png|webp|gif|bmp)$/i;
  PT.isPdf = function (f) { return /\.pdf$/i.test(f.name) || f.type === 'application/pdf'; };
  PT.isImage = function (f) { return IMG_EXT.test(f.name) || /^image\/(jpeg|png|webp|gif|bmp)$/.test(f.type); };
  /* kind: 'pdf' | 'image' | 'pdf+image'. Returns the good files and tells the user about the bad ones. */
  PT.validate = function (files, kind, maxMB) {
    var ok = [], max = (maxMB || PT.MAX_MB) * 1048576;
    Array.prototype.forEach.call(files, function (f) {
      var good = (kind === 'pdf' && PT.isPdf(f)) || (kind === 'image' && PT.isImage(f)) || (kind === 'pdf+image' && (PT.isPdf(f) || PT.isImage(f)));
      if (!good) {
        var why = /\.(heic|heif)$/i.test(f.name) ? 'is a HEIC photo, which browsers cannot read. Convert it to JPG first.' :
          /\.docx?$/i.test(f.name) ? 'is a Word file and cannot be used here. See the Word to PDF page.' : 'is not a supported file type.';
        PT.notify('"' + f.name + '" ' + why, 'err');
      } else if (f.size === 0) PT.notify('"' + f.name + '" is empty.', 'err');
      else if (f.size > max) PT.notify('"' + f.name + '" is ' + PT.fmtBytes(f.size) + ' - larger than the ' + (maxMB || PT.MAX_MB) + ' MB limit.', 'err');
      else ok.push(f);
    });
    return ok;
  };
  /* opts: kind, multiple, compact, label, hint, onFiles(files[]) */
  PT.dropzone = function (o) {
    var accept = o.kind === 'pdf' ? '.pdf,application/pdf' : o.kind === 'image' ? '.jpg,.jpeg,.png,.webp,.gif,.bmp,image/*' : '.pdf,.jpg,.jpeg,.png,.webp,.gif,.bmp,application/pdf,image/*';
    var input = h('input', { type: 'file', accept: accept, multiple: !!o.multiple, class: 'sr', tabindex: '-1', 'aria-hidden': 'true' });
    var take = function (list) {
      var ok = PT.validate(list, o.kind, o.maxMB);
      if (!o.multiple && ok.length > 1) { ok = ok.slice(0, 1); PT.notify('Only one file is used here - took the first.', 'warn'); }
      if (ok.length) o.onFiles(ok);
    };
    var btn = h('button', { type: 'button', class: 'btn pick', onclick: function () { input.click(); } }, o.label || ('Select PDF file' + (o.multiple ? 's' : '')));
    var zone = h('div', { class: 'drop' + (o.compact ? ' compact' : ''), role: 'group', 'aria-label': 'File drop area' },
      input, btn, h('p', null, o.hint || ('or drop ' + (o.multiple ? 'files' : 'a file') + ' here. Files stay on your device. Max ' + PT.MAX_MB + ' MB each.')));
    input.addEventListener('change', function () { var l = Array.prototype.slice.call(input.files); input.value = ''; take(l); });
    var over = function (e) { e.preventDefault(); zone.classList.add('over'); };
    zone.addEventListener('dragenter', over); zone.addEventListener('dragover', over);
    zone.addEventListener('dragleave', function (e) { if (!zone.contains(e.relatedTarget)) zone.classList.remove('over'); });
    zone.addEventListener('drop', function (e) { e.preventDefault(); zone.classList.remove('over'); if (e.dataTransfer && e.dataTransfer.files.length) take(Array.prototype.slice.call(e.dataTransfer.files)); });
    zone.pick = function () { input.click(); };
    return zone;
  };
  // A missed drop must not make the browser navigate away to the dropped file.
  ['dragover', 'drop'].forEach(function (ev) { window.addEventListener(ev, function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) e.preventDefault(); }); });

  PT.readBytes = async function (file) { return new Uint8Array(await file.arrayBuffer()); };

  /* ---------- downloads + result panel ---------- */
  PT.download = function (data, name, mime) {
    var blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' });
    var url = URL.createObjectURL(blob);
    var a = h('a', { href: url, download: name, style: { display: 'none' } });
    document.body.appendChild(a); a.click();
    setTimeout(function () { if (a.parentNode) a.parentNode.removeChild(a); URL.revokeObjectURL(url); }, 60000);
  };
  PT.pdfBlob = function (bytes) { return new Blob([bytes], { type: 'application/pdf' }); };
  /* o: {title, files:[{name, blob}], stats:[{k,v}], note, onReset, resetLabel}
     Replaces/creates a panel in `host`. Returns the element. */
  PT.result = function (host, o) {
    var old = host.querySelector(':scope > .result'); if (old) old.remove();
    var files = o.files || [];
    var el = h('div', { class: 'result', role: 'region', 'aria-label': 'Result' },
      h('h3', null, o.title || 'Done!'),
      o.stats ? h('div', { class: 'statrow' }, o.stats.map(function (s) { return h('div', { class: 'stat' }, h('b', null, s.v), h('span', null, s.k)); })) : null,
      o.note ? h('p', { class: 'note' }, o.note) : null,
      files.slice(0, 40).map(function (f) {
        return h('div', { class: 'rf' }, h('span', { class: 'nm' }, f.name), h('span', { class: 'sz' }, PT.fmtBytes(f.blob.size)),
          h('button', { type: 'button', class: 'btn sm', onclick: function () { PT.download(f.blob, f.name); } }, 'Download'));
      }),
      files.length > 40 ? h('p', { class: 'note' }, '...and ' + (files.length - 40) + ' more files (use the ZIP).') : null,
      h('div', { class: 'row', style: { marginTop: '10px' } },
        files.length > 1 ? h('button', { type: 'button', class: 'btn big', onclick: async function (ev) {
          var btn = ev.currentTarget; btn.disabled = true;
          try {
            var items = [];
            for (var i = 0; i < files.length; i++) items.push({ name: files[i].name, data: new Uint8Array(await files[i].blob.arrayBuffer()) });
            PT.download(PT.zip(items), o.zipName || 'pdf-tools-files.zip', 'application/zip');
          } catch (e) { PT.notify(PT.friendlyError(e), 'err'); } finally { btn.disabled = false; }
        } }, 'Download all as ZIP') : null,
        files.length === 1 ? h('button', { type: 'button', class: 'btn big', onclick: function () { PT.download(files[0].blob, files[0].name); } }, 'Download ' + (/\.pdf$/i.test(files[0].name) ? 'PDF' : 'file')) : null,
        o.onReset ? h('button', { type: 'button', class: 'btn ghost', onclick: o.onReset }, o.resetLabel || 'Start over') : null));
    host.appendChild(el);
    setTimeout(function () { el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, 30);
    return el;
  };

  /* ---------- store-only ZIP writer ---------- */
  var CRC = (function () { var t = new Uint32Array(256); for (var n = 0; n < 256; n++) { var c = n; for (var k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
  function crc32(u8) { var c = 0xFFFFFFFF; for (var i = 0; i < u8.length; i++) c = CRC[(c ^ u8[i]) & 255] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }
  /* files: [{name, data:Uint8Array}] -> Blob (a normal .zip, no compression; images/PDFs are already compressed) */
  PT.zip = function (files) {
    var enc = new TextEncoder(), parts = [], central = [], offset = 0, now = new Date();
    var dt = ((now.getFullYear() - 1980) << 9 | (now.getMonth() + 1) << 5 | now.getDate()) & 0xFFFF;
    var tm = (now.getHours() << 11 | now.getMinutes() << 5 | (now.getSeconds() >> 1)) & 0xFFFF;
    files.forEach(function (f) {
      var nm = enc.encode(f.name), crc = crc32(f.data), size = f.data.length;
      var lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034b50, true); lh.setUint16(4, 20, true); lh.setUint16(6, 0x0800, true); lh.setUint16(8, 0, true);
      lh.setUint16(10, tm, true); lh.setUint16(12, dt, true); lh.setUint32(14, crc, true); lh.setUint32(18, size, true); lh.setUint32(22, size, true);
      lh.setUint16(26, nm.length, true); lh.setUint16(28, 0, true);
      parts.push(lh.buffer, nm, f.data);
      var ch = new DataView(new ArrayBuffer(46));
      ch.setUint32(0, 0x02014b50, true); ch.setUint16(4, 20, true); ch.setUint16(6, 20, true); ch.setUint16(8, 0x0800, true); ch.setUint16(10, 0, true);
      ch.setUint16(12, tm, true); ch.setUint16(14, dt, true); ch.setUint32(16, crc, true); ch.setUint32(20, size, true); ch.setUint32(24, size, true);
      ch.setUint16(28, nm.length, true); ch.setUint32(42, offset, true);
      central.push(ch.buffer, nm);
      offset += 30 + nm.length + size;
    });
    var csize = 0; central.forEach(function (p) { csize += p.byteLength; });
    var end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
    end.setUint32(12, csize, true); end.setUint32(16, offset, true);
    return new Blob(parts.concat(central, [end.buffer]), { type: 'application/zip' });
  };

  /* ---------- page ranges: "1-3, 5, 8-last", "odd", "even", "all" ----------
     parseRanges(str, n)       -> flat array of 0-based page indexes (order kept, duplicates removed)
     parseRangeGroups(str, n)  -> array of arrays, one per comma-separated part
     Throws an Error with a friendly message (e.friendly = true).  */
  function rangeErr(msg) { var e = new Error(msg); e.friendly = true; return e; }
  PT.parseRangeGroups = function (str, n) {
    var s = String(str || '').toLowerCase().replace(/[–—]/g, '-').replace(/;/g, ',').trim();
    if (!s) throw rangeErr('Type the pages you want, for example 1-3, 5, 8-last.');
    var out = [];
    s.split(',').forEach(function (part) {
      part = part.replace(/\s+/g, '');
      if (!part) return;
      var g = [], i;
      if (part === 'all') { for (i = 0; i < n; i++) g.push(i); }
      else if (part === 'odd') { for (i = 0; i < n; i += 2) g.push(i); }
      else if (part === 'even') { for (i = 1; i < n; i += 2) g.push(i); }
      else {
        var m = /^(\d+|last)(?:-(\d+|last|))?$/.exec(part);
        if (!m) throw rangeErr('"' + part + '" is not a valid page or range. Use numbers like 3 or 2-6.');
        var a = m[1] === 'last' ? n : parseInt(m[1], 10);
        var b = m[2] === undefined ? a : (m[2] === '' || m[2] === 'last') ? n : parseInt(m[2], 10);
        if (a < 1 || b < 1 || a > n || b > n) throw rangeErr('Page ' + (a < 1 || a > n ? a : b) + ' does not exist - this PDF has ' + n + ' page' + (n === 1 ? '' : 's') + '.');
        if (a > b) throw rangeErr('"' + part + '" goes backwards. Write it as ' + b + '-' + a + '.');
        for (i = a; i <= b; i++) g.push(i - 1);
      }
      out.push(g);
    });
    if (!out.length) throw rangeErr('Type the pages you want, for example 1-3, 5, 8-last.');
    return out;
  };
  PT.parseRanges = function (str, n) {
    var seen = {}, out = [];
    PT.parseRangeGroups(str, n).forEach(function (g) { g.forEach(function (p) { if (!seen[p]) { seen[p] = 1; out.push(p); } }); });
    return out;
  };
  /* [0,1,2,5] -> "1-3, 6" (for showing a selection) */
  PT.rangeText = function (idx) {
    var a = idx.slice().sort(function (x, y) { return x - y; }), out = [], i = 0;
    while (i < a.length) { var j = i; while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++; out.push(j > i ? (a[i] + 1) + '-' + (a[j] + 1) : String(a[i] + 1)); i = j + 1; }
    return out.join(', ');
  };

  /* ---------- opening PDFs ---------- */
  function engineCheck(needLib, needJs) {
    if (needLib && !window.PDFLib) { var e = new Error('The PDF engine (pdf-lib) did not load. Check your connection or that you opened this page from a web server (not as a file).'); e.friendly = true; throw e; }
    if (needJs && !window.pdfjsLib) { var e2 = new Error('The PDF viewer engine (pdf.js) did not load. Open this page from a web server (see the README), not by double-clicking the file.'); e2.friendly = true; throw e2; }
  }
  PT.ensureEngines = engineCheck;
  if (window.pdfjsLib) {
    try { try { pdfjsLib.GlobalWorkerOptions.workerPort = new Worker(window.__PDF_WORKER_URL); } catch (e2) { pdfjsLib.GlobalWorkerOptions.workerSrc = window.__PDF_WORKER_URL; } } catch (e) { console.warn(e); }
  }
  /* Open with pdf.js.  src: File | Uint8Array.  Asks for the password if the PDF needs one.
     Returns {doc, bytes, password, numPages, name, size, destroy()}.  Rejects with .cancelled if the user cancels. */
  PT.openPdf = async function (src, opts) {
    opts = opts || {};
    engineCheck(false, true);
    var bytes = src instanceof Uint8Array ? src : await PT.readBytes(src);
    var name = opts.name || src.name || 'document.pdf';
    // quick sanity check: a PDF has "%PDF-" near the start. (Repair tool skips this.)
    if (!opts.skipHeaderCheck) {
      var head = new TextDecoder('latin1').decode(bytes.subarray(0, Math.min(1024, bytes.length)));
      if (head.indexOf('%PDF-') < 0) { var e = new Error('"' + name + '" is not a PDF file (no PDF header found). If it is damaged, try the Repair PDF tool.'); e.friendly = true; throw e; }
    }
    var password = opts.password, tries = 0;
    for (;;) {
      var task = pdfjsLib.getDocument({
        data: bytes.slice(),                                   // pdf.js takes ownership of the buffer, so give it a copy
        password: password, cMapUrl: ASSETS + 'vendor/cmaps/', cMapPacked: true,
        standardFontDataUrl: ASSETS + 'vendor/standard_fonts/', isEvalSupported: false
      });
      try {
        var doc = await task.promise;
        return { doc: doc, bytes: bytes, password: password, numPages: doc.numPages, name: name, size: bytes.length, destroy: function () { try { doc.destroy(); } catch (e) { /* ignore */ } } };
      } catch (err) {
        try { task.destroy(); } catch (e2) { /* ignore */ }
        if (err && err.name === 'PasswordException' && opts.askPassword !== false) {
          var pw = await PT.askPassword(name, tries++ > 0 && err.code === 2, opts.passwordText);
          if (pw == null) throw PT.CANCEL();
          password = pw; continue;
        }
        throw err;
      }
    }
  };
  /* Open the same file with pdf-lib so it can be changed. info = result of openPdf(). */
  PT.loadLib = async function (info, extra) {
    engineCheck(true, false);
    try {
      var doc = await PDFLib.PDFDocument.load(info.bytes, Object.assign({ password: info.password || undefined, updateMetadata: false, throwOnInvalidObject: false }, extra || {}));
      if (info.password) {                                     // the engine leaves some document-info strings scrambled: blank any that look broken
        ['Title', 'Author', 'Subject', 'Keywords', 'Creator', 'Producer'].forEach(function (k) {
          try { var v = doc['get' + k](); if (v && /[\x00-\x08\x0e-\x1f\ufffd\u0080-\u009f]/.test(v)) doc['set' + k](k === 'Keywords' ? [] : ''); } catch (e) { try { doc['set' + k](k === 'Keywords' ? [] : ''); } catch (e2) { /* ignore */ } }
        });
        doc.setProducer('Binjal Halwai PDF Tools'); doc.setCreator('Binjal Halwai PDF Tools');
      }
      return doc;
    } catch (e) {
      if (/password|encrypt/i.test(String(e && e.message))) { var f = new Error('This PDF is protected and could not be opened for editing. Use Unlock PDF first.'); f.friendly = true; throw f; }
      throw e;
    }
  };
  PT.saveDoc = async function (doc, opts) { return await doc.save(Object.assign({ useObjectStreams: true, addDefaultPage: false }, opts || {})); };

  /* ---------- rendering + thumbnails ---------- */
  var MAX_PIXELS = 16000000;                                   // stay inside mobile canvas limits
  /* Render page `n` (1-based) to a canvas.  opts: {scale | maxSide, rotate (extra degrees), background} */
  PT.renderPage = async function (doc, n, opts) {
    opts = opts || {};
    var page = await doc.getPage(n);
    var rot = (((page.rotate + (opts.rotate || 0)) % 360) + 360) % 360;
    var v1 = page.getViewport({ scale: 1, rotation: rot });
    var scale = opts.scale || (opts.maxSide ? opts.maxSide / Math.max(v1.width, v1.height) : 1);
    if (v1.width * v1.height * scale * scale > MAX_PIXELS) scale = Math.sqrt(MAX_PIXELS / (v1.width * v1.height));
    var vp = page.getViewport({ scale: scale, rotation: rot });
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.floor(vp.width)); c.height = Math.max(1, Math.floor(vp.height));
    var ctx = c.getContext('2d', { alpha: false });
    ctx.fillStyle = opts.background || '#fff'; ctx.fillRect(0, 0, c.width, c.height);
    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    var out = { canvas: c, width: v1.width, height: v1.height, scale: scale, rotate: rot, pdfRotate: page.rotate };
    page.cleanup();
    return out;
  };
  PT.canvasToBlob = function (c, type, q) { return new Promise(function (res, rej) { c.toBlob(function (b) { b ? res(b) : rej(new Error('Could not create the image (the page may be too large for this device).')); }, type || 'image/png', q); }); };

  /* Thumbnails: renders on demand, two at a time, cached as JPEG blob URLs.
     var th = new PT.Thumbs(info);   th.get(pageNo) -> Promise<{url,w,h}>   th.dispose() */
  PT.Thumbs = function (info, maxSide) {
    this.info = info; this.max = maxSide || 300; this.cache = {}; this.q = []; this.running = 0; this.dead = false;
  };
  PT.Thumbs.prototype.get = function (n) {
    var self = this;
    if (!this.cache[n]) this.cache[n] = new Promise(function (res, rej) { self.q.push({ n: n, res: res, rej: rej }); self._pump(); });
    return this.cache[n];
  };
  PT.Thumbs.prototype._pump = function () {
    var self = this;
    while (this.running < 2 && this.q.length && !this.dead) {
      var job = this.q.shift(); this.running++;
      (function (job) {
        PT.renderPage(self.info.doc, job.n, { maxSide: self.max }).then(function (r) {
          return PT.canvasToBlob(r.canvas, 'image/jpeg', 0.8).then(function (b) {
            r.canvas.width = r.canvas.height = 1;               // free canvas memory early (Safari)
            job.res({ url: URL.createObjectURL(b), w: r.width, h: r.height });
          });
        }).catch(function (e) { job.rej(e); }).then(function () { self.running--; self._pump(); });
      })(job);
    }
  };
  PT.Thumbs.prototype.dispose = function () {
    this.dead = true;
    Object.keys(this.cache).forEach(function (k) { this.cache[k].then(function (t) { URL.revokeObjectURL(t.url); }, function () { }); }, this);
    this.cache = {}; this.q = [];
  };
  /* Calls fn() once when el scrolls near the screen (lazy thumbnails). */
  var io = null;
  PT.whenVisible = function (el, fn) {
    if (!('IntersectionObserver' in window)) { fn(); return; }
    if (!io) io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); var f = e.target._wv; e.target._wv = null; if (f) f(); } });
    }, { rootMargin: '400px' });
    el._wv = fn; io.observe(el);
  };
  PT.unwatch = function (el) { if (io) io.unobserve(el); el._wv = null; };

  /* ---------- page picker (used by Split, Rotate and PDF to JPG) ----------
     A grid of page thumbnails. Click a page to select it, Shift+click for a range, Space or Enter with the keyboard.
       var grid = PT.PageGrid(host, info, thumbs, { onChange: function (selectedIndexes) {}, badge: function (i) { return 'text'; } });
     grid.el  the wrapper element (set grid.el.hidden to hide it)      grid.selected()  selected pages, 0-based, ascending
     grid.count()  how many are selected      grid.set([0,2])  replace the selection      grid.selectAll() / grid.clear()
     grid.setRot(i, deg)  show page i turned by deg (0/90/180/270) and refresh its badge
     onChange runs for changes made by the user and by set()/selectAll()/clear(); it does not run when the grid is created. */
  PT.PageGrid = function (host, info, thumbs, o) {
    o = o || {};
    var h = PT.h, n = info.numPages, sel = new Set(), cards = [], rots = new Array(n).fill(0), last = -1;
    var el = h('div', { class: 'pagegrid' }), list = h('div', { class: 'pgrid', role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': 'Pages of your document' });
    var all = h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { api.selectAll(); } }, 'Select all');
    var none = h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { api.clear(); } }, 'Clear selection');
    var count = h('span', { class: 'note', 'aria-live': 'polite' });
    el.appendChild(h('div', { class: 'row', style: { margin: '0 0 10px', alignItems: 'center' } }, all, none, count));
    el.appendChild(list);

    function sorted() { return Array.from(sel).sort(function (a, b) { return a - b; }); }
    function paint(i) {
      var c = cards[i], on = sel.has(i), tag = c.querySelector('.tag'), box = c.querySelector('.tbox'), im = box.querySelector('img'), r = rots[i];
      c.classList.toggle('sel', on); c.setAttribute('aria-selected', on ? 'true' : 'false'); c.querySelector('.cb').textContent = on ? '✓' : '';
      box.classList.toggle('r90', r === 90); box.classList.toggle('r270', r === 270);
      if (im) im.style.transform = r ? 'rotate(' + r + 'deg)' : '';
      var t = o.badge ? String(o.badge(i) || '') : ''; tag.textContent = t; tag.hidden = !t;
    }
    function changed() {
      count.textContent = sel.size ? sel.size + ' of ' + n + ' page(s) selected' : 'No pages selected';
      if (o.onChange) o.onChange(sorted());
    }
    function toggle(i, e) {
      if (e && e.shiftKey && last >= 0) { for (var k = Math.min(last, i); k <= Math.max(last, i); k++) { sel.add(k); paint(k); } }
      else { if (sel.has(i)) sel.delete(i); else sel.add(i); paint(i); }
      last = i; changed();
    }
    function card(i) {
      var box = h('div', { class: 'tbox' }, h('div', { class: 'ph' }));
      var c = h('div', { class: 'pcard', role: 'option', tabindex: '0', 'aria-selected': 'false', 'aria-label': 'Page ' + (i + 1) },
        h('span', { class: 'cb', 'aria-hidden': 'true' }), box, h('div', { class: 'pn' }, h('b', null, String(i + 1)), h('span', { class: 'tag', hidden: true })));
      c.addEventListener('click', function (e) { toggle(i, e); });
      c.addEventListener('keydown', function (e) { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(i, e); } });
      PT.whenVisible(c, function () {
        thumbs.get(i + 1).then(function (t) { PT.clear(box); box.appendChild(h('img', { src: t.url, alt: 'Page ' + (i + 1), draggable: 'false' })); paint(i); },
          function () { PT.clear(box); box.appendChild(h('span', { class: 'note' }, 'No preview')); });
      });
      return c;
    }
    for (var i = 0; i < n; i++) { cards.push(card(i)); list.appendChild(cards[i]); paint(i); }
    count.textContent = 'No pages selected';
    host.appendChild(el);

    var api = {
      el: el,
      selected: function () { return sorted(); },
      count: function () { return sel.size; },
      set: function (idx) {
        var next = new Set((idx || []).filter(function (k) { return Number.isInteger(k) && k >= 0 && k < n; }));
        sel.forEach(function (k) { if (!next.has(k)) { sel.delete(k); paint(k); } });
        next.forEach(function (k) { if (!sel.has(k)) { sel.add(k); paint(k); } });
        changed();
      },
      selectAll: function () { for (var k = 0; k < n; k++) sel.add(k); cards.forEach(function (_, k) { paint(k); }); changed(); },
      clear: function () { var had = sel.size; sel.clear(); cards.forEach(function (_, k) { paint(k); }); if (had) changed(); },
      setRot: function (i, deg) { if (i >= 0 && i < n) { rots[i] = ((deg % 360) + 360) % 360; paint(i); } }
    };
    return api;
  };

  /* ---------- images -> pdf-lib ---------- */
  function jpegInfo(u8) {                                      // reads orientation and component count from a JPEG
    var o = 1, comps = 3, i = 2;
    if (u8[0] !== 0xFF || u8[1] !== 0xD8) return { o: 1, comps: 3 };
    while (i + 4 < u8.length) {
      if (u8[i] !== 0xFF) { i++; continue; }
      var m = u8[i + 1];
      if (m === 0xFF) { i++; continue; }
      if (m === 0xD8 || (m >= 0xD0 && m <= 0xD7) || m === 0x01) { i += 2; continue; }
      var len = (u8[i + 2] << 8) | u8[i + 3];
      if (m === 0xE1 && u8[i + 4] === 0x45 && u8[i + 5] === 0x78 && u8[i + 6] === 0x69 && u8[i + 7] === 0x66) {   // "Exif"
        var t = i + 10, le = u8[t] === 0x49;
        var r16 = function (p) { return le ? (u8[p] | u8[p + 1] << 8) : (u8[p] << 8 | u8[p + 1]); };
        var r32 = function (p) { return le ? (u8[p] | u8[p + 1] << 8 | u8[p + 2] << 16 | u8[p + 3] << 24) >>> 0 : (u8[p] << 24 | u8[p + 1] << 16 | u8[p + 2] << 8 | u8[p + 3]) >>> 0; };
        var ifd = t + r32(t + 4), cnt = r16(ifd);
        for (var k = 0; k < cnt && k < 64; k++) { var e = ifd + 2 + k * 12; if (r16(e) === 0x0112) { o = r16(e + 8); break; } }
      }
      if (m >= 0xC0 && m <= 0xCF && m !== 0xC4 && m !== 0xC8 && m !== 0xCC) { comps = u8[i + 9]; break; }
      if (m === 0xDA) break;
      i += 2 + len;
    }
    return { o: o, comps: comps };
  }
  PT.jpegInfo = jpegInfo;
  PT.mimeOf = function (u8) {
    if (u8[0] === 0xFF && u8[1] === 0xD8) return 'jpeg';
    if (u8[0] === 0x89 && u8[1] === 0x50 && u8[2] === 0x4E && u8[3] === 0x47) return 'png';
    return 'other';
  };
  async function bitmapFrom(file) {
    if (window.createImageBitmap) { try { return await createImageBitmap(file); } catch (e) { /* fall through */ } }
    var url = URL.createObjectURL(file);
    try {
      return await new Promise(function (res, rej) { var im = new Image(); im.onload = function () { res(im); }; im.onerror = function () { rej(new Error('"' + (file.name || 'image') + '" could not be read as an image.')); }; im.src = url; });
    } finally { setTimeout(function () { URL.revokeObjectURL(url); }, 5000); }
  }
  /* Read an image file into {bytes, kind:'jpeg'|'png', width, height, name, url(thumb)}.
     JPEG with rotation info (EXIF) and non-JPEG/PNG types are redrawn through a canvas so they look right in the PDF. */
  PT.readImage = async function (file, name) {
    var u8 = file instanceof Uint8Array ? file : await PT.readBytes(file);
    var kind = PT.mimeOf(u8), blob = file instanceof Blob ? file : new Blob([u8]);
    var bmp = await bitmapFrom(blob);                          // already upright in modern browsers
    var w = bmp.width, h2 = bmp.height, redraw = false;
    if (kind === 'jpeg') { var ji = jpegInfo(u8); if (ji.o !== 1 || ji.comps === 4) redraw = true; }
    else if (kind === 'other') redraw = true;
    if (redraw) {
      var c = document.createElement('canvas'); c.width = w; c.height = h2;
      var ctx = c.getContext('2d'); if (kind === 'jpeg') { ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h2); }
      ctx.drawImage(bmp, 0, 0);
      var isPng = kind !== 'jpeg';
      var nb = await PT.canvasToBlob(c, isPng ? 'image/png' : 'image/jpeg', 0.95);
      u8 = new Uint8Array(await nb.arrayBuffer()); kind = isPng ? 'png' : 'jpeg';
    }
    if (bmp.close) bmp.close();
    return { bytes: u8, kind: kind, width: w, height: h2, name: name || file.name || 'image' };
  };
  /* Embed a readImage() result in a pdf-lib document. */
  PT.embedImage = async function (doc, img) {
    try { return img.kind === 'jpeg' ? await doc.embedJpg(img.bytes) : await doc.embedPng(img.bytes); }
    catch (e) {                                                // odd PNG/JPEG variants: re-encode via canvas once
      var bmp = await bitmapFrom(new Blob([img.bytes]));
      var c = document.createElement('canvas'); c.width = bmp.width; c.height = bmp.height;
      c.getContext('2d').drawImage(bmp, 0, 0);
      var b = await PT.canvasToBlob(c, 'image/png');
      return await doc.embedPng(new Uint8Array(await b.arrayBuffer()));
    }
  };
  PT.imageThumb = function (img) { return URL.createObjectURL(new Blob([img.bytes], { type: img.kind === 'png' ? 'image/png' : 'image/jpeg' })); };

  /* ---------- text that the standard PDF fonts cannot draw (e.g. Nepali / Hindi) ----------
     The 14 standard PDF fonts only know Latin-1 characters. For anything else we draw the
     text on a canvas with the browser's own fonts and embed it as a transparent PNG. */
  PT.canEncode = function (font, text) { try { font.encodeText(text); return true; } catch (e) { return false; } };
  PT.textToPng = async function (text, o) {
    o = o || {};
    var px = o.px || 96, lines = String(text).split(/\r?\n/), pad = Math.round(px * 0.2);
    var fam = o.family || '"Noto Sans Devanagari","Mangal","Nirmala UI","Segoe UI",Arial,sans-serif';
    var font = (o.bold ? 'bold ' : '') + px + 'px ' + fam;
    var c = document.createElement('canvas'), ctx = c.getContext('2d');
    ctx.font = font;
    var w = 0; lines.forEach(function (l) { w = Math.max(w, ctx.measureText(l).width); });
    var lh = Math.round(px * 1.3);
    c.width = Math.ceil(w + pad * 2); c.height = lines.length * lh + pad * 2;
    ctx = c.getContext('2d'); ctx.font = font; ctx.textBaseline = 'top'; ctx.fillStyle = o.color || '#000';
    lines.forEach(function (l, i) { ctx.fillText(l, pad, pad + i * lh); });
    var b = await PT.canvasToBlob(c, 'image/png');
    return { bytes: new Uint8Array(await b.arrayBuffer()), width: c.width, height: c.height };
  };

  /* ---------- paper sizes (points) ---------- */
  PT.PAPER = { a4: [595.28, 841.89], letter: [612, 792], legal: [612, 1008], a3: [841.89, 1190.55], a5: [419.53, 595.28] };

  /* ---------- drag-and-drop reordering (mouse, touch and pen) ----------
     PT.dragSort(container, {
        items(): array of item elements (in order),
        handle: '.grip',            // touch/pen must start on this; mouse may start anywhere if mouseAnywhere
        mouseAnywhere: true,
        horizontal: true,           // grids flow left->right; lists (false) flow top->bottom
        getDragged(el): array of elements moved together (default [el]),
        onDrop(draggedEls, beforeEl|null)   // insert before beforeEl (null = at the end)
     })                                                        */
  PT.dragSort = function (box, o) {
    var st = null, justDragged = false;
    box.style.position = box.style.position || 'relative';
    function candidates() { return o.items().filter(function (el) { return st.dragged.indexOf(el) < 0; }); }
    function locate(x, y) {
      var list = candidates(), best = null, bd = 1e12;
      list.forEach(function (el, i) {
        var r = el.getBoundingClientRect();
        var dx = x < r.left ? r.left - x : x > r.right ? x - r.right : 0;
        var dy = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
        var d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = { el: el, i: i, r: r }; }
      });
      if (!best) return { before: null, list: list, i: -1 };
      var after = o.horizontal ? (x > best.r.left + best.r.width / 2) : (y > best.r.top + best.r.height / 2);
      if (o.horizontal && (y > best.r.bottom + 4 || y < best.r.top - 4)) after = y > best.r.bottom;     // pointer in a different row
      return { before: after ? (list[best.i + 1] || null) : best.el, ref: best.el, after: after, list: list };
    }
    function showLine(t) {
      if (!st.line) { st.line = h('div', { class: 'dropline' }); box.appendChild(st.line); }
      if (!t.ref) { st.line.style.display = 'none'; return; }
      var br = box.getBoundingClientRect(), r = t.ref.getBoundingClientRect(), s = st.line.style;
      s.display = 'block';
      if (o.horizontal) {
        s.width = '4px'; s.height = r.height + 'px'; s.top = (r.top - br.top) + 'px';
        s.left = ((t.after ? r.right : r.left) - br.left - 6) + 'px';
      } else {
        s.height = '4px'; s.width = r.width + 'px'; s.left = (r.left - br.left) + 'px';
        s.top = ((t.after ? r.bottom : r.top) - br.top - 6) + 'px';
      }
    }
    function end(commit) {
      if (!st) return;
      cancelAnimationFrame(st.raf);
      window.removeEventListener('pointermove', move, true); window.removeEventListener('pointerup', up, true);
      window.removeEventListener('pointercancel', cancel, true); window.removeEventListener('keydown', esc, true);
      var s = st; st = null;
      if (s.ghost) s.ghost.remove();
      if (s.line) s.line.remove();
      s.dragged.forEach(function (el) { el.classList.remove('dragging'); });
      document.body.style.userSelect = '';
      if (s.started) {
        justDragged = true; setTimeout(function () { justDragged = false; }, 60);
        if (commit && s.target && s.target.list) o.onDrop(s.dragged, s.target.before);
      }
    }
    function cancel() { end(false); }
    function esc(e) { if (e.key === 'Escape') end(false); }
    function up() { end(true); }
    function scrollLoop() {
      if (!st || !st.started) return;
      var y = st.y, vh = window.innerHeight, dy = 0;
      if (y < 70) dy = -Math.ceil((70 - y) / 5); else if (y > vh - 70) dy = Math.ceil((y - (vh - 70)) / 5);
      if (dy) { window.scrollBy(0, dy); st.target = locate(st.x, st.y); showLine(st.target); }
      st.raf = requestAnimationFrame(scrollLoop);
    }
    function move(e) {
      if (!st || e.pointerId !== st.pid) return;
      st.x = e.clientX; st.y = e.clientY;
      if (!st.started) {
        if (Math.abs(e.clientX - st.sx) + Math.abs(e.clientY - st.sy) < 6) return;
        st.started = true; document.body.style.userSelect = 'none';
        st.dragged.forEach(function (el) { el.classList.add('dragging'); });
        var im = st.dragged[0].querySelector('img');
        st.ghost = h('div', { class: 'dragghost' }, im ? h('img', { src: im.src, style: { display: 'block', maxWidth: '140px', maxHeight: '180px', transform: im.style.transform } }) : h('div', { style: { padding: '8px 12px', color: '#000', lineHeight: 1.3 } }, (st.dragged[0].querySelector('.nm') || {}).textContent || 'Item'),
          st.dragged.length > 1 ? h('span', { class: 'cnt' }, String(st.dragged.length)) : null);
        document.body.appendChild(st.ghost);
        st.raf = requestAnimationFrame(scrollLoop);
      }
      e.preventDefault();
      st.ghost.style.left = e.clientX + 'px'; st.ghost.style.top = e.clientY + 'px';
      st.target = locate(e.clientX, e.clientY); showLine(st.target);
    }
    box.addEventListener('pointerdown', function (e) {
      if (st || (e.pointerType === 'mouse' && e.button !== 0)) return;
      var item = e.target.closest && o.items().filter(function (el) { return el.contains(e.target); })[0];
      if (!item) return;
      var onHandle = o.handle && e.target.closest(o.handle);
      var blocked = e.target.closest('button,input,select,textarea,a');
      if (!onHandle && !(e.pointerType === 'mouse' && o.mouseAnywhere && !blocked)) return;
      if (onHandle) e.preventDefault();
      st = { pid: e.pointerId, sx: e.clientX, sy: e.clientY, x: e.clientX, y: e.clientY, started: false, dragged: o.getDragged ? o.getDragged(item) : [item] };
      window.addEventListener('pointermove', move, { capture: true, passive: false }); window.addEventListener('pointerup', up, true);
      window.addEventListener('pointercancel', cancel, true); window.addEventListener('keydown', esc, true);
    });
    box.addEventListener('dragstart', function (e) { if (e.target.closest && e.target.closest('img')) e.preventDefault(); });   // no native image dragging
    box.addEventListener('click', function (e) { if (justDragged) { e.stopPropagation(); e.preventDefault(); } }, true);
  };

  /* ---------- a small header showing the opened PDF ---------- */
  PT.fileHeader = function (info, thumbs, onClear, clearLabel) {
    var img = h('img', { class: 'thumb', alt: '' });
    thumbs.get(1).then(function (t) { img.src = t.url; }, function () { });
    return h('div', { class: 'fitem', style: { marginBottom: '12px' } }, img,
      h('div', { class: 'meta' }, h('div', { class: 'nm' }, info.name), h('div', { class: 'sub' }, info.numPages + ' page' + (info.numPages === 1 ? '' : 's') + ' · ' + PT.fmtBytes(info.size))),
      onClear ? h('button', { type: 'button', class: 'btn ghost sm', onclick: onClear }, clearLabel || 'Choose another file') : null);
  };

  /* ---------- the usual "choose one PDF, then show options" screen ----------
     PT.single(host, {label, hint, open:false}, function(info, thumbs, area, reset){ ... build the options in `area` ... })
     Shows a drop zone; once a PDF is opened it shows a file header and calls back.       */
  PT.single = function (host, o, cb) {
    var cur = null;
    function start() {
      PT.clear(host);
      host.appendChild(PT.dropzone({ kind: 'pdf', multiple: false, label: o.label, hint: o.hint, onFiles: function (f) { load(f[0]); } }));
    }
    function reset() { if (cur) { cur.thumbs.dispose(); cur.info.destroy(); cur = null; } start(); }
    async function load(file) {
      var r = await PT.run('Opening PDF...', async function () {
        var info = await PT.openPdf(file, o.openOpts);
        return { info: info, thumbs: new PT.Thumbs(info, o.thumbSize || 300) };
      });
      if (!r) return;
      cur = r; PT.clear(host);
      host.appendChild(PT.fileHeader(r.info, r.thumbs, reset));
      var area = h('div'); host.appendChild(area);
      cb(r.info, r.thumbs, area, reset);
    }
    start();
    return { reset: reset };
  };
  /* view -> PDF user space for a page shown with rotation `rot` (0/90/180/270) inside box cb {x,y,width,height}.
     (vx, vy) are measured in points from the TOP-LEFT of the page as you see it on screen. */
  PT.viewMap = function (cb, rot) {
    var W = cb.width, H = cb.height;
    return function (vx, vy) {
      switch (((rot % 360) + 360) % 360) {
        case 90: return [cb.x + vy, cb.y + vx];
        case 180: return [cb.x + W - vx, cb.y + vy];
        case 270: return [cb.x + W - vy, cb.y + H - vx];
        default: return [cb.x + vx, cb.y + H - vy];
      }
    };
  };
  /* size of a pdf-lib page as displayed (rotation applied) */
  PT.viewSize = function (page) {
    var cb = page.getCropBox ? page.getCropBox() : page.getMediaBox(), r = ((page.getRotation().angle % 360) + 360) % 360;
    return { cb: cb, rot: r, w: r % 180 ? cb.height : cb.width, h: r % 180 ? cb.width : cb.height };
  };

  /* ---------- last-resort error reporting (never leave the user with a silent failure) ---------- */
  window.addEventListener('unhandledrejection', function (e) {
    var m = PT.friendlyError(e.reason); if (m) { console.error(e.reason); PT.notify(m, 'err'); }
  });
  window.addEventListener('error', function (e) { console.error(e.error || e.message); });
})();
