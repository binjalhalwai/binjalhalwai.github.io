
(function(){
 var TOOLJS={
"home":function(){
/* Home page: live search + category chips for the tool cards. */
(function () {
  'use strict';
  var cards = Array.prototype.slice.call(document.querySelectorAll('#tgrid .tcard'));
  var q = document.getElementById('q'), none = document.getElementById('noMatch');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var cat = 'all';
  function apply() {
    var term = (q.value || '').toLowerCase().trim(), shown = 0;
    cards.forEach(function (c) {
      var ok = (cat === 'all' || c.getAttribute('data-cat') === cat) &&
        (!term || term.split(/\s+/).every(function (w) { return c.getAttribute('data-q').indexOf(w) >= 0; }));
      c.hidden = !ok; if (ok) shown++;
    });
    none.hidden = shown > 0;
  }
  q.addEventListener('input', apply);
  chips.forEach(function (b) {
    b.addEventListener('click', function () {
      cat = b.getAttribute('data-cat');
      chips.forEach(function (x) { x.classList.toggle('on', x === b); });
      apply();
    });
  });
})();

},
"organize":function(){
/* =====================================================================
   ORGANIZE PDF  -  the page editor (the most important tool)

   Model
     srcs   : every file the user opened (PDFs and images). We keep the original
              bytes in memory only; nothing is uploaded or stored.
     items  : the pages of the NEW document, in order. Each item points at one
              page of one source:  {id, src, page, rot}   (rot = extra turns, 0/90/180/270)
              or is a blank page {id, blank:true, w, h}.
     sel    : Set of selected item ids. Every action (rotate, delete, duplicate,
              move, extract) works ONLY on the selected pages.
     hist   : undo / redo snapshots of `items`.

   "Apply changes" builds a new PDF with pdf-lib by COPYING the original page
   objects (no re-rendering), so text stays text and image quality is untouched.
   ===================================================================== */
(function () {
  'use strict';
  var h = PT.h;
  var host = PT.mount();
  var srcs = new Map();                 // id -> source
  var items = [], sel = new Set();
  var hist = [], redoStack = [];
  var cardMap = new Map();
  var built = null;                     // {bytes} of the last Apply, null when stale
  var dirty = false;
  var el = {};                          // UI elements

  /* ------------------------------------------------------------ start screen */
  function showStart() {
    PT.clear(host);
    host.appendChild(PT.dropzone({
      kind: 'pdf+image', multiple: true, label: 'Select PDF files (or images)',
      hint: 'or drop files here. You can add more PDFs or images later, even between existing pages.',
      onFiles: function (files) { addFiles(files, 'end', true); }
    }));
  }

  /* ------------------------------------------------------------ history */
  function snap() { return { items: items.map(function (i) { return Object.assign({}, i); }), sel: Array.from(sel) }; }
  function restore(s) {
    items = s.items.map(function (i) { return Object.assign({}, i); });
    var ids = new Set(items.map(function (i) { return i.id; }));
    sel = new Set(s.sel.filter(function (id) { return ids.has(id); }));
  }
  function commit() { hist.push(snap()); if (hist.length > 100) hist.shift(); redoStack = []; markDirty(); }
  function undo() { if (!hist.length) return; redoStack.push(snap()); restore(hist.pop()); markDirty(); render(); }
  function redo() { if (!redoStack.length) return; hist.push(snap()); restore(redoStack.pop()); markDirty(); render(); }
  function markDirty() {
    dirty = true; built = null;
    var r = host.querySelector(':scope > .result'); if (r) r.remove();
  }

  /* ------------------------------------------------------------ adding files */
  function insertIndex(pos) {
    var idx = items.map(function (i, k) { return sel.has(i.id) ? k : -1; }).filter(function (k) { return k >= 0; });
    if (pos === 'start') return 0;
    if (pos === 'end') return items.length;
    if (pos === 'afterN') return PT.clamp(parseInt(el.posN && el.posN.value, 10) || 0, 0, items.length);
    if (!idx.length) { PT.notify('Select a page first (or choose "At the end" / "At the start").', 'warn'); return -1; }
    return pos === 'before' ? idx[0] : idx[idx.length - 1] + 1;
  }
  async function addFiles(files, pos, first) {
    var at = first ? 0 : insertIndex(pos);
    if (at < 0) return;
    var added = [];
    await PT.run('Reading files...', async function (b) {
      for (var f = 0; f < files.length; f++) {
        var file = files[f];
        b.set(f / files.length, file.name); await PT.tick();
        try {
          var s;
          if (PT.isPdf(file)) {
            var info = await PT.openPdf(file);
            s = { id: PT.uid(), kind: 'pdf', name: file.name, info: info, thumbs: new PT.Thumbs(info, 300) };
            srcs.set(s.id, s);
            for (var p = 1; p <= info.numPages; p++) added.push({ id: PT.uid(), src: s.id, page: p, rot: 0 });
          } else {
            var img = await PT.readImage(file);
            s = { id: PT.uid(), kind: 'image', name: file.name, img: img, url: PT.imageThumb(img) };
            srcs.set(s.id, s);
            added.push({ id: PT.uid(), src: s.id, page: 1, rot: 0 });
          }
        } catch (e) {
          if (e && e.cancelled) { PT.notify('Skipped "' + file.name + '".', 'warn'); continue; }
          PT.notify('"' + file.name + '": ' + PT.friendlyError(e), 'err');
        }
      }
    });
    if (!added.length) { if (first) showStart(); return; }
    if (first && !el.grid) buildEditor();
    commit();
    items.splice.apply(items, [at, 0].concat(added));
    sel = new Set(added.map(function (i) { return i.id; }));      // select what was just added so it is easy to see
    if (first) { sel = new Set(); hist = []; dirty = false; }
    render();
    if (!first) PT.notify(added.length + ' page' + (added.length === 1 ? '' : 's') + ' added.', 'ok');
  }

  /* ------------------------------------------------------------ editing actions (selected pages only) */
  function needSel() { if (!sel.size) { PT.notify('Select one or more pages first (click a page).', 'warn'); return false; } return true; }
  function rotateSel(deg) { if (!needSel()) return; commit(); items.forEach(function (i) { if (sel.has(i.id)) i.rot = ((i.rot + deg) % 360 + 360) % 360; }); render(); }
  function rotateOne(it, deg) { commit(); it.rot = ((it.rot + deg) % 360 + 360) % 360; render(); }
  function deleteSel() { if (!needSel()) return; commit(); var n = sel.size; items = items.filter(function (i) { return !sel.has(i.id); }); sel.clear(); render(); PT.notify(n + ' page' + (n === 1 ? '' : 's') + ' deleted. Use Undo to bring back.', 'ok'); }
  function deleteOne(it) { commit(); items = items.filter(function (i) { return i !== it; }); sel.delete(it.id); render(); }
  function dupSel() {
    if (!needSel()) return; commit();
    var out = []; items.forEach(function (i) { out.push(i); if (sel.has(i.id)) out.push(Object.assign({}, i, { id: PT.uid() })); });
    items = out; render();
  }
  function dupOne(it) { commit(); items.splice(items.indexOf(it) + 1, 0, Object.assign({}, it, { id: PT.uid() })); render(); }
  function moveSel(dir) {
    if (!needSel()) return;
    var a = items.slice(), moved = false, i;
    if (dir < 0) { for (i = 1; i < a.length; i++) if (sel.has(a[i].id) && !sel.has(a[i - 1].id)) { var t = a[i]; a[i] = a[i - 1]; a[i - 1] = t; moved = true; } }
    else { for (i = a.length - 2; i >= 0; i--) if (sel.has(a[i].id) && !sel.has(a[i + 1].id)) { var u = a[i]; a[i] = a[i + 1]; a[i + 1] = u; moved = true; } }
    if (!moved) { PT.notify(dir < 0 ? 'Already at the start.' : 'Already at the end.', 'warn', 1800); return; }
    commit(); items = a; render();
    var first = items.filter(function (i) { return sel.has(i.id); })[0];
    var c = first && cardMap.get(first.id); if (c) c.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  function moveOne(it, dir) { var k = items.indexOf(it), j = k + dir; if (j < 0 || j >= items.length) return; commit(); items.splice(k, 1); items.splice(j, 0, it); render(); }
  async function blankSel() {
    var base = items.filter(function (i) { return !i.blank; })[0], w = 595.28, hh = 841.89;
    if (base) {
      var s = srcs.get(base.src);
      if (s.kind === 'pdf') { try { var v = (await s.info.doc.getPage(base.page)).getViewport({ scale: 1 }); w = v.width; hh = v.height; } catch (e) { /* keep A4 */ } }
    }
    var at = sel.size ? Math.max.apply(null, items.map(function (i, k) { return sel.has(i.id) ? k : -1; })) + 1 : items.length;
    commit(); items.splice(at, 0, { id: PT.uid(), blank: true, w: w, h: hh, rot: 0 }); render();
  }
  function selectRange(text) {
    try { var idx = PT.parseRanges(text, items.length); sel = new Set(idx.map(function (k) { return items[k].id; })); render(); }
    catch (e) { PT.notify(PT.friendlyError(e), 'err'); }
  }

  /* ------------------------------------------------------------ big preview of one page */
  async function preview(it) {
    var s = it.blank ? null : srcs.get(it.src), body = h('div', { style: { textAlign: 'center' } }, h('p', { class: 'loading' }, 'Rendering...'));
    var p = PT.dialog({ title: 'Page preview', wide: true, body: body, buttons: [{ label: 'Close', value: true, primary: true }] });
    try {
      var node;
      if (it.blank) node = h('div', { style: { background: '#fff', width: '60%', aspectRatio: it.w + '/' + it.h, margin: '0 auto', boxShadow: 'var(--shadow)' } });
      else if (s.kind === 'image') node = h('img', { src: s.url, alt: 'Image', style: { maxWidth: '100%', maxHeight: '70vh', transform: it.rot ? 'rotate(' + it.rot + 'deg)' : '' } });
      else { var r = await PT.renderPage(s.info.doc, it.page, { maxSide: 1100, rotate: it.rot }); r.canvas.style.cssText = 'max-width:100%;max-height:70vh;width:auto;height:auto;box-shadow:var(--shadow)'; node = r.canvas; }
      PT.clear(body); body.appendChild(node);
    } catch (e) { PT.clear(body); body.appendChild(h('p', { class: 'err' }, PT.friendlyError(e))); }
    await p;
  }

  /* ------------------------------------------------------------ rendering the grid */
  function tagText(it) {
    if (it.blank) return 'blank';
    var s = srcs.get(it.src);
    if (s.kind === 'image') return 'image';
    return (srcs.size > 1 ? s.name.replace(/\.pdf$/i, '').slice(0, 14) + ' ' : 'orig ') + it.page;
  }
  function qbtn(txt, title, fn, cls) { return h('button', { type: 'button', class: cls || '', title: title, 'aria-label': title, onclick: function (e) { e.stopPropagation(); fn(); } }, txt); }
  function makeCard(it) {
    var box = h('div', { class: 'tbox' }, h('div', { class: 'ph' }));
    var c = h('div', { class: 'pcard', role: 'option', tabindex: '0', 'aria-selected': 'false' },
      h('span', { class: 'cb', 'aria-hidden': 'true' }), h('span', { class: 'grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, '⠇'),
      box, h('div', { class: 'pn' }, h('b'), h('span', { class: 'tag' })),
      h('div', { class: 'qa' },
        qbtn('⟲', 'Rotate this page left', function () { rotateOne(it, -90); }),
        qbtn('⟳', 'Rotate this page right', function () { rotateOne(it, 90); }),
        qbtn('⧉', 'Duplicate this page', function () { dupOne(it); }),
        qbtn('⌕', 'Preview this page', function () { preview(it); }),
        qbtn('✕', 'Delete this page', function () { deleteOne(it); }, 'del')));
    c._it = it;
    c.addEventListener('click', function (e) { clickCard(it, e); });
    c.addEventListener('keydown', function (e) { if (e.target === c && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); clickCard(it, e); } });
    PT.whenVisible(c, function () {
      if (it.blank) { PT.clear(box); box.appendChild(h('div', { class: 'blankpg', style: { aspectRatio: it.w + '/' + it.h } })); return; }
      var s = srcs.get(it.src);
      if (s.kind === 'image') { PT.clear(box); box.appendChild(h('img', { src: s.url, alt: 'Image', draggable: 'false' })); paint(c, it); return; }
      s.thumbs.get(it.page).then(function (t) { PT.clear(box); box.appendChild(h('img', { src: t.url, alt: 'Page ' + it.page, draggable: 'false' })); paint(c, it); },
        function () { PT.clear(box); box.appendChild(h('span', { class: 'note' }, 'No preview')); });
    });
    return c;
  }
  var lastClick = null;
  function clickCard(it, e) {
    if (e.shiftKey && lastClick) {
      var a = items.findIndex(function (i) { return i.id === lastClick; }), b = items.indexOf(it);
      if (a >= 0) { for (var k = Math.min(a, b); k <= Math.max(a, b); k++) sel.add(items[k].id); render(); return; }
    }
    if (sel.has(it.id)) sel.delete(it.id); else sel.add(it.id);
    lastClick = it.id; render();
  }
  function paint(c, it) {
    var box = c.querySelector('.tbox'), r = it.rot || 0;
    box.classList.toggle('r90', r === 90); box.classList.toggle('r270', r === 270);
    var im = box.querySelector('img,.blankpg'); if (im) im.style.transform = r ? 'rotate(' + r + 'deg)' : '';
  }
  function render() {
    if (!el.grid) return;
    var seen = new Set(), prev = null;
    items.forEach(function (it, k) {
      seen.add(it.id);
      var c = cardMap.get(it.id); if (!c) { c = makeCard(it); cardMap.set(it.id, c); }
      c._it = it;
      var want = prev ? prev.nextSibling : el.grid.firstChild;
      if (want !== c) el.grid.insertBefore(c, want);
      var on = sel.has(it.id);
      c.classList.toggle('sel', on); c.setAttribute('aria-selected', on ? 'true' : 'false');
      c.firstChild.textContent = on ? '✓' : '';
      c.querySelector('.pn b').textContent = String(k + 1);
      var tg = c.querySelector('.tag'); tg.textContent = tagText(it); tg.title = tg.textContent;
      paint(c, it);
      prev = c;
    });
    Array.from(cardMap.keys()).forEach(function (id) { if (!seen.has(id)) { var c = cardMap.get(id); PT.unwatch(c); c.remove(); cardMap.delete(id); } });
    var n = sel.size;
    el.count.textContent = items.length + ' page' + (items.length === 1 ? '' : 's') + ' · ' + (n ? n + ' selected' : 'none selected');
    el.undo.disabled = !hist.length; el.redo.disabled = !redoStack.length;
    el.apply.disabled = el.dl.disabled = !items.length;
    el.empty.hidden = items.length > 0;
    var sels = ['rotL', 'rotR', 'rot180', 'mvE', 'mvL', 'dup', 'del', 'extract'];
    sels.forEach(function (k) { el[k].disabled = !n; });
    el.files.textContent = 'Files: ' + Array.from(srcs.values()).map(function (s) { return s.name + (s.kind === 'pdf' ? ' (' + s.info.numPages + ' p.)' : ''); }).join(' · ');
  }

  /* ------------------------------------------------------------ build the new PDF */
  async function build(list, b) {
    PT.ensureEngines(true, false);
    var out = await PDFLib.PDFDocument.create();
    out.setProducer('Binjal Halwai PDF Tools'); out.setCreator('Binjal Halwai PDF Tools');
    var bySrc = new Map();
    list.forEach(function (it) {
      if (it.blank) return; var s = srcs.get(it.src); if (s.kind !== 'pdf') return;
      var e = bySrc.get(s.id); if (!e) { e = { idx: [], ids: [] }; bySrc.set(s.id, e); }
      e.idx.push(it.page - 1); e.ids.push(it.id);
    });
    var pageOf = new Map(), k = 0;
    for (var pair of bySrc) {                                      // copy per source, one batch each (keeps shared resources shared)
      var s = srcs.get(pair[0]); b.check(); b.set(0.05 + 0.45 * k / bySrc.size, 'Reading ' + s.name); await PT.tick();
      var lib = await PT.loadLib(s.info);
      var copied = await out.copyPages(lib, pair[1].idx);
      copied.forEach(function (p, i) { pageOf.set(pair[1].ids[i], p); }); k++;
    }
    var embedded = new Map(), n = 0;
    for (var it of list) {
      b.check(); b.set(0.5 + 0.45 * n / list.length, 'Adding page ' + (n + 1) + ' of ' + list.length);
      var page;
      if (it.blank) page = out.addPage([it.w, it.h]);
      else {
        var sc = srcs.get(it.src);
        if (sc.kind === 'image') {
          var emb = embedded.get(sc.id); if (!emb) { emb = await PT.embedImage(out, sc.img); embedded.set(sc.id, emb); }
          var w = sc.img.width, hh = sc.img.height, f = Math.min(841.89 / Math.max(w, hh), 595.28 / Math.min(w, hh));
          page = out.addPage([w * f, hh * f]); page.drawImage(emb, { x: 0, y: 0, width: w * f, height: hh * f });
        } else { page = pageOf.get(it.id); out.addPage(page); }
      }
      if (it.rot) page.setRotation(PDFLib.degrees((((page.getRotation().angle + it.rot) % 360) + 360) % 360));
      if (++n % 8 === 0) await PT.tick();
    }
    b.set(0.97, 'Saving...'); await PT.tick();
    return await PT.saveDoc(out);
  }
  function outName(suffix) {
    var first = Array.from(srcs.values()).filter(function (s) { return s.kind === 'pdf'; })[0] || Array.from(srcs.values())[0];
    return PT.outName(first ? first.name : 'document.pdf', suffix);
  }
  async function apply(thenDownload) {
    if (!items.length) { PT.notify('There are no pages left. Use Undo or add files.', 'warn'); return; }
    if (built) { if (thenDownload) PT.download(PT.pdfBlob(built.bytes), outName('organized')); return; }
    var bytes = await PT.run('Applying your changes...', function (b) { return build(items.slice(), b); }, { cancel: true });
    if (!bytes) return;
    built = { bytes: bytes }; dirty = false;
    var blob = PT.pdfBlob(bytes), name = outName('organized');
    PT.result(host, { title: 'Your PDF is ready', files: [{ name: name, blob: blob }], stats: [{ k: 'pages', v: String(items.length) }, { k: 'size', v: PT.fmtBytes(bytes.length) }],
      note: 'Original quality is preserved: pages are copied, not re-drawn. Keep editing if you like, then apply again.' });
    if (thenDownload) PT.download(blob, name);
  }
  async function extract() {
    if (!needSel()) return;
    var list = items.filter(function (i) { return sel.has(i.id); });
    var bytes = await PT.run('Extracting ' + list.length + ' page(s)...', function (b) { return build(list, b); }, { cancel: true });
    if (!bytes) return;
    PT.download(PT.pdfBlob(bytes), outName('extracted'));
    PT.notify('Extracted ' + list.length + ' page' + (list.length === 1 ? '' : 's') + ' to a new PDF (' + PT.fmtBytes(bytes.length) + ').', 'ok');
  }

  /* ------------------------------------------------------------ editor UI */
  function tb(label, title, fn, key, cls) {
    var b = h('button', { type: 'button', class: 'btn ghost sm ' + (cls || ''), title: title, 'aria-label': title, onclick: fn }, label);
    if (key) el[key] = b; return b;
  }
  function buildEditor() {
    PT.clear(host);
    el.files = h('p', { class: 'note', style: { margin: '0 0 8px' } });
    el.count = h('span', { class: 'cnt' });
    var pos = h('select', { class: 'in', style: { width: 'auto', minHeight: '34px', padding: '4px 8px' }, 'aria-label': 'Where to insert', onchange: function () { el.posN.hidden = pos.value !== 'afterN'; } },
      h('option', { value: 'end' }, 'at the end'), h('option', { value: 'start' }, 'at the start'),
      h('option', { value: 'after' }, 'after selected page'), h('option', { value: 'before' }, 'before selected page'), h('option', { value: 'afterN' }, 'after page number...'));
    el.posN = h('input', { type: 'number', class: 'in', min: '0', placeholder: 'page #', hidden: true, style: { width: '84px', minHeight: '34px', padding: '4px 8px' }, 'aria-label': 'Insert after this page number' });
    var picker = PT.dropzone({ kind: 'pdf+image', multiple: true, compact: true, onFiles: function (f) { addFiles(f, pos.value); } });
    var pickBtn = h('button', { type: 'button', class: 'btn sm', onclick: function () { picker.pick(); } }, '+ Add PDF / image');
    var rangeIn = h('input', { class: 'in', placeholder: 'e.g. 2-5, 9', 'aria-label': 'Select pages by number', style: { width: '120px', minHeight: '34px', padding: '4px 8px' },
      onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); selectRange(rangeIn.value); } } });
    var zoom = h('input', { type: 'range', min: '90', max: '260', value: '150', 'aria-label': 'Thumbnail size', style: { width: '110px' }, oninput: function () { el.grid.style.setProperty('--tw', zoom.value + 'px'); } });

    var toolbar = h('div', { class: 'bar sticky', role: 'toolbar', 'aria-label': 'Page tools' },
      h('div', { class: 'grp' },
        tb('Select all', 'Select every page', function () { sel = new Set(items.map(function (i) { return i.id; })); render(); }),
        tb('None', 'Clear selection', function () { sel.clear(); render(); }),
        tb('Invert', 'Invert selection', function () { sel = new Set(items.filter(function (i) { return !sel.has(i.id); }).map(function (i) { return i.id; })); render(); }),
        tb('Odd', 'Select odd pages', function () { sel = new Set(items.filter(function (i, k) { return k % 2 === 0; }).map(function (i) { return i.id; })); render(); }),
        tb('Even', 'Select even pages', function () { sel = new Set(items.filter(function (i, k) { return k % 2 === 1; }).map(function (i) { return i.id; })); render(); }),
        rangeIn, tb('Select', 'Select the typed pages', function () { selectRange(rangeIn.value); })),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' },
        tb('⟲ 90°', 'Rotate selected pages 90 degrees left', function () { rotateSel(-90); }, 'rotL'),
        tb('⟳ 90°', 'Rotate selected pages 90 degrees right', function () { rotateSel(90); }, 'rotR'),
        tb('180°', 'Rotate selected pages 180 degrees', function () { rotateSel(180); }, 'rot180')),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' },
        tb('← Earlier', 'Move selected pages one place earlier', function () { moveSel(-1); }, 'mvE'),
        tb('Later →', 'Move selected pages one place later', function () { moveSel(1); }, 'mvL'),
        tb('Duplicate', 'Duplicate selected pages', dupSel, 'dup'),
        tb('Delete', 'Delete selected pages (Delete key)', deleteSel, 'del', 'danger'),
        tb('Blank page', 'Insert a blank page after the selection (or at the end)', blankSel)),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' }, tb('Undo', 'Undo (Ctrl+Z)', undo, 'undo'), tb('Redo', 'Redo (Ctrl+Y)', redo, 'redo')),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' }, pickBtn, h('span', { class: 'note' }, 'insert'), pos, el.posN),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' }, tb('Extract selected → new PDF', 'Download only the selected pages as a new PDF', extract, 'extract'), h('label', { class: 'note', style: { display: 'flex', alignItems: 'center', gap: '6px' } }, 'Size', zoom)),
      el.count);
    el.grid = h('div', { class: 'pgrid', role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': 'Pages of your document' });
    el.empty = h('p', { class: 'empty', hidden: true }, 'No pages left. Use Undo, or add a PDF or image.');
    el.apply = h('button', { type: 'button', class: 'btn big', onclick: function () { apply(false); } }, 'Apply changes');
    el.dl = h('button', { type: 'button', class: 'btn big ghost', onclick: function () { apply(true); } }, 'Download final PDF');
    var reset = h('button', { type: 'button', class: 'btn ghost', onclick: async function () {
      if ((hist.length || dirty) && !(await PT.confirm('Start over?', 'This closes the editor and drops all changes you have not downloaded.', 'Start over'))) return;
      teardown(); showStart();
    } }, 'Start over');
    host.appendChild(el.files); host.appendChild(toolbar); host.appendChild(picker); picker.hidden = true;
    host.appendChild(el.grid); host.appendChild(el.empty);
    host.appendChild(h('div', { class: 'row', style: { marginTop: '16px' } }, el.apply, el.dl, reset));
    host.appendChild(h('p', { class: 'hint' }, 'Tip: click pages to select them (Shift-click for a range). Drag a page to move it; drag a selected page to move all selected pages together. Shortcuts: Delete, Ctrl+Z, Ctrl+Y, Ctrl+A.'));
    PT.dragSort(el.grid, {
      items: function () { return Array.from(el.grid.querySelectorAll(':scope > .pcard')); },
      handle: '.grip', mouseAnywhere: true, horizontal: true,
      getDragged: function (c) {
        if (sel.has(c._it.id) && sel.size > 1) return Array.from(el.grid.querySelectorAll(':scope > .pcard')).filter(function (x) { return sel.has(x._it.id); });
        return [c];
      },
      onDrop: function (cards, before) {
        var moving = cards.map(function (c) { return c._it; }), ids = new Set(moving.map(function (i) { return i.id; }));
        var rest = items.filter(function (i) { return !ids.has(i.id); });
        var at = before ? rest.indexOf(before._it) : rest.length;
        var next = rest.slice(0, at).concat(moving, rest.slice(at));
        if (next.every(function (i, k) { return i === items[k]; })) return;
        commit(); items = next; render();
      }
    });
    // drop files straight onto the grid to append them
    el.grid.addEventListener('dragover', function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0) e.preventDefault(); });
    el.grid.addEventListener('drop', function (e) {
      if (!e.dataTransfer || !e.dataTransfer.files.length) return; e.preventDefault();
      var ok = PT.validate(e.dataTransfer.files, 'pdf+image'); if (ok.length) addFiles(ok, 'end');
    });
    document.addEventListener('keydown', keys);
    window.addEventListener('beforeunload', warn);
  }
  function keys(e) {
    if (!el.grid || !document.body.contains(el.grid)) return;
    var t = e.target, tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || document.querySelector('.ov')) return;
    var ctrl = e.ctrlKey || e.metaKey;
    if (e.key === 'Delete' || e.key === 'Backspace') { if (sel.size) { e.preventDefault(); deleteSel(); } }
    else if (ctrl && e.key.toLowerCase() === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (ctrl && (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey))) { e.preventDefault(); redo(); }
    else if (ctrl && e.key.toLowerCase() === 'a') { e.preventDefault(); sel = new Set(items.map(function (i) { return i.id; })); render(); }
  }
  function warn(e) { if (dirty && hist.length) { e.preventDefault(); e.returnValue = ''; } }
  function teardown() {
    document.removeEventListener('keydown', keys); window.removeEventListener('beforeunload', warn);
    srcs.forEach(function (s) { if (s.thumbs) s.thumbs.dispose(); if (s.info) s.info.destroy(); if (s.url) URL.revokeObjectURL(s.url); });
    srcs.clear(); items = []; sel = new Set(); hist = []; redoStack = []; cardMap.clear(); built = null; dirty = false; el = {};
  }

  showStart();
})();

},
"merge":function(){
/* MERGE PDF - add PDFs, drag them into order, combine into one file. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount(), files = [];     // files: [{id, info, thumbs, name}]
  var listEl, countEl, mergeBtn, picker;

  function build() {
    PT.clear(host);
    picker = PT.dropzone({ kind: 'pdf', multiple: true, label: 'Select PDF files', hint: 'or drop two or more PDF files here. You can add more later.', onFiles: add });
    host.appendChild(picker);
  }
  function editor() {
    if (listEl) return;
    PT.clear(host);
    countEl = h('p', { class: 'note', style: { margin: '0 0 8px' } });
    listEl = h('ul', { class: 'flist', 'aria-label': 'PDF files to merge, in order' });
    var more = PT.dropzone({ kind: 'pdf', multiple: true, compact: true, label: '+ Add more PDFs', hint: 'or drop files here', onFiles: add });
    mergeBtn = h('button', { type: 'button', class: 'btn big', onclick: merge }, 'Merge PDF');
    host.appendChild(countEl); host.appendChild(listEl); host.appendChild(more);
    host.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, mergeBtn,
      h('button', { type: 'button', class: 'btn ghost', onclick: function () { files.slice().sort(function (a, b) { return a.name.localeCompare(b.name, undefined, { numeric: true }); }).forEach(function (f, i) { files[i] = f; }); render(); } }, 'Sort A→Z'),
      h('button', { type: 'button', class: 'btn danger', onclick: clearAll }, 'Remove all')));
    PT.dragSort(listEl, {
      items: function () { return Array.from(listEl.children); }, handle: '.grip', mouseAnywhere: true, horizontal: false,
      onDrop: function (els, before) {
        var f = files.find(function (x) { return x.id === els[0].dataset.id; });
        files = files.filter(function (x) { return x !== f; });
        var at = before ? files.findIndex(function (x) { return x.id === before.dataset.id; }) : files.length;
        files.splice(at, 0, f); render();
      }
    });
  }
  async function add(list) {
    var added = await PT.run('Reading PDFs...', async function (b) {
      var out = [];
      for (var i = 0; i < list.length; i++) {
        b.set(i / list.length, list[i].name); await PT.tick();
        try { var info = await PT.openPdf(list[i]); out.push({ id: PT.uid(), info: info, thumbs: new PT.Thumbs(info, 120), name: list[i].name }); }
        catch (e) { if (e && e.cancelled) PT.notify('Skipped "' + list[i].name + '".', 'warn'); else PT.notify('"' + list[i].name + '": ' + PT.friendlyError(e), 'err'); }
      }
      return out;
    });
    if (!added || !added.length) return;
    editor(); files = files.concat(added); render();
  }
  function clearAll() { files.forEach(dispose); files = []; listEl = null; build(); }
  function dispose(f) { f.thumbs.dispose(); f.info.destroy(); }
  function remove(f) { files = files.filter(function (x) { return x !== f; }); dispose(f); if (!files.length) { listEl = null; build(); } else render(); }
  function move(f, d) { var i = files.indexOf(f), j = i + d; if (j < 0 || j >= files.length) return; files.splice(i, 1); files.splice(j, 0, f); render(); }

  function render() {
    PT.clear(listEl);
    var pages = 0;
    files.forEach(function (f, i) {
      pages += f.info.numPages;
      var img = h('img', { class: 'thumb', alt: '' });
      f.thumbs.get(1).then(function (t) { img.src = t.url; }, function () { });
      listEl.appendChild(h('li', { class: 'fitem', 'data-id': f.id },
        h('span', { class: 'grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, '⠇'), img,
        h('div', { class: 'meta' }, h('div', { class: 'nm', title: f.name }, (i + 1) + '. ' + f.name), h('div', { class: 'sub' }, f.info.numPages + ' page' + (f.info.numPages === 1 ? '' : 's') + ' · ' + PT.fmtBytes(f.info.size))),
        h('button', { type: 'button', class: 'iconbtn', title: 'Move up', 'aria-label': 'Move ' + f.name + ' up', disabled: i === 0, onclick: function () { move(f, -1); } }, '↑'),
        h('button', { type: 'button', class: 'iconbtn', title: 'Move down', 'aria-label': 'Move ' + f.name + ' down', disabled: i === files.length - 1, onclick: function () { move(f, 1); } }, '↓'),
        h('button', { type: 'button', class: 'iconbtn del', title: 'Remove', 'aria-label': 'Remove ' + f.name, onclick: function () { remove(f); } }, '✕')));
    });
    countEl.textContent = files.length + ' file' + (files.length === 1 ? '' : 's') + ' · ' + pages + ' pages in total. Drag to reorder.';
    mergeBtn.disabled = files.length < 2;
    mergeBtn.title = files.length < 2 ? 'Add at least two PDFs' : '';
    var r = host.querySelector(':scope > .result'); if (r) r.remove();
  }

  async function merge() {
    if (files.length < 2) return;
    var res = await PT.run('Merging PDFs...', async function (b) {
      PT.ensureEngines(true, false);
      var out = await PDFLib.PDFDocument.create(), total = 0;
      out.setProducer('Binjal Halwai PDF Tools'); out.setCreator('Binjal Halwai PDF Tools');
      for (var i = 0; i < files.length; i++) {
        b.check(); b.set(i / files.length, 'Adding ' + files[i].name + ' (' + (i + 1) + ' of ' + files.length + ')'); await PT.tick();
        var lib = await PT.loadLib(files[i].info);
        var pages = await out.copyPages(lib, lib.getPageIndices());
        pages.forEach(function (p) { out.addPage(p); }); total += pages.length;
      }
      b.set(0.95, 'Saving...'); await PT.tick();
      return { bytes: await PT.saveDoc(out), pages: total };
    }, { cancel: true });
    if (!res) return;
    PT.result(host, { title: 'Your merged PDF is ready', files: [{ name: 'merged.pdf', blob: PT.pdfBlob(res.bytes) }],
      stats: [{ k: 'files merged', v: String(files.length) }, { k: 'pages', v: String(res.pages) }, { k: 'size', v: PT.fmtBytes(res.bytes.length) }],
      note: files.some(function (f) { return f.info.password; }) ? 'Password-protected inputs were unlocked with the passwords you typed, so the merged file is not protected. Use Protect PDF to add a password again.' : null });
  }
  build();
})();

},
"split":function(){
/* SPLIT PDF - extract pages, split by ranges, every N pages, or one file per page. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, mode = 'extract', grid, syncing = false;
    var modes = [
      ['extract', 'Extract pages', 'Pick pages (click them or type ranges). They are saved together as ONE new PDF.'],
      ['ranges', 'Split by ranges', 'Each comma-separated range becomes its own PDF. Example: 1-3, 4-6, 7-last makes three files.'],
      ['every', 'Every N pages', 'Cut the document into equal parts of N pages (the last part may be shorter).'],
      ['each', 'Every page', 'Save each page as its own PDF.'],
      ['remove', 'Remove pages', 'Delete the pages you pick; the rest is saved as one PDF.']];
    var seg = h('div', { class: 'seg', role: 'tablist' });
    var desc = h('p', { class: 'hint', style: { margin: '8px 0 12px' } });
    var rangeIn = h('input', { class: 'in', value: '1', 'aria-label': 'Pages or ranges', oninput: fromText });
    var everyIn = h('input', { class: 'in', type: 'number', min: '1', max: String(n), value: String(Math.min(n, 2)), 'aria-label': 'Pages per file' });
    var fRange = h('div', { class: 'fld' }, h('label', null, 'Pages (e.g. 1-3, 5, 8-last, odd, even)'), rangeIn);
    var fEvery = h('div', { class: 'fld' }, h('label', null, 'Pages per file'), everyIn);
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Split PDF');
    function setMode(m) {
      mode = m;
      Array.from(seg.children).forEach(function (b) { b.classList.toggle('on', b.dataset.m === m); b.setAttribute('aria-selected', b.dataset.m === m); });
      desc.textContent = modes.filter(function (x) { return x[0] === m; })[0][2];
      fRange.hidden = !(m === 'extract' || m === 'ranges' || m === 'remove'); fEvery.hidden = m !== 'every';
      grid.el.hidden = !(m === 'extract' || m === 'remove');
      go.textContent = m === 'extract' ? 'Extract pages' : m === 'remove' ? 'Remove pages' : 'Split PDF';
      if (m === 'ranges' && /^\d+$/.test(rangeIn.value)) rangeIn.value = '1-' + Math.max(1, Math.ceil(n / 2)) + ', ' + Math.min(n, Math.ceil(n / 2) + 1) + '-last';
    }
    modes.forEach(function (m) { seg.appendChild(h('button', { type: 'button', role: 'tab', 'data-m': m[0], onclick: function () { setMode(m[0]); } }, m[1])); });
    function fromText() {
      if (syncing || (mode !== 'extract' && mode !== 'remove')) return;
      try { syncing = true; grid.set(PT.parseRanges(rangeIn.value, n)); } catch (e) { /* user is still typing */ } finally { syncing = false; }
    }
    var gridHost = h('div');
    grid = PT.PageGrid(gridHost, info, thumbs, { onChange: function (sel) { if (!syncing && (mode === 'extract' || mode === 'remove')) { syncing = true; rangeIn.value = PT.rangeText(sel); syncing = false; } } });
    area.appendChild(seg); area.appendChild(desc); area.appendChild(fRange); area.appendChild(fEvery); area.appendChild(gridHost);
    area.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go));
    setMode('extract'); fromText();     // highlight the page(s) named in the box from the start, so the box and the grid agree

    async function run() {
      var groups, names, base = PT.baseName(info.name);
      try {
        if (mode === 'extract') { var p = PT.parseRanges(rangeIn.value, n); groups = [p]; names = [base + '-pages-' + PT.rangeText(p).replace(/, /g, '_') + '.pdf']; }
        else if (mode === 'remove') {
          var del = new Set(PT.parseRanges(rangeIn.value, n)), keep = []; for (var i = 0; i < n; i++) if (!del.has(i)) keep.push(i);
          if (!keep.length) throw Object.assign(new Error('That would remove every page. Keep at least one.'), { friendly: true });
          groups = [keep]; names = [base + '-removed.pdf'];
        } else if (mode === 'ranges') {
          groups = PT.parseRangeGroups(rangeIn.value, n); names = groups.map(function (g, k) { return base + '-part-' + String(k + 1).padStart(2, '0') + '.pdf'; });
        } else if (mode === 'every') {
          var k2 = parseInt(everyIn.value, 10); if (!(k2 >= 1)) throw Object.assign(new Error('Enter how many pages each file should have (1 or more).'), { friendly: true });
          groups = []; for (var s = 0; s < n; s += k2) { var g = []; for (var q = s; q < Math.min(n, s + k2); q++) g.push(q); groups.push(g); }
          names = groups.map(function (g, k) { return base + '-part-' + String(k + 1).padStart(2, '0') + '.pdf'; });
        } else { groups = []; for (var z = 0; z < n; z++) groups.push([z]); names = groups.map(function (g, k) { return base + '-page-' + String(k + 1).padStart(String(n).length, '0') + '.pdf'; }); }
        if (groups.length > 500) throw Object.assign(new Error('That would create ' + groups.length + ' files. Please choose fewer (max 500).'), { friendly: true });
      } catch (e) { PT.notify(PT.friendlyError(e), 'err'); return; }
      var out = await PT.run('Splitting...', async function (b) {
        var lib = await PT.loadLib(info), res = [];
        for (var i = 0; i < groups.length; i++) {
          b.check(); b.set(i / groups.length, 'File ' + (i + 1) + ' of ' + groups.length); await PT.tick();
          var d = await PDFLib.PDFDocument.create(); d.setProducer('Binjal Halwai PDF Tools');
          (await d.copyPages(lib, groups[i])).forEach(function (pg) { d.addPage(pg); });
          res.push({ name: names[i], blob: PT.pdfBlob(await PT.saveDoc(d)), pages: groups[i].length });
        }
        return res;
      }, { cancel: true });
      if (!out) return;
      PT.result(area, { title: out.length === 1 ? 'Your PDF is ready' : out.length + ' PDF files are ready', files: out, zipName: base + '-split.zip',
        stats: [{ k: 'files', v: String(out.length) }, { k: 'pages in total', v: String(out.reduce(function (a, f) { return a + f.pages; }, 0)) }] });
    }
  });
})();

},
"rotate":function(){
/* ROTATE PDF - rotate all pages or only the selected ones by 90 / 180 / 270 degrees. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, rots = new Array(n).fill(0), scope = 'sel', grid;
    var seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Which pages' },
      h('button', { type: 'button', 'data-s': 'sel', class: 'on', onclick: function () { setScope('sel'); } }, 'Selected pages'),
      h('button', { type: 'button', 'data-s': 'all', onclick: function () { setScope('all'); } }, 'All pages'));
    var info2 = h('p', { class: 'hint' });
    function setScope(s) { scope = s; Array.from(seg.children).forEach(function (b) { b.classList.toggle('on', b.dataset.s === s); }); upd(); }
    function targets() { return scope === 'all' ? Array.from({ length: n }, function (_, i) { return i; }) : grid.selected(); }
    function turn(d) {
      var t = targets(); if (!t.length) { PT.notify('Select the pages to rotate (click them), or choose "All pages".', 'warn'); return; }
      t.forEach(function (i) { rots[i] = ((rots[i] + d) % 360 + 360) % 360; grid.setRot(i, rots[i]); }); upd();
    }
    function changed() { return rots.filter(function (r) { return r; }).length; }
    function upd() { var c = changed(); apply.disabled = !c; info2.textContent = (scope === 'all' ? 'Buttons rotate all ' + n + ' pages.' : grid.count() + ' page(s) selected - buttons rotate only those.') + ' ' + (c ? c + ' page(s) will be changed.' : 'No changes yet.'); }
    var apply = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Rotate PDF');
    var gridHost = h('div');
    grid = PT.PageGrid(gridHost, info, thumbs, { onChange: upd, badge: function (i) { return rots[i] ? rots[i] + '°' : ''; } });
    area.appendChild(h('div', { class: 'bar' },
      h('div', { class: 'grp' }, h('span', { class: 'note' }, 'Rotate:'), seg),
      h('span', { class: 'sep' }),
      h('div', { class: 'grp' },
        h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { turn(-90); } }, '⟲ Left 90°'),
        h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { turn(90); } }, '⟳ Right 90°'),
        h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { turn(180); } }, '180°'),
        h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { rots.fill(0); rots.forEach(function (_, i) { grid.setRot(i, 0); }); upd(); } }, 'Reset'))));
    area.appendChild(info2); area.appendChild(gridHost);
    area.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, apply));
    upd();
    async function run() {
      var out = await PT.run('Rotating pages...', async function (b) {
        var doc = await PT.loadLib(info), pages = doc.getPages();
        pages.forEach(function (p, i) { if (rots[i]) p.setRotation(PDFLib.degrees((((p.getRotation().angle + rots[i]) % 360) + 360) % 360)); });
        b.set(0.8, 'Saving...'); await PT.tick(); return await PT.saveDoc(doc);
      });
      if (!out) return;
      PT.result(area, { title: 'Your rotated PDF is ready', files: [{ name: PT.outName(info.name, 'rotated'), blob: PT.pdfBlob(out) }],
        stats: [{ k: 'pages rotated', v: String(changed()) }, { k: 'size', v: PT.fmtBytes(out.length) }] });
    }
  });
})();

},
"crop":function(){
/* CROP PDF - drag the box on a preview (or type margins in mm) and crop all, some or one page.
   Cropping sets the page's visible area (CropBox + MediaBox). The hidden part stays inside the file. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, cur = 0, F = { l: 0.08, t: 0.08, r: 0.08, b: 0.08 }, pt = { w: 595, h: 842 };   // F = margins as fractions of the page shown
    var stage = h('div', { class: 'stage' }), wrap = h('div', { class: 'stagewrap' }, stage), strip = h('div', { class: 'pgstrip', role: 'tablist', 'aria-label': 'Choose the preview page' });
    var cropEl = h('div', { class: 'crop', title: 'Drag to move' }), inputs = {}, scope = 'all';
    ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach(function (d) {
      var pos = { nw: [0, 0], n: [50, 0], ne: [100, 0], e: [100, 50], se: [100, 100], s: [50, 100], sw: [0, 100], w: [0, 50] }[d];
      cropEl.appendChild(h('span', { class: 'h', 'data-d': d, style: { left: pos[0] + '%', top: pos[1] + '%', cursor: d + '-resize' } }));
    });
    var canvas = null;

    for (var i = 0; i < n; i++) (function (i) {
      var im = h('img', { alt: 'Page ' + (i + 1) }), b = h('button', { type: 'button', role: 'tab', 'aria-label': 'Preview page ' + (i + 1), onclick: function () { go(i); } }, im, h('small', null, String(i + 1)));
      strip.appendChild(b); PT.whenVisible(b, function () { thumbs.get(i + 1).then(function (t) { im.src = t.url; }, function () { }); });
    })(i);

    function mm(v) { return (v * 25.4 / 72); }
    function sync(skip) {
      cropEl.style.left = F.l * 100 + '%'; cropEl.style.top = F.t * 100 + '%'; cropEl.style.right = F.r * 100 + '%'; cropEl.style.bottom = F.b * 100 + '%';
      var m = { t: F.t * pt.h, b: F.b * pt.h, l: F.l * pt.w, r: F.r * pt.w };
      ['t', 'r', 'b', 'l'].forEach(function (k) { if (k !== skip) inputs[k].value = mm(m[k]).toFixed(1); });
    }
    async function go(p) {
      cur = p; Array.from(strip.children).forEach(function (b, k) { b.classList.toggle('on', k === p); });
      var r = await PT.run('Rendering page ' + (p + 1) + '...', function () { return PT.renderPage(info.doc, p + 1, { maxSide: 1000 }); });
      if (!r) return;
      PT.clear(stage); canvas = r.canvas; stage.appendChild(canvas); stage.appendChild(cropEl); pt = { w: r.width, h: r.height }; sync();
    }
    // ---- dragging
    var drag = null;
    cropEl.addEventListener('pointerdown', function (e) {
      var d = e.target.dataset.d || 'move', rect = stage.getBoundingClientRect();
      drag = { d: d, x: e.clientX, y: e.clientY, W: rect.width, H: rect.height, F: Object.assign({}, F) };
      cropEl.setPointerCapture(e.pointerId); e.preventDefault();
    });
    cropEl.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var dx = (e.clientX - drag.x) / drag.W, dy = (e.clientY - drag.y) / drag.H, o = drag.F, d = drag.d, MIN = 0.05;
      var l = o.l, t = o.t, r = o.r, b = o.b;
      if (d === 'move') { dx = PT.clamp(dx, -o.l, o.r); dy = PT.clamp(dy, -o.t, o.b); l = o.l + dx; r = o.r - dx; t = o.t + dy; b = o.b - dy; }
      else {
        if (d.indexOf('w') >= 0) l = PT.clamp(o.l + dx, 0, 1 - o.r - MIN);
        if (d.indexOf('e') >= 0) r = PT.clamp(o.r - dx, 0, 1 - o.l - MIN);
        if (d.indexOf('n') >= 0) t = PT.clamp(o.t + dy, 0, 1 - o.b - MIN);
        if (d.indexOf('s') >= 0) b = PT.clamp(o.b - dy, 0, 1 - o.t - MIN);
      }
      F = { l: l, t: t, r: r, b: b }; sync();
    });
    ['pointerup', 'pointercancel'].forEach(function (ev) { cropEl.addEventListener(ev, function () { drag = null; }); });

    // ---- options
    function numIn(k, label) {
      var i = h('input', { class: 'in', type: 'number', min: '0', step: '0.5', 'aria-label': label + ' margin in millimetres', oninput: function () {
        var v = parseFloat(i.value); if (!(v >= 0)) return;
        var f = (k === 't' || k === 'b') ? v * 72 / 25.4 / pt.h : v * 72 / 25.4 / pt.w;
        var nf = Object.assign({}, F); nf[k] = PT.clamp(f, 0, 0.95);
        if (nf.l + nf.r > 0.95 || nf.t + nf.b > 0.95) return; F = nf; sync(k);
      } });
      inputs[k] = i; return h('div', { class: 'fld' }, h('label', null, label + ' (mm)'), i);
    }
    var pagesIn = h('input', { class: 'in', placeholder: 'e.g. 1-3, 5', value: '1-' + n, 'aria-label': 'Pages to crop', hidden: true });
    var seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Pages to crop' }, [['all', 'All pages'], ['pages', 'Selected pages'], ['one', 'This page only']].map(function (s, k) {
      return h('button', { type: 'button', class: k === 0 ? 'on' : '', onclick: function (e) { scope = s[0]; Array.from(seg.children).forEach(function (b) { b.classList.toggle('on', b === e.currentTarget); }); pagesIn.hidden = scope !== 'pages'; } }, s[1]);
    }));
    var goBtn = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Crop PDF');
    area.appendChild(h('div', { class: 'split2' }, h('div', null, h('p', { class: 'note', style: { margin: '0 0 6px' } }, 'Choose a page to preview, then drag the box (corners and sides resize it).'), strip, wrap),
      h('div', { class: 'opts' }, h('h3', null, 'Crop margins'), h('div', { class: 'row' }, h('div', { style: { flex: '1 1 110px' } }, numIn('t', 'Top')), h('div', { style: { flex: '1 1 110px' } }, numIn('b', 'Bottom'))),
        h('div', { class: 'row' }, h('div', { style: { flex: '1 1 110px' } }, numIn('l', 'Left')), h('div', { style: { flex: '1 1 110px' } }, numIn('r', 'Right'))),
        h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { F = { l: 0, t: 0, r: 0, b: 0 }; sync(); } }, 'Reset'),
        h('div', { class: 'fld', style: { marginTop: '12px' } }, h('div', { class: 'lbl' }, 'Apply to'), seg, pagesIn),
        h('p', { class: 'hint' }, 'The same margins are applied to every chosen page, in proportion to the page shown (pages with other sizes are cropped by the same share). The cropped-away area is hidden, not erased.'),
        h('div', { class: 'row', style: { marginTop: '12px' } }, goBtn))));
    go(0);

    async function run() {
      var idx;
      try { idx = scope === 'all' ? Array.from({ length: n }, function (_, i) { return i; }) : scope === 'one' ? [cur] : PT.parseRanges(pagesIn.value, n); } catch (e) { PT.notify(PT.friendlyError(e), 'err'); return; }
      if (F.l + F.r >= 0.95 || F.t + F.b >= 0.95) { PT.notify('The crop box is too small.', 'warn'); return; }
      var out = await PT.run('Cropping...', async function (b) {
        var doc = await PT.loadLib(info), pages = doc.getPages();
        idx.forEach(function (i, k) {
          if (k % 20 === 0) b.set(k / idx.length, 'Page ' + (i + 1));
          var pg = pages[i], v = PT.viewSize(pg), map = PT.viewMap(v.cb, v.rot);
          var a = map(F.l * v.w, F.t * v.h), c = map((1 - F.r) * v.w, (1 - F.b) * v.h);
          var x = Math.min(a[0], c[0]), y = Math.min(a[1], c[1]), w = Math.abs(a[0] - c[0]), hh = Math.abs(a[1] - c[1]);
          pg.setCropBox(x, y, w, hh); pg.setMediaBox(x, y, w, hh);
        });
        b.set(0.9, 'Saving...'); await PT.tick(); return await PT.saveDoc(doc);
      });
      if (!out) return;
      PT.result(area, { title: 'Your cropped PDF is ready', files: [{ name: PT.outName(info.name, 'cropped'), blob: PT.pdfBlob(out) }], stats: [{ k: 'pages cropped', v: String(idx.length) }, { k: 'size', v: PT.fmtBytes(out.length) }],
        note: 'The removed margins are hidden but still inside the file. If you must remove sensitive content, redact it instead.' });
    }
  });
})();

},
"compress":function(){
/* COMPRESS PDF
   Browsers cannot run Ghostscript, so we do what a browser CAN do safely:
     1. re-pack the PDF structure (object streams)                          -> lossless
     2. re-compress the embedded JPEG pictures at a lower quality / size    -> lossy, text stays text
     3. "Extreme": turn every page into a picture                           -> text is no longer selectable
   Pictures stored in other formats (PNG-like "Flate" images) are left alone. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  var LEVELS = {
    low: { name: 'Low compression', note: 'Best quality. Only re-packs the file and lightly compresses big photos.', q: 0.88, max: 3500 },
    rec: { name: 'Recommended', note: 'Good balance. Photos are recompressed at medium quality and limited to about 2000 px.', q: 0.72, max: 2000 },
    high: { name: 'High compression', note: 'Smallest file with readable pictures (about 1400 px, lower quality).', q: 0.55, max: 1400 },
    ext: { name: 'Extreme (pages become pictures)', note: 'Every page is turned into an image. Smallest result for scans, but TEXT IS NO LONGER SELECTABLE or searchable. Links and forms are lost.', raster: true, q: 0.5, scale: 1.25 }
  };
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var level = 'rec', desc = h('p', { class: 'hint' }), radios = [];
    var box = h('div', { class: 'opts' }, h('h3', null, 'Compression level'));
    Object.keys(LEVELS).forEach(function (k) {
      var r = h('input', { type: 'radio', name: 'lvl', value: k, checked: k === level, onchange: function () { level = k; show(); } });
      box.appendChild(h('label', { class: 'chk' }, r, h('span', null, h('b', null, LEVELS[k].name))));
    });
    function show() { desc.textContent = LEVELS[level].note; go.textContent = 'Compress PDF'; }
    box.appendChild(desc);
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Compress PDF');
    area.appendChild(box); area.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go)); show();
    area.appendChild(h('p', { class: 'hint' }, 'Current size: ' + PT.fmtBytes(info.size) + '. Tip: PDFs that are mostly text are already small and may not shrink much.'));

    async function jpegToJpeg(bytes, dict, L) {
      var ji = PT.jpegInfo(bytes); if (ji.comps === 4) return null;                    // CMYK: leave alone
      var bmp;
      try { bmp = await createImageBitmap(new Blob([bytes], { type: 'image/jpeg' }), { imageOrientation: 'none' }); } catch (e) { return null; }
      var w = bmp.width, hh = bmp.height;
      var W = dict.get(PDFLib.PDFName.of('Width')), H = dict.get(PDFLib.PDFName.of('Height'));
      if ((W && W.asNumber && W.asNumber() !== w) || (H && H.asNumber && H.asNumber() !== hh)) { bmp.close && bmp.close(); return null; }
      var s = Math.min(1, L.max / Math.max(w, hh)), nw = Math.max(1, Math.round(w * s)), nh = Math.max(1, Math.round(hh * s));
      var c = document.createElement('canvas'); c.width = nw; c.height = nh;
      var ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, nw, nh); ctx.imageSmoothingQuality = 'high'; ctx.drawImage(bmp, 0, 0, nw, nh);
      bmp.close && bmp.close();
      var blob = await PT.canvasToBlob(c, 'image/jpeg', L.q); c.width = c.height = 1;
      var nb = new Uint8Array(await blob.arrayBuffer());
      return nb.length < bytes.length * 0.95 ? { bytes: nb, w: nw, h: nh } : null;
    }
    /* The JPEG data of an image stream, or null if the image is not a plain JPEG. Handles [ASCII85 + DCT] chains made by some tools. */
    function dctBytes(obj, N) {
      var f = obj.dict.get(N.of('Filter')), names = f instanceof N ? [f] : (f && f.asArray ? f.asArray() : []);
      if (!names.length || names[names.length - 1] !== N.of('DCTDecode')) return null;
      if (names.length === 1) return obj.contents;
      try {
        var dict = obj.dict.context.obj({ Filter: names.slice(0, -1) });
        return PDFLib.decodePDFRawStream(PDFLib.PDFRawStream.of(dict, obj.contents)).decode();
      } catch (e) { return null; }
    }
    async function structure(b, L) {
      var doc = await PT.loadLib(info), ctx = doc.context, N = PDFLib.PDFName, done = 0, seen = 0, saved = 0;
      var list = ctx.enumerateIndirectObjects().filter(function (e) { return e[1] instanceof PDFLib.PDFRawStream && e[1].dict.get(N.of('Subtype')) === N.of('Image'); });
      for (var i = 0; i < list.length; i++) {
        b.check(); b.set(0.05 + 0.8 * i / Math.max(1, list.length), 'Picture ' + (i + 1) + ' of ' + list.length); if (i % 3 === 0) await PT.tick();
        var ref = list[i][0], obj = list[i][1], d = obj.dict, f = d.get(N.of('Filter'));
        var raw = dctBytes(obj, N); if (!raw) continue;
        var im = d.get(N.of('ImageMask')); if ((im && String(im) === 'true') || d.has(N.of('Decode')) || d.has(N.of('Mask'))) continue;
        seen++;
        try {
          var r = await jpegToJpeg(raw, d, L); if (!r) continue;
          var nd = { Type: 'XObject', Subtype: 'Image', Width: r.w, Height: r.h, ColorSpace: 'DeviceRGB', BitsPerComponent: 8, Filter: 'DCTDecode' };
          var sm = d.get(N.of('SMask')); if (sm) nd.SMask = sm;
          ctx.assign(ref, ctx.stream(r.bytes, nd)); done++;
        } catch (e) { console.warn('skip image', e); }
      }
      b.set(0.9, 'Saving...'); await PT.tick();
      return { bytes: await PT.saveDoc(doc), pictures: seen, changed: done };
    }
    async function raster(b, L) {
      var out = await PDFLib.PDFDocument.create(); out.setProducer('Binjal Halwai PDF Tools');
      for (var p = 1; p <= info.numPages; p++) {
        b.check(); b.set((p - 1) / info.numPages, 'Page ' + p + ' of ' + info.numPages); await PT.tick();
        var r = await PT.renderPage(info.doc, p, { scale: L.scale });
        var blob = await PT.canvasToBlob(r.canvas, 'image/jpeg', L.q), img = await out.embedJpg(new Uint8Array(await blob.arrayBuffer()));
        var page = out.addPage([r.width, r.height]); page.drawImage(img, { x: 0, y: 0, width: r.width, height: r.height }); r.canvas.width = r.canvas.height = 1;
      }
      b.set(0.97, 'Saving...'); await PT.tick();
      return { bytes: await PT.saveDoc(out), pictures: info.numPages, changed: info.numPages };
    }
    async function run() {
      var L = LEVELS[level];
      var res = await PT.run('Compressing...', function (b) { return L.raster ? raster(b, L) : structure(b, L); }, { cancel: true });
      if (!res) return;
      var before = info.size, after = res.bytes.length, smaller = after < before * 0.99, pct = Math.round((1 - after / before) * 100);
      var final = smaller ? res.bytes : info.bytes;
      PT.result(area, {
        title: smaller ? 'Your PDF is ' + pct + '% smaller' : 'This PDF could not be made smaller',
        files: [{ name: PT.outName(info.name, smaller ? 'compressed' : 'original'), blob: PT.pdfBlob(final) }],
        stats: [{ k: 'before', v: PT.fmtBytes(before) }, { k: 'after', v: PT.fmtBytes(smaller ? after : before) }, { k: 'pictures recompressed', v: res.changed + ' of ' + res.pictures }],
        note: smaller ? (L.raster ? 'Pages were converted to pictures: text is no longer selectable.' : (res.changed ? '' : 'No JPEG pictures needed changing; only the file structure was optimized.')) :
          'It is already well optimized (mostly text or already-compressed pictures). The original is offered unchanged. Try a stronger level; "Extreme" can shrink scanned documents.'
      });
    }
  });
})();

},
"repair":function(){
/* REPAIR PDF - try to recover a damaged PDF.
   Honest limits: a repair can only save what is still readable. We try, in order:
     1. strip junk before the "%PDF-" header and rebuild the file structure, copying page by page
        (each page is tried on its own, so one bad page does not kill the rest)
     2. if that fails, draw the pages that the viewer engine can still show and rebuild the PDF from pictures
        (text is no longer selectable in that case). */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  function start() {
    PT.clear(host);
    host.appendChild(PT.dropzone({ kind: 'pdf', multiple: false, label: 'Select damaged PDF', hint: 'or drop it here. The file is only read in your browser.', onFiles: function (f) { show(f[0]); } }));
  }
  function show(file) {
    PT.clear(host);
    var raster = h('input', { type: 'checkbox', checked: true });
    var go = h('button', { type: 'button', class: 'btn big', onclick: function () { run(file, raster.checked); } }, 'Repair PDF');
    host.appendChild(h('div', { class: 'fitem', style: { marginBottom: '12px' } }, h('div', { class: 'meta' }, h('div', { class: 'nm' }, file.name), h('div', { class: 'sub' }, PT.fmtBytes(file.size))),
      h('button', { type: 'button', class: 'btn ghost sm', onclick: start }, 'Choose another file')));
    host.appendChild(h('div', { class: 'opts' }, h('h3', null, 'Options'),
      h('label', { class: 'chk' }, raster, 'If gentle repair fails, rebuild the pages as pictures (text will not be selectable)'),
      h('p', { class: 'hint' }, 'Nothing can be recovered from a file that is mostly missing or encrypted without its password. Always keep the original file.')));
    host.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go));
  }
  async function run(file, allowRaster) {
    var log = [], out = null;
    var res = await PT.run('Repairing...', async function (b) {
      PT.ensureEngines(true, true);
      var bytes = await PT.readBytes(file), head = new TextDecoder('latin1').decode(bytes.subarray(0, Math.min(bytes.length, 1048576))), at = head.indexOf('%PDF-');
      if (at > 0) { bytes = bytes.subarray(at); log.push('Removed ' + at + ' bytes of junk before the PDF header.'); }
      else if (at < 0) log.push('No "%PDF-" header found; trying anyway.');
      var tail = new TextDecoder('latin1').decode(bytes.subarray(Math.max(0, bytes.length - 2048)));
      if (tail.indexOf('%%EOF') < 0) log.push('The end of the file (%%EOF marker) is missing: the file looks cut off.');

      // ---- attempt 1: rebuild structure with pdf-lib, page by page
      b.set(0.1, 'Reading the file structure...'); await PT.tick();
      var src = null, password;
      try {
        src = await PDFLib.PDFDocument.load(bytes, { ignoreEncryption: true, throwOnInvalidObject: false, updateMetadata: false });
        if (src.isEncrypted) {
          var pw = await PT.askPassword(file.name, false, 'is password-protected. Type its password so the pages can be read.'); if (pw == null) throw PT.CANCEL();
          src = await PDFLib.PDFDocument.load(bytes, { password: pw, throwOnInvalidObject: false, updateMetadata: false }); password = pw;
        }
      } catch (e) { if (e && e.cancelled) throw e; log.push('Structure could not be read directly: ' + (e && e.message || e)); src = null; }
      var total = 0, good = 0, bad = [];
      if (src) {
        total = src.getPageCount();
        var dst = await PDFLib.PDFDocument.create(); dst.setProducer('Binjal Halwai PDF Tools');
        for (var i = 0; i < total; i++) {
          b.check(); b.set(0.15 + 0.5 * i / Math.max(1, total), 'Page ' + (i + 1) + ' of ' + total); if (i % 5 === 0) await PT.tick();
          try { var cp = await dst.copyPages(src, [i]); dst.addPage(cp[0]); good++; } catch (e) { bad.push(i + 1); }
        }
        if (good > 0) {
          log.push('Rebuilt the PDF structure and copied ' + good + ' of ' + total + ' pages.');
          out = { bytes: await PT.saveDoc(dst), method: 'Structure rebuilt (text stays text)', total: total, good: good, bad: bad };
          if (bad.length === 0) return out;
          log.push('Pages that could not be copied: ' + bad.join(', ') + '.');
        }
      }
      // ---- attempt 2: pictures of the pages the viewer engine can still draw
      if (!allowRaster) { if (out) return out; throw Object.assign(new Error('Gentle repair did not work and picture rebuild is switched off.'), { friendly: true }); }
      b.set(0.7, 'Trying to draw the pages...'); await PT.tick();
      var info;
      try { info = await PT.openPdf(bytes, { name: file.name, skipHeaderCheck: true, password: password }); }
      catch (e) { if (e && e.cancelled) throw e; if (out) return out; throw Object.assign(new Error('This file is too damaged to read: ' + (e && e.message || e) + '. Nothing could be recovered.'), { friendly: true }); }
      var d2 = await PDFLib.PDFDocument.create(), ok2 = 0, bad2 = [];
      for (var p = 1; p <= info.numPages; p++) {
        b.check(); b.set(0.7 + 0.25 * p / info.numPages, 'Drawing page ' + p + ' of ' + info.numPages); await PT.tick();
        try {
          var r = await PT.renderPage(info.doc, p, { scale: 1.6 }), blob = await PT.canvasToBlob(r.canvas, 'image/jpeg', 0.9), im = await d2.embedJpg(new Uint8Array(await blob.arrayBuffer()));
          d2.addPage([r.width, r.height]).drawImage(im, { x: 0, y: 0, width: r.width, height: r.height }); ok2++; r.canvas.width = r.canvas.height = 1;
        } catch (e) { bad2.push(p); }
      }
      info.destroy();
      if (!ok2) { if (out) return out; throw Object.assign(new Error('No page could be drawn. The file is too damaged to recover.'), { friendly: true }); }
      if (out && out.good >= ok2) return out;                      // gentle result was better
      log.push('Rebuilt ' + ok2 + ' of ' + info.numPages + ' pages as pictures.');
      return { bytes: await PT.saveDoc(d2), method: 'Rebuilt from page pictures (text is not selectable)', total: info.numPages, good: ok2, bad: bad2 };
    }, { cancel: true });
    if (!res) return;
    var lost = res.bad.length;
    PT.result(host, { title: lost ? 'Partly recovered' : 'Your PDF was repaired', files: [{ name: PT.outName(file.name, 'repaired'), blob: PT.pdfBlob(res.bytes) }],
      stats: [{ k: 'pages found', v: String(res.total) }, { k: 'pages recovered', v: String(res.good) }, { k: 'size', v: PT.fmtBytes(res.bytes.length) }],
      note: res.method + '. ' + log.join(' ') + (lost ? ' Pages not recovered: ' + res.bad.join(', ') + '.' : '') + ' Open the result and check every page. Bookmarks, form fields and attachments may be lost.' , onReset: start, resetLabel: 'Repair another file' });
  }
  start();
})();

},
"jpg-to-pdf":function(){
/* JPG TO PDF - images (JPG, PNG, WebP, GIF, BMP) -> one PDF. Reorder, rotate, choose page size. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount(), imgs = [];            // [{id, img, url, rot}]
  var listEl, countEl, goBtn, opt = {};

  function start() {
    PT.clear(host); imgs = []; listEl = null;
    host.appendChild(PT.dropzone({ kind: 'image', multiple: true, label: 'Select images', hint: 'JPG, PNG, WebP, GIF or BMP. Drop several to make a multi-page PDF.', onFiles: add }));
  }
  function ui() {
    if (listEl) return;
    PT.clear(host);
    countEl = h('p', { class: 'note', style: { margin: '0 0 8px' } });
    listEl = h('ul', { class: 'flist', 'aria-label': 'Images, in page order' });
    opt.size = sel('Page size', [['fit', 'Same shape as the image (no margins)'], ['a4', 'A4'], ['letter', 'US Letter'], ['a5', 'A5']]);
    opt.orient = sel('Orientation (fixed sizes)', [['auto', 'Automatic'], ['p', 'Portrait'], ['l', 'Landscape']]);
    opt.margin = sel('Margin', [['0', 'None'], ['10', 'Small (10 mm)'], ['20', 'Big (20 mm)']]);
    opt.margin.querySelector('select').value = '0';
    goBtn = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Convert to PDF');
    var more = PT.dropzone({ kind: 'image', multiple: true, compact: true, label: '+ Add more images', hint: 'or drop images here', onFiles: add });
    host.appendChild(h('div', { class: 'split2' },
      h('div', null, countEl, listEl, more),
      h('div', { class: 'opts' }, h('h3', null, 'PDF options'), opt.size, opt.orient, opt.margin,
        h('p', { class: 'hint' }, 'Images are embedded without re-compressing (JPG and PNG), so quality is kept.'),
        h('div', { class: 'row', style: { marginTop: '10px' } }, goBtn, h('button', { type: 'button', class: 'btn danger', onclick: start }, 'Remove all')))));
    PT.dragSort(listEl, {
      items: function () { return Array.from(listEl.children); }, handle: '.grip', mouseAnywhere: true, horizontal: false,
      onDrop: function (els, before) {
        var f = imgs.find(function (x) { return x.id === els[0].dataset.id; }); imgs = imgs.filter(function (x) { return x !== f; });
        imgs.splice(before ? imgs.findIndex(function (x) { return x.id === before.dataset.id; }) : imgs.length, 0, f); render();
      }
    });
  }
  function sel(label, opts) {
    var s = h('select', { class: 'in', 'aria-label': label }, opts.map(function (o) { return h('option', { value: o[0] }, o[1]); }));
    var w = h('div', { class: 'fld' }, h('label', null, label), s); w.get = function () { return s.value; }; return w;
  }
  function val(w) { return w.querySelector('select').value; }

  async function add(list) {
    var got = await PT.run('Reading images...', async function (b) {
      var out = [];
      for (var i = 0; i < list.length; i++) {
        b.set(i / list.length, list[i].name); await PT.tick();
        try { var img = await PT.readImage(list[i]); out.push({ id: PT.uid(), img: img, url: PT.imageThumb(img), rot: 0 }); }
        catch (e) { PT.notify('"' + list[i].name + '": ' + PT.friendlyError(e), 'err'); }
      }
      return out;
    });
    if (!got || !got.length) return;
    ui(); imgs = imgs.concat(got); render();
  }
  function render() {
    PT.clear(listEl);
    imgs.forEach(function (it, i) {
      var im = h('img', { class: 'thumb', src: it.url, alt: '', style: { transform: it.rot ? 'rotate(' + it.rot + 'deg)' : '', objectFit: 'contain' } });
      listEl.appendChild(h('li', { class: 'fitem', 'data-id': it.id },
        h('span', { class: 'grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, '\u2807'), im,
        h('div', { class: 'meta' }, h('div', { class: 'nm', title: it.img.name }, (i + 1) + '. ' + it.img.name), h('div', { class: 'sub' }, it.img.width + ' \u00d7 ' + it.img.height + ' px \u00b7 ' + PT.fmtBytes(it.img.bytes.length))),
        h('button', { type: 'button', class: 'iconbtn', title: 'Rotate left', 'aria-label': 'Rotate ' + it.img.name + ' left', onclick: function () { it.rot = (it.rot + 270) % 360; render(); } }, '\u27F2'),
        h('button', { type: 'button', class: 'iconbtn', title: 'Rotate right', 'aria-label': 'Rotate ' + it.img.name + ' right', onclick: function () { it.rot = (it.rot + 90) % 360; render(); } }, '\u27F3'),
        h('button', { type: 'button', class: 'iconbtn', title: 'Move up', 'aria-label': 'Move up', disabled: i === 0, onclick: function () { mv(it, -1); } }, '\u2191'),
        h('button', { type: 'button', class: 'iconbtn', title: 'Move down', 'aria-label': 'Move down', disabled: i === imgs.length - 1, onclick: function () { mv(it, 1); } }, '\u2193'),
        h('button', { type: 'button', class: 'iconbtn del', title: 'Remove', 'aria-label': 'Remove ' + it.img.name, onclick: function () { URL.revokeObjectURL(it.url); imgs = imgs.filter(function (x) { return x !== it; }); if (!imgs.length) start(); else render(); } }, '\u2715')));
    });
    countEl.textContent = imgs.length + ' image' + (imgs.length === 1 ? '' : 's') + ' = ' + imgs.length + ' page' + (imgs.length === 1 ? '' : 's') + '. Drag to reorder.';
    var r = host.querySelector('.result'); if (r) r.remove();
  }
  function mv(it, d) { var i = imgs.indexOf(it), j = i + d; if (j < 0 || j >= imgs.length) return; imgs.splice(i, 1); imgs.splice(j, 0, it); render(); }

  /* Draw `img` (already embedded) turned clockwise by rot, centred and scaled to fit the box (bx,by,bw,bh). */
  function place(page, emb, w, hh, rot, bx, by, bw, bh) {
    var ew = rot % 180 ? hh : w, eh = rot % 180 ? w : hh, s = Math.min(bw / ew, bh / eh), dw = w * s, dh = hh * s;
    var cx = bx + bw / 2, cy = by + bh / 2, x, y;
    if (rot === 90) { x = cx - dh / 2; y = cy + dw / 2; }
    else if (rot === 180) { x = cx + dw / 2; y = cy + dh / 2; }
    else if (rot === 270) { x = cx + dh / 2; y = cy - dw / 2; }
    else { x = cx - dw / 2; y = cy - dh / 2; }
    page.drawImage(emb, { x: x, y: y, width: dw, height: dh, rotate: PDFLib.degrees(-rot) });
  }
  async function run() {
    var size = val(opt.size), orient = val(opt.orient), m = parseFloat(val(opt.margin)) * 72 / 25.4;
    var res = await PT.run('Creating PDF...', async function (b) {
      PT.ensureEngines(true, false);
      var doc = await PDFLib.PDFDocument.create(); doc.setProducer('Binjal Halwai PDF Tools'); doc.setCreator('Binjal Halwai PDF Tools');
      for (var i = 0; i < imgs.length; i++) {
        b.check(); b.set(i / imgs.length, 'Image ' + (i + 1) + ' of ' + imgs.length); await PT.tick();
        var it = imgs[i], emb = await PT.embedImage(doc, it.img), w = it.img.width, hh = it.img.height, rot = it.rot;
        var ew = rot % 180 ? hh : w, eh = rot % 180 ? w : hh, pw, ph;
        if (size === 'fit') { var f = 841.89 / Math.max(ew, eh); pw = ew * f; ph = eh * f; m = 0; }
        else { var P = PT.PAPER[size]; pw = P[0]; ph = P[1]; var land = orient === 'l' || (orient === 'auto' && ew > eh); if (land) { var t = pw; pw = ph; ph = t; } }
        var page = doc.addPage([pw, ph]);
        place(page, emb, w, hh, rot, m, m, pw - 2 * m, ph - 2 * m);
      }
      b.set(0.95, 'Saving...'); await PT.tick();
      return await PT.saveDoc(doc);
    }, { cancel: true });
    if (!res) return;
    PT.result(host, { title: 'Your PDF is ready', files: [{ name: 'images.pdf', blob: PT.pdfBlob(res) }], stats: [{ k: 'pages', v: String(imgs.length) }, { k: 'size', v: PT.fmtBytes(res.length) }] });
  }
  start();
})();

},
"pdf-to-jpg":function(){
/* PDF TO JPG - turn pages into JPG (or PNG) images. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages;
    var fmt = h('select', { class: 'in', 'aria-label': 'Format' }, h('option', { value: 'jpg' }, 'JPG (smaller files)'), h('option', { value: 'png' }, 'PNG (sharp text, bigger files)'));
    var dpi = h('select', { class: 'in', 'aria-label': 'Resolution' }, [[72, '72 DPI - screen, smallest'], [150, '150 DPI - good (default)'], [200, '200 DPI - high'], [300, '300 DPI - print quality']].map(function (o) { return h('option', { value: o[0], selected: o[0] === 150 }, o[1]); }));
    var q = h('select', { class: 'in', 'aria-label': 'JPG quality' }, [[0.6, 'Low (smallest)'], [0.8, 'Medium'], [0.92, 'High (default)'], [0.98, 'Maximum']].map(function (o) { return h('option', { value: o[0], selected: o[0] === 0.92 }, o[1]); }));
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Convert to JPG');
    fmt.addEventListener('change', function () { qf.hidden = fmt.value === 'png'; go.textContent = fmt.value === 'png' ? 'Convert to PNG' : 'Convert to JPG'; });
    var qf = h('div', { class: 'fld' }, h('label', null, 'JPG quality'), q);
    var gridHost = h('div'), grid = PT.PageGrid(gridHost, info, thumbs, { onChange: function (s) { note.textContent = s.length ? s.length + ' selected page(s) will be converted.' : 'No pages selected: all ' + n + ' pages will be converted.'; } });
    var note = h('p', { class: 'hint' }, 'No pages selected: all ' + n + ' pages will be converted.');
    area.appendChild(h('div', { class: 'opts' }, h('div', { class: 'row' }, h('div', { class: 'fld', style: { flex: '1 1 180px' } }, h('label', null, 'Format'), fmt), h('div', { class: 'fld', style: { flex: '1 1 220px' } }, h('label', null, 'Resolution'), dpi), h('div', { style: { flex: '1 1 180px' } }, qf)), note));
    area.appendChild(h('div', { style: { height: '12px' } })); area.appendChild(gridHost);
    area.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go));
    async function run() {
      var pages = grid.selected(); if (!pages.length) pages = Array.from({ length: n }, function (_, i) { return i; });
      var png = fmt.value === 'png', scale = parseInt(dpi.value, 10) / 72, quality = parseFloat(q.value), ext = png ? 'png' : 'jpg', capped = false;
      var out = await PT.run('Converting pages...', async function (b) {
        var res = [], pad = String(n).length;
        for (var k = 0; k < pages.length; k++) {
          b.check(); b.set(k / pages.length, 'Page ' + (pages[k] + 1) + ' (' + (k + 1) + ' of ' + pages.length + ')'); await PT.tick();
          var r = await PT.renderPage(info.doc, pages[k] + 1, { scale: scale });
          if (r.scale < scale - 0.001) capped = true;
          var blob = await PT.canvasToBlob(r.canvas, png ? 'image/png' : 'image/jpeg', quality); r.canvas.width = r.canvas.height = 1;
          res.push({ name: PT.baseName(info.name) + '-page-' + String(pages[k] + 1).padStart(pad, '0') + '.' + ext, blob: blob });
        }
        return res;
      }, { cancel: true });
      if (!out) return;
      PT.result(area, { title: out.length + ' image' + (out.length === 1 ? '' : 's') + ' ready', files: out, zipName: PT.baseName(info.name) + '-images.zip',
        stats: [{ k: 'images', v: String(out.length) }, { k: 'total size', v: PT.fmtBytes(out.reduce(function (a, f) { return a + f.blob.size; }, 0)) }],
        note: capped ? 'Some pages were so large that the resolution was lowered to fit your device\'s limits.' : null });
    }
  });
})();

},
"word-to-pdf":function(){
/* WORD TO PDF - an honest page. Browsers cannot convert Word files faithfully, so we do NOT pretend to.
   We explain why, and give the quickest real ways to get a PDF. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  function card(t, kids) { return h('div', { class: 'opts', style: { marginBottom: '12px' } }, h('h3', null, t), kids); }
  var verdict = h('div');
  // (own file input: this page takes Word files only, for advice - nothing is read or converted)
  var input = h('input', { type: 'file', accept: '.doc,.docx,.odt,.rtf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document', class: 'sr', tabindex: '-1', 'aria-hidden': 'true',
    onchange: function () { var f = input.files[0]; input.value = ''; if (f) advise(f); } });
  var pick = h('button', { type: 'button', class: 'btn pick', onclick: function () { input.click(); } }, 'Select your Word file');
  var drop = h('div', { class: 'drop', style: { marginBottom: '16px' } }, input, pick, h('p', null, 'Optional: choose the file to get advice for it. It is not uploaded and not converted - nothing leaves your device.'));
  drop.addEventListener('dragover', function (e) { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', function () { drop.classList.remove('over'); });
  drop.addEventListener('drop', function (e) { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files[0]) advise(e.dataTransfer.files[0]); });
  function advise(f) {
    PT.clear(verdict);
    var ok = /\.(docx?|odt|rtf)$/i.test(f.name);
    verdict.appendChild(h('div', { class: 'msg ' + (ok ? 'warn' : 'err') },
      ok ? h('span', null, h('b', null, f.name), ' (' + PT.fmtBytes(f.size) + '). A Word file is a layout of fonts, tables, headers, images and fields. Converting it faithfully needs a real word-processing engine (Microsoft Word or LibreOffice). A web page cannot do that reliably, so this tool does not fake it. Use one of the methods below - it takes about 20 seconds.')
        : h('span', null, '"' + f.name + '" is not a Word file. This page is only about Word documents. If you already have a PDF, pick another tool from the menu.')));
  }
  host.appendChild(h('div', { class: 'msg warn', style: { margin: '0 0 16px' } }, h('b', null, 'Honest answer: '), 'converting Word to PDF inside a browser gives wrong fonts, broken tables and missing pictures, so we do not offer a fake converter. Here are the reliable ways to do it, all free or already on your computer.'));
  host.appendChild(drop); host.appendChild(verdict);
  host.appendChild(h('div', { class: 'split2 wide' },
    h('div', null,
      card('1. Microsoft Word (best result)', h('ol', null, h('li', null, 'Open the file in Word.'), h('li', null, 'File \u2192 Save As (or Export) \u2192 choose "PDF" as the type.'), h('li', null, 'Under Options you can keep headings as bookmarks and choose "ISO 19005-1 compliant (PDF/A)" for archiving.'))),
      card('2. Google Docs (any computer, free)', h('ol', null, h('li', null, 'Upload the .docx to Google Drive and open it with Google Docs.'), h('li', null, 'File \u2192 Download \u2192 PDF Document (.pdf).'), h('li', null, 'Check fonts and tables: Google may substitute fonts Word had.'))),
      card('3. LibreOffice (free, works offline, good for many files)', h('ol', null, h('li', null, 'Install LibreOffice (libreoffice.org).'), h('li', null, 'Open the file \u2192 File \u2192 Export as PDF.'),
        h('li', null, 'For a whole folder, open Command Prompt in the folder and run:'),
        h('li', { style: { listStyle: 'none' } }, h('code', { style: { display: 'block', padding: '8px', background: 'var(--ch)', borderRadius: '6px', overflowX: 'auto', whiteSpace: 'pre' } }, '"C:\\Program Files\\LibreOffice\\program\\soffice.exe" --headless --convert-to pdf --outdir out *.docx'))))),
    h('div', null,
      card('After you have the PDF', h('ul', null,
        h('li', null, h('a', { href: '#/merge' }, 'Merge'), ' several PDFs into one'),
        h('li', null, h('a', { href: '#/compress' }, 'Compress'), ' it to email size'),
        h('li', null, h('a', { href: '#/protect' }, 'Protect'), ' it with a password'),
        h('li', null, h('a', { href: '#/watermark' }, 'Watermark'), ' or ', h('a', { href: '#/page-numbers' }, 'number'), ' the pages'),
        h('li', null, h('a', { href: '#/organize' }, 'Organize'), ' pages: reorder, delete, insert'))),
      card('Why not just convert here?', h('p', { class: 'note', style: { margin: 0 } }, 'There are browser libraries that read the text of a .docx, but they ignore headers, footers, page breaks, text boxes and many fonts. A PDF that looks "almost right" can mislead the reader, which is worse than no PDF. If you build office automation (many files), LibreOffice from a script is the dependable route.')))));
})();

},
"watermark":function(){
/* WATERMARK - stamp text or an image on every page (or chosen pages).
   Geometry is worked out in "view points": measured from the top-left corner of the page as you SEE it
   (rotation already applied). The same numbers draw the preview and the real PDF, so they match. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount(), NS = 'http://www.w3.org/2000/svg';
  var FONTS = [['Helvetica', 'Helvetica (Arial)', 'Helvetica', 'Arial, Helvetica, sans-serif', false], ['HelveticaBold', 'Helvetica Bold', 'HelveticaBold', 'Arial, Helvetica, sans-serif', true],
    ['TimesRoman', 'Times', 'TimesRoman', '"Times New Roman", serif', false], ['TimesRomanBold', 'Times Bold', 'TimesRomanBold', '"Times New Roman", serif', true], ['Courier', 'Courier', 'Courier', '"Courier New", monospace', false]];
  var POS = [['tl', 'Top left'], ['tc', 'Top centre'], ['tr', 'Top right'], ['ml', 'Middle left'], ['c', 'Centre'], ['mr', 'Middle right'], ['bl', 'Bottom left'], ['bc', 'Bottom centre'], ['br', 'Bottom right']];

  /* centres (view points) of every stamp for one page. ew/eh = size of the stamp's bounding box after rotation */
  function centers(pos, vw, vh, ew, eh, margin) {
    if (pos === 'tile') {
      var sx = ew * 1.5 + 20, sy = eh * 1.8 + 30, out = [], row = 0;
      for (var y = sy / 2; y < vh + sy / 2; y += sy, row++) for (var x = (row % 2 ? 0 : sx / 2); x < vw + sx / 2; x += sx) out.push([x, y]);
      return out;
    }
    var c = { tl: ['l', 't'], tc: ['c', 't'], tr: ['r', 't'], ml: ['l', 'm'], c: ['c', 'm'], mr: ['r', 'm'], bl: ['l', 'b'], bc: ['c', 'b'], br: ['r', 'b'] }[pos];
    var x0 = c[0] === 'l' ? margin + ew / 2 : c[0] === 'r' ? vw - margin - ew / 2 : vw / 2, y0 = c[1] === 't' ? margin + eh / 2 : c[1] === 'b' ? vh - margin - eh / 2 : vh / 2;
    return [[x0, y0]];
  }
  function bbox(w, hh, a) { var r = a * Math.PI / 180, c = Math.abs(Math.cos(r)), s = Math.abs(Math.sin(r)); return [w * c + hh * s, w * s + hh * c]; }

  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, mode = 'text', pos = 'c', logo = null, vw = 595, vh = 842;
    var o = {};
    o.text = h('textarea', { class: 'in', rows: '2', 'aria-label': 'Watermark text', oninput: draw }, 'CONFIDENTIAL');
    o.font = h('select', { class: 'in', 'aria-label': 'Font', onchange: draw }, FONTS.map(function (f) { return h('option', { value: f[0] }, f[1]); }));
    o.size = range('Font size (pt)', 8, 250, 64, 1); o.color = h('input', { type: 'color', value: '#d1203f', 'aria-label': 'Colour', oninput: draw });
    o.opacity = range('Opacity (%)', 5, 100, 25, 1); o.rot = range('Rotation (degrees, counter-clockwise)', -180, 180, 45, 5);
    o.scale = range('Image width (% of page)', 5, 100, 40, 1);
    o.pages = h('input', { class: 'in', value: 'all', 'aria-label': 'Pages', placeholder: 'all, or 1-3, 5' });
    function range(label, min, max, val, step) {
      var num = h('input', { class: 'in', type: 'number', min: min, max: max, step: step, value: val, style: { width: '84px' }, 'aria-label': label });
      var r = h('input', { type: 'range', min: min, max: max, step: step, value: val, 'aria-label': label + ' slider' });
      r.oninput = function () { num.value = r.value; draw(); }; num.oninput = function () { if (num.value !== '') { r.value = num.value; draw(); } };
      var w = h('div', { class: 'fld' }, h('label', null, label), h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, r, num)); w.val = function () { return parseFloat(num.value); }; w.set = function (v) { num.value = r.value = v; }; return w;
    }
    var posGrid = h('div', { role: 'radiogroup', 'aria-label': 'Position', style: { display: 'grid', gridTemplateColumns: 'repeat(3,44px)', gap: '4px' } }, POS.map(function (p) {
      return h('button', { type: 'button', role: 'radio', 'data-p': p[0], title: p[1], 'aria-label': p[1], class: 'iconbtn' + (p[0] === pos ? ' on' : ''), style: { width: '44px', height: '30px' }, onclick: function () { setPos(p[0]); } }, '●');
    }));
    var tile = h('label', { class: 'chk', style: { marginTop: '8px' } }, h('input', { type: 'checkbox', onchange: function (e) { setPos(e.target.checked ? 'tile' : 'c'); } }), 'Repeat across the whole page (tiled)');
    function setPos(p) { pos = p; Array.from(posGrid.children).forEach(function (b) { b.classList.toggle('on', b.dataset.p === p); b.style.background = b.dataset.p === p ? 'var(--ac)' : ''; b.style.color = b.dataset.p === p ? 'var(--onac)' : ''; }); tile.firstChild.checked = p === 'tile'; draw(); }
    var logoIn = h('input', { type: 'file', accept: '.png,.jpg,.jpeg,.webp,image/*', 'aria-label': 'Watermark image', onchange: async function () {
      var f = logoIn.files[0]; if (!f) return; var ok = PT.validate([f], 'image', 25); if (!ok.length) return;
      try { logo = await PT.readImage(ok[0]); logo.url = PT.imageThumb(logo); PT.notify('Image loaded.', 'ok', 1500); draw(); } catch (e) { PT.notify(PT.friendlyError(e), 'err'); }
    } });
    var textBox = h('div', null, h('div', { class: 'fld' }, h('label', null, 'Text (several lines allowed)'), o.text), h('div', { class: 'fld' }, h('label', null, 'Font'), o.font), o.size, h('div', { class: 'fld' }, h('label', null, 'Colour'), o.color));
    var imgBox = h('div', { hidden: true }, h('div', { class: 'fld' }, h('label', null, 'Image (PNG with transparency works best)'), logoIn), o.scale);
    var seg = h('div', { class: 'seg', role: 'tablist' }, [['text', 'Text'], ['image', 'Image']].map(function (m, k) {
      return h('button', { type: 'button', role: 'tab', class: k ? '' : 'on', onclick: function (e) { mode = m[0]; Array.from(seg.children).forEach(function (b) { b.classList.toggle('on', b === e.currentTarget); }); textBox.hidden = mode !== 'text'; imgBox.hidden = mode !== 'image'; if (mode === 'image' && o.rot.val() === 45) o.rot.set(0); if (mode === 'text' && o.rot.val() === 0) o.rot.set(45); draw(); } }, m[1]);
    }));
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Add watermark');

    /* ---- live preview on page 1 (SVG drawn in view points) ---- */
    var stage = h('div', { class: 'stage' }), svg = document.createElementNS(NS, 'svg'); svg.setAttribute('class', 'overlay-svg');
    area.appendChild(h('div', { class: 'split2' }, h('div', null, h('p', { class: 'note', style: { margin: '0 0 6px' } }, 'Preview (first page)'), h('div', { class: 'stagewrap' }, stage)),
      h('div', { class: 'opts' }, h('h3', null, 'Watermark'), seg, h('div', { style: { height: '10px' } }), textBox, imgBox, o.opacity, o.rot,
        h('div', { class: 'fld' }, h('div', { class: 'lbl' }, 'Position'), posGrid, tile), h('div', { class: 'fld' }, h('label', null, 'Pages'), o.pages), h('div', { class: 'row' }, go))));
    PT.run('Rendering preview...', function () { return PT.renderPage(info.doc, 1, { maxSide: 900 }); }).then(function (r) {
      if (!r) return; vw = r.width; vh = r.height; stage.appendChild(r.canvas); svg.setAttribute('viewBox', '0 0 ' + vw + ' ' + vh); stage.appendChild(svg); setPos('c');
    });
    function draw() {
      if (!svg.getAttribute('viewBox')) return;
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var a = o.rot.val() || 0, op = (o.opacity.val() || 25) / 100, f = FONTS.filter(function (x) { return x[0] === o.font.value; })[0];
      if (mode === 'text') {
        var lines = o.text.value.split(/\r?\n/).filter(function (x) { return x.length; }); if (!lines.length) return;
        var sz = o.size.val() || 64, ctx = document.createElement('canvas').getContext('2d'); ctx.font = (f[4] ? 'bold ' : '') + sz + 'px ' + f[3];
        var tw = Math.max.apply(null, lines.map(function (l) { return ctx.measureText(l).width; })), th = lines.length * sz * 1.2, bb = bbox(tw, th, a);
        centers(pos, vw, vh, bb[0], bb[1], 24).forEach(function (c) {
          var g = document.createElementNS(NS, 'g'); g.setAttribute('transform', 'translate(' + c[0] + ' ' + c[1] + ') rotate(' + (-a) + ')'); g.setAttribute('opacity', op);
          lines.forEach(function (l, i) { var t = document.createElementNS(NS, 'text'); t.setAttribute('x', 0); t.setAttribute('y', -th / 2 + i * sz * 1.2 + sz * 0.6); t.setAttribute('text-anchor', 'middle'); t.setAttribute('dominant-baseline', 'middle'); t.setAttribute('font-size', sz); t.setAttribute('font-family', f[3]); if (f[4]) t.setAttribute('font-weight', 'bold'); t.setAttribute('fill', o.color.value); t.textContent = l; g.appendChild(t); });
          svg.appendChild(g);
        });
      } else if (logo) {
        var w = vw * (o.scale.val() || 40) / 100, hh = w * logo.height / logo.width, bb2 = bbox(w, hh, a);
        centers(pos, vw, vh, bb2[0], bb2[1], 24).forEach(function (c) {
          var im = document.createElementNS(NS, 'image'); im.setAttribute('href', logo.url); im.setAttribute('x', -w / 2); im.setAttribute('y', -hh / 2); im.setAttribute('width', w); im.setAttribute('height', hh);
          im.setAttribute('opacity', op); im.setAttribute('transform', 'translate(' + c[0] + ' ' + c[1] + ') rotate(' + (-a) + ')'); svg.appendChild(im);
        });
      }
    }

    async function run() {
      var idx; try { idx = /^\s*all\s*$/i.test(o.pages.value) ? Array.from({ length: n }, function (_, i) { return i; }) : PT.parseRanges(o.pages.value, n); } catch (e) { PT.notify(PT.friendlyError(e), 'err'); return; }
      var text = o.text.value.replace(/\s+$/, ''); if (mode === 'text' && !text.trim()) { PT.notify('Type the watermark text.', 'warn'); return; }
      if (mode === 'image' && !logo) { PT.notify('Choose a watermark image first.', 'warn'); return; }
      var a = o.rot.val() || 0, op = PT.clamp((o.opacity.val() || 25) / 100, 0.05, 1), sz = o.size.val() || 64, col = PT.hexToRgb(o.color.value), fdef = FONTS.filter(function (x) { return x[0] === o.font.value; })[0];
      var out = await PT.run('Adding watermark...', async function (b) {
        var doc = await PT.loadLib(info), pages = doc.getPages(), stamp, lines = text.split(/\r?\n/).filter(function (l) { return l.length; });
        if (mode === 'image') stamp = { img: await PT.embedImage(doc, logo) };
        else {
          var font = await doc.embedFont(PDFLib.StandardFonts[fdef[2]]);
          if (lines.every(function (l) { return PT.canEncode(font, l); })) stamp = { font: font };
          else { var png = await PT.textToPng(lines.join('\n'), { px: 96, color: o.color.value, bold: fdef[4] }); stamp = { img: await doc.embedPng(png.bytes), pngW: png.width * sz / 96, pngH: png.height * sz / 96 }; }
        }
        idx.forEach(function (i, k) {
          if (k % 10 === 0) b.set(k / idx.length, 'Page ' + (i + 1)); b.check();
          var pg = pages[i], v = PT.viewSize(pg), map = PT.viewMap(v.cb, v.rot), phi = a + v.rot, cr = Math.cos(a * Math.PI / 180), sr = Math.sin(a * Math.PI / 180);
          function toView(cx, cy, u, vv) { return [cx + u * cr + vv * sr, cy - u * sr + vv * cr]; }   // local (u right, vv down) -> view
          var w, hh;
          if (stamp.font) { var tw = Math.max.apply(null, lines.map(function (l) { return stamp.font.widthOfTextAtSize(l, sz); })); w = tw; hh = lines.length * sz * 1.2; }
          else if (mode === 'image') { w = v.w * (o.scale.val() || 40) / 100; hh = w * logo.height / logo.width; }
          else { w = stamp.pngW; hh = stamp.pngH; }
          var bb = bbox(w, hh, a);
          centers(pos, v.w, v.h, bb[0], bb[1], 24).forEach(function (c) {
            if (stamp.font) {
              lines.forEach(function (l, li) {
                var lw = stamp.font.widthOfTextAtSize(l, sz), p = toView(c[0], c[1], -lw / 2, -hh / 2 + li * sz * 1.2 + sz * 0.95), u = map(p[0], p[1]);
                pg.drawText(l, { x: u[0], y: u[1], size: sz, font: stamp.font, color: PDFLib.rgb(col.r, col.g, col.b), opacity: op, rotate: PDFLib.degrees(phi) });
              });
            } else { var p2 = toView(c[0], c[1], -w / 2, hh / 2), u2 = map(p2[0], p2[1]); pg.drawImage(stamp.img, { x: u2[0], y: u2[1], width: w, height: hh, opacity: op, rotate: PDFLib.degrees(phi) }); }
          });
        });
        b.set(0.92, 'Saving...'); await PT.tick(); return await PT.saveDoc(doc);
      }, { cancel: true });
      if (!out) return;
      PT.result(area, { title: 'Your watermarked PDF is ready', files: [{ name: PT.outName(info.name, 'watermarked'), blob: PT.pdfBlob(out) }], stats: [{ k: 'pages stamped', v: String(idx.length) }, { k: 'size', v: PT.fmtBytes(out.length) }] });
    }
  });
})();

},
"page-numbers":function(){
/* PAGE NUMBERS - number the pages: position, format, start number, size and colour. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount(), NS = 'http://www.w3.org/2000/svg';
  var FORMATS = [['n', '1, 2, 3'], ['page', 'Page 1'], ['of', 'Page 1 of N'], ['slash', '1 / N'], ['dash', '- 1 -'], ['roman', 'i, ii, iii (roman)'], ['ROMAN', 'I, II, III (roman capitals)']];
  var FONTS = [['Helvetica', 'Helvetica (Arial)', 'Arial, Helvetica, sans-serif'], ['TimesRoman', 'Times', '"Times New Roman", serif'], ['Courier', 'Courier', '"Courier New", monospace']];
  var POS = [['tl', 'Top left'], ['tc', 'Top centre'], ['tr', 'Top right'], ['bl', 'Bottom left'], ['bc', 'Bottom centre'], ['br', 'Bottom right']];
  function roman(num) { var m = [[1000, 'm'], [900, 'cm'], [500, 'd'], [400, 'cd'], [100, 'c'], [90, 'xc'], [50, 'l'], [40, 'xl'], [10, 'x'], [9, 'ix'], [5, 'v'], [4, 'iv'], [1, 'i']], s = ''; if (num < 1) return String(num); m.forEach(function (p) { while (num >= p[0]) { s += p[1]; num -= p[0]; } }); return s; }
  function label(fmt, num, last) {
    switch (fmt) { case 'page': return 'Page ' + num; case 'of': return 'Page ' + num + ' of ' + last; case 'slash': return num + ' / ' + last; case 'dash': return '- ' + num + ' -'; case 'roman': return roman(num); case 'ROMAN': return roman(num).toUpperCase(); default: return String(num); }
  }
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, pos = 'bc', vw = 595, vh = 842, o = {};
    o.fmt = h('select', { class: 'in', 'aria-label': 'Format', onchange: draw }, FORMATS.map(function (f) { return h('option', { value: f[0] }, f[1]); }));
    o.font = h('select', { class: 'in', 'aria-label': 'Font', onchange: draw }, FONTS.map(function (f) { return h('option', { value: f[0] }, f[1]); }));
    o.start = num('Start number', 1, 1, 100000); o.size = num('Font size (pt)', 12, 6, 72); o.margin = num('Distance from edge (mm)', 10, 3, 60);
    o.from = num('First page to number', 1, 1, n); o.to = num('Last page to number', n, 1, n);
    o.color = h('input', { type: 'color', value: '#000000', 'aria-label': 'Colour', oninput: draw });
    o.skip = h('label', { class: 'chk' }, h('input', { type: 'checkbox', onchange: draw }), 'Do not number the first page (cover): numbering starts on page 2 with the start number');
    function num(label, v, min, max) { var i = h('input', { class: 'in', type: 'number', value: v, min: min, max: max, 'aria-label': label, oninput: draw }); var w = h('div', { class: 'fld' }, h('label', null, label), i); w.i = i; return w; }
    var posGrid = h('div', { role: 'radiogroup', 'aria-label': 'Position', style: { display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: '4px', maxWidth: '260px' } }, POS.map(function (p) {
      return h('button', { type: 'button', role: 'radio', 'data-p': p[0], class: 'btn ghost sm', onclick: function () { setPos(p[0]); } }, p[1]);
    }));
    function setPos(p) { pos = p; Array.from(posGrid.children).forEach(function (b) { b.classList.toggle('on', b.dataset.p === p); b.setAttribute('aria-checked', b.dataset.p === p); }); draw(); }
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Add page numbers');
    var stage = h('div', { class: 'stage' }), svg = document.createElementNS(NS, 'svg'); svg.setAttribute('class', 'overlay-svg');
    area.appendChild(h('div', { class: 'split2' }, h('div', null, h('p', { class: 'note', style: { margin: '0 0 6px' } }, 'Preview (first numbered page)'), h('div', { class: 'stagewrap' }, stage)),
      h('div', { class: 'opts' }, h('h3', null, 'Numbering'), h('div', { class: 'fld' }, h('div', { class: 'lbl' }, 'Position'), posGrid), h('div', { class: 'fld' }, h('label', null, 'Format'), o.fmt), o.start,
        h('div', { class: 'row' }, h('div', { style: { flex: '1 1 120px' } }, o.from), h('div', { style: { flex: '1 1 120px' } }, o.to)), o.skip,
        h('div', { class: 'row' }, h('div', { style: { flex: '1 1 120px' } }, o.size), h('div', { style: { flex: '1 1 120px' } }, o.margin)),
        h('div', { class: 'row' }, h('div', { class: 'fld', style: { flex: '1 1 140px' } }, h('label', null, 'Font'), o.font), h('div', { class: 'fld' }, h('label', null, 'Colour'), o.color)), h('div', { class: 'row' }, go))));
    var mm = function (v) { return v * 72 / 25.4; };
    function first() { var f = Math.max(1, parseInt(o.from.i.value, 10) || 1); if (o.skip.firstChild.checked) f = Math.max(f, 2); return f; }
    function text(p) { var f = first(), s = parseInt(o.start.i.value, 10) || 1, last = s + (Math.max(f, parseInt(o.to.i.value, 10) || n) - f); return label(o.fmt.value, s + (p - f), last); }
    PT.run('Rendering preview...', function () { return PT.renderPage(info.doc, 1, { maxSide: 900 }); }).then(function (r) {
      if (!r) return; vw = r.width; vh = r.height; stage.appendChild(r.canvas); svg.setAttribute('viewBox', '0 0 ' + vw + ' ' + vh); stage.appendChild(svg); setPos('bc');
    });
    function anchor(p) { return { x: p.indexOf('l') >= 0 ? 'start' : p.indexOf('r') >= 0 ? 'end' : 'middle' }; }
    function draw() {
      if (!svg.getAttribute('viewBox')) return; while (svg.firstChild) svg.removeChild(svg.firstChild);
      var sz = parseFloat(o.size.i.value) || 12, m = mm(parseFloat(o.margin.i.value) || 10), f = FONTS.filter(function (x) { return x[0] === o.font.value; })[0];
      var t = document.createElementNS(NS, 'text'), x = pos.indexOf('l') >= 0 ? m : pos.indexOf('r') >= 0 ? vw - m : vw / 2, y = pos[0] === 't' ? m + sz * 0.75 : vh - m;
      t.setAttribute('x', x); t.setAttribute('y', y); t.setAttribute('text-anchor', anchor(pos).x); t.setAttribute('font-size', sz); t.setAttribute('font-family', f[2]); t.setAttribute('fill', o.color.value); t.textContent = text(first()); svg.appendChild(t);
    }
    async function run() {
      var f = first(), to = Math.min(n, parseInt(o.to.i.value, 10) || n), s = parseInt(o.start.i.value, 10);
      if (!(s >= 0) && s !== 0) { PT.notify('Enter a start number.', 'warn'); return; }
      if (to < f) { PT.notify('"Last page" must not be before "First page".', 'warn'); return; }
      var sz = parseFloat(o.size.i.value) || 12, m = mm(parseFloat(o.margin.i.value) || 10), col = PT.hexToRgb(o.color.value);
      var out = await PT.run('Adding page numbers...', async function (b) {
        var doc = await PT.loadLib(info), font = await doc.embedFont(PDFLib.StandardFonts[o.font.value]), pages = doc.getPages();
        for (var p = f; p <= to; p++) {
          if (p % 15 === 0) { b.set((p - f) / (to - f + 1), 'Page ' + p); await PT.tick(); }
          var pg = pages[p - 1], v = PT.viewSize(pg), map = PT.viewMap(v.cb, v.rot), txt = text(p), w = font.widthOfTextAtSize(txt, sz);
          var vx = pos.indexOf('l') >= 0 ? m : pos.indexOf('r') >= 0 ? v.w - m - w : v.w / 2 - w / 2, vy = pos[0] === 't' ? m + sz * 0.75 : v.h - m;
          // baseline-left point in view -> PDF space; text is rotated with the page so it reads upright
          var u = map(vx, vy); pg.drawText(txt, { x: u[0], y: u[1], size: sz, font: font, color: PDFLib.rgb(col.r, col.g, col.b), rotate: PDFLib.degrees(v.rot) });
        }
        b.set(0.9, 'Saving...'); await PT.tick(); return await PT.saveDoc(doc);
      });
      if (!out) return;
      PT.result(area, { title: 'Your numbered PDF is ready', files: [{ name: PT.outName(info.name, 'numbered'), blob: PT.pdfBlob(out) }], stats: [{ k: 'pages numbered', v: String(to - f + 1) }, { k: 'size', v: PT.fmtBytes(out.length) }] });
    }
  });
})();

},
"edit":function(){
/* =====================================================================
   EDIT PDF - add text, images, shapes, arrows, highlights, white-out boxes and freehand drawings.

   How it works
     * The page is drawn on a canvas by pdf.js. On top sits an SVG whose coordinate system is
       "view points": 1 unit = 1 PDF point, measured from the top-left of the page AS YOU SEE IT.
     * Every thing you add is an object  {id, pg, type, ...}  kept in `objs` (undo/redo = copies of that list).
     * "Save" draws each object into the PDF with pdf-lib. PT.viewMap() converts view points to the PDF's own
       coordinates, so pages that are rotated inside the file still come out right.
     * Your original text and pictures are not changed - new items are drawn on top.
   ===================================================================== */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount(), NS = 'http://www.w3.org/2000/svg';
  var FONT_CSS = { Helvetica: 'Arial, Helvetica, sans-serif', HelveticaBold: 'Arial, Helvetica, sans-serif', TimesRoman: '"Times New Roman", serif', Courier: '"Courier New", monospace' };
  var TOOLS = [['select', '↖ Select / move'], ['text', 'T Text'], ['image', '▣ Image'], ['rect', '▭ Rectangle'], ['ellipse', '◯ Ellipse'], ['line', '╱ Line'], ['arrow', '→ Arrow'], ['pen', '✎ Pen'], ['highlight', '▮ Highlight'], ['whiteout', '▢ White-out']];
  function el(tag, a) { var e = document.createElementNS(NS, tag); if (a) Object.keys(a).forEach(function (k) { e.setAttribute(k, a[k]); }); return e; }

  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var n = info.numPages, cur = 0, vw = 595, vh = 842, objs = [], selId = null, tool = 'select', hist = [], redoS = [], draft = null, drag = null, editing = null, P = { color: '#d1203f', fill: '#ffff00', fillOn: false, sw: 3, size: 20, font: 'Helvetica', opacity: 100 };
    var stage = h('div', { class: 'stage' }), svg = el('svg', { class: 'overlay-svg' }), wrap = h('div', { class: 'stagewrap' }, stage), strip = h('div', { class: 'pgstrip', role: 'tablist', 'aria-label': 'Pages' });
    var fileIn = h('input', { type: 'file', accept: '.png,.jpg,.jpeg,.webp,.gif,.bmp,image/*', class: 'sr', tabindex: '-1', 'aria-hidden': 'true', onchange: addImage });
    var measure = document.createElement('canvas').getContext('2d');

    /* ---------- page strip + page rendering ---------- */
    for (var i = 0; i < n; i++) (function (i) {
      var im = h('img', { alt: 'Page ' + (i + 1) }), b = h('button', { type: 'button', role: 'tab', 'aria-label': 'Page ' + (i + 1), onclick: function () { go(i); } }, im, h('small', null, String(i + 1)));
      strip.appendChild(b); PT.whenVisible(b, function () { thumbs.get(i + 1).then(function (t) { im.src = t.url; }, function () { }); });
    })(i);
    async function go(p) {
      closeEditor(true); selId = null; cur = p; Array.from(strip.children).forEach(function (b, k) { b.classList.toggle('on', k === p); });
      var r = await PT.run('Rendering page ' + (p + 1) + '...', function () { return PT.renderPage(info.doc, p + 1, { maxSide: 1100 }); }); if (!r) return;
      vw = r.width; vh = r.height; PT.clear(stage); stage.appendChild(r.canvas); svg.setAttribute('viewBox', '0 0 ' + vw + ' ' + vh); stage.appendChild(svg); stage.style.position = 'relative'; render(); upd();
    }

    /* ---------- history ---------- */
    function snap() { return objs.map(function (o) { return Object.assign({}, o); }); }
    function push(pre) { hist.push(pre || snap()); if (hist.length > 100) hist.shift(); redoS = []; upd(); }
    function undo() { if (!hist.length) return; closeEditor(true); redoS.push(snap()); objs = hist.pop(); if (!find(selId)) selId = null; render(); upd(); }
    function redo() { if (!redoS.length) return; hist.push(snap()); objs = redoS.pop(); if (!find(selId)) selId = null; render(); upd(); }
    function find(id) { return objs.filter(function (o) { return o.id === id; })[0]; }

    /* ---------- geometry helpers ---------- */
    function lines(o) { return String(o.text).split(/\r?\n/); }
    function textW(o) { measure.font = (o.font === 'HelveticaBold' ? 'bold ' : '') + o.size + 'px ' + FONT_CSS[o.font]; return Math.max.apply(null, lines(o).map(function (l) { return measure.measureText(l).width; })); }
    function bbox(o) {
      if (o.type === 'text') return { x: o.x, y: o.y, w: textW(o), h: lines(o).length * o.size * 1.2 };
      if (o.type === 'line' || o.type === 'arrow') return { x: Math.min(o.x1, o.x2), y: Math.min(o.y1, o.y2), w: Math.abs(o.x1 - o.x2), h: Math.abs(o.y1 - o.y2) };
      if (o.type === 'pen') { var xs = o.pts.map(function (p) { return p[0]; }), ys = o.pts.map(function (p) { return p[1]; }); var x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys); return { x: x0, y: y0, w: Math.max.apply(null, xs) - x0, h: Math.max.apply(null, ys) - y0 }; }
      return { x: o.x, y: o.y, w: o.w, h: o.h };
    }
    function toPt(e) { var r = svg.getBoundingClientRect(); return [PT.clamp((e.clientX - r.left) * vw / r.width, 0, vw), PT.clamp((e.clientY - r.top) * vh / r.height, 0, vh)]; }
    function sc() { return svg.getBoundingClientRect().width / vw || 1; }

    /* ---------- drawing objects into the SVG ---------- */
    function shape(o, hit) {
      var op = (o.opacity == null ? 100 : o.opacity) / 100, g = el('g', { 'data-id': o.id, opacity: op });
      var stroke = o.sw > 0 ? o.color : 'none', common = { fill: o.fill || 'none', stroke: stroke, 'stroke-width': o.sw || 0, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
      var s, hitEl;
      if (o.type === 'rect' || o.type === 'highlight' || o.type === 'whiteout') { s = el('rect', Object.assign({ x: o.x, y: o.y, width: o.w, height: o.h }, common)); hitEl = el('rect', { x: o.x, y: o.y, width: o.w, height: o.h }); }
      else if (o.type === 'ellipse') { s = el('ellipse', Object.assign({ cx: o.x + o.w / 2, cy: o.y + o.h / 2, rx: o.w / 2, ry: o.h / 2 }, common)); hitEl = el('ellipse', { cx: o.x + o.w / 2, cy: o.y + o.h / 2, rx: o.w / 2, ry: o.h / 2 }); }
      else if (o.type === 'line' || o.type === 'arrow') {
        s = el('g'); s.appendChild(el('line', { x1: o.x1, y1: o.y1, x2: o.x2, y2: o.y2, stroke: o.color, 'stroke-width': o.sw, 'stroke-linecap': 'round' }));
        if (o.type === 'arrow') arrowHead(o).forEach(function (l) { s.appendChild(el('line', { x1: l[0], y1: l[1], x2: l[2], y2: l[3], stroke: o.color, 'stroke-width': o.sw, 'stroke-linecap': 'round' })); });
        hitEl = el('line', { x1: o.x1, y1: o.y1, x2: o.x2, y2: o.y2 });
      } else if (o.type === 'pen') { var d = 'M' + o.pts.map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' L'); s = el('path', { d: d, fill: 'none', stroke: o.color, 'stroke-width': o.sw, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }); hitEl = el('path', { d: d, fill: 'none' }); }
      else if (o.type === 'text') {
        s = el('g'); lines(o).forEach(function (l, k) { var t = el('text', { x: o.x, y: o.y + o.size * 0.8 + k * o.size * 1.2, 'font-size': o.size, 'font-family': FONT_CSS[o.font], fill: o.color, 'xml:space': 'preserve' }); if (o.font === 'HelveticaBold') t.setAttribute('font-weight', 'bold'); t.textContent = l; s.appendChild(t); });
        var b = bbox(o); hitEl = el('rect', { x: b.x, y: b.y, width: b.w, height: b.h });
      } else if (o.type === 'image') { s = el('image', { href: o.url, x: o.x, y: o.y, width: o.w, height: o.h, preserveAspectRatio: 'none' }); hitEl = el('rect', { x: o.x, y: o.y, width: o.w, height: o.h }); }
      s.setAttribute('pointer-events', 'none'); g.appendChild(s);
      var filled = (o.type === 'text' || o.type === 'image' || o.fill);
      Object.keys({ a: 1 }); hitEl.setAttribute('fill', filled ? 'transparent' : 'none'); hitEl.setAttribute('stroke', 'transparent'); hitEl.setAttribute('stroke-width', Math.max(o.sw || 0, 14 / sc())); hitEl.setAttribute('pointer-events', filled ? 'all' : 'stroke'); g.appendChild(hitEl);
      return g;
    }
    function arrowHead(o) {
      var a = Math.atan2(o.y2 - o.y1, o.x2 - o.x1), L = 6 * o.sw + 8, r = []; [0.45, -0.45].forEach(function (d) { r.push([o.x2, o.y2, o.x2 - L * Math.cos(a - d), o.y2 - L * Math.sin(a - d)]); }); return r;
    }
    function render() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      objs.filter(function (o) { return o.pg === cur; }).forEach(function (o) { svg.appendChild(shape(o)); });
      if (draft) { var g = shape(draft); g.setAttribute('pointer-events', 'none'); svg.appendChild(g); }
      var o = find(selId); if (o && o.pg === cur && !editing) {
        var b = bbox(o), pad = 3 / sc(), r = 7 / sc();
        svg.appendChild(el('rect', { x: b.x - pad, y: b.y - pad, width: b.w + 2 * pad, height: b.h + 2 * pad, fill: 'none', stroke: '#1d4ed8', 'stroke-width': 1.5 / sc(), 'stroke-dasharray': (5 / sc()) + ' ' + (3 / sc()), 'pointer-events': 'none' }));
        var hs = (o.type === 'line' || o.type === 'arrow') ? [['1', o.x1, o.y1], ['2', o.x2, o.y2]] : (o.type === 'text' || o.type === 'pen') ? [] : [['se', o.x + o.w, o.y + o.h]];
        hs.forEach(function (p) { svg.appendChild(el('circle', { cx: p[1], cy: p[2], r: r, fill: '#fff', stroke: '#1d4ed8', 'stroke-width': 2 / sc(), 'data-h': p[0], style: 'cursor:nwse-resize' })); });
      }
    }

    /* ---------- pointer interaction ---------- */
    svg.addEventListener('pointerdown', function (e) {
      if (editing) { closeEditor(false); }
      e.preventDefault(); var p = toPt(e);
      if (tool === 'select') {
        var hd = e.target.closest && e.target.closest('[data-h]'), g = e.target.closest && e.target.closest('[data-id]'), o = find(selId);
        if (hd && o) { drag = { mode: 'resize', h: hd.getAttribute('data-h'), o: o, pre: snap(), orig: Object.assign({}, o), moved: false }; }
        else if (g) { selId = g.getAttribute('data-id'); o = find(selId); drag = { mode: 'move', o: o, start: p, pre: snap(), orig: Object.assign({}, o), moved: false }; syncProps(); }
        else { selId = null; syncProps(); }
        render(); upd();
      } else if (tool === 'text') { openEditor(null, p); return; }
      else {
        var t = tool, base = { id: PT.uid(), pg: cur, type: t, opacity: P.opacity };
        if (t === 'rect' || t === 'ellipse') Object.assign(base, { x: p[0], y: p[1], w: 0, h: 0, color: P.color, sw: P.sw, fill: P.fillOn ? P.fill : null });
        else if (t === 'highlight') Object.assign(base, { x: p[0], y: p[1], w: 0, h: 0, color: '#000000', sw: 0, fill: '#ffe500', opacity: 35 });
        else if (t === 'whiteout') Object.assign(base, { x: p[0], y: p[1], w: 0, h: 0, color: '#ffffff', sw: 0, fill: '#ffffff', opacity: 100 });
        else if (t === 'line' || t === 'arrow') Object.assign(base, { x1: p[0], y1: p[1], x2: p[0], y2: p[1], color: P.color, sw: P.sw });
        else if (t === 'pen') Object.assign(base, { pts: [p], color: P.color, sw: P.sw });
        draft = base; drag = { mode: 'draw', start: p };
      }
      try { svg.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ }
    });
    svg.addEventListener('pointermove', function (e) {
      if (!drag) return; var p = toPt(e), o = drag.o;
      if (drag.mode === 'draw') {
        var d = draft;
        if (d.type === 'pen') { var l = d.pts[d.pts.length - 1]; if (Math.hypot(p[0] - l[0], p[1] - l[1]) > 1.2) d.pts = d.pts.concat([p]); }
        else if (d.type === 'line' || d.type === 'arrow') { d.x2 = p[0]; d.y2 = p[1]; }
        else { d.x = Math.min(drag.start[0], p[0]); d.y = Math.min(drag.start[1], p[1]); d.w = Math.abs(p[0] - drag.start[0]); d.h = Math.abs(p[1] - drag.start[1]); }
      } else if (drag.mode === 'move') {
        var dx = p[0] - drag.start[0], dy = p[1] - drag.start[1], a = drag.orig;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 2) return; if (!drag.moved) { push(drag.pre); drag.moved = true; }
        if (o.type === 'line' || o.type === 'arrow') { o.x1 = a.x1 + dx; o.y1 = a.y1 + dy; o.x2 = a.x2 + dx; o.y2 = a.y2 + dy; }
        else if (o.type === 'pen') o.pts = a.pts.map(function (q) { return [q[0] + dx, q[1] + dy]; });
        else { o.x = a.x + dx; o.y = a.y + dy; }
      } else if (drag.mode === 'resize') {
        if (!drag.moved) { push(drag.pre); drag.moved = true; }
        if (drag.h === '1') { o.x1 = p[0]; o.y1 = p[1]; } else if (drag.h === '2') { o.x2 = p[0]; o.y2 = p[1]; }
        else { o.w = Math.max(6, p[0] - o.x); o.h = o.type === 'image' ? o.w * drag.orig.h / drag.orig.w : Math.max(6, p[1] - o.y); }
      }
      render();
    });
    function endDrag() {
      if (!drag) return; var d = drag; drag = null;
      if (d.mode === 'draw' && draft) {
        var o = draft, big; draft = null;
        if (o.type === 'pen') big = o.pts.length > 1; else if (o.type === 'line' || o.type === 'arrow') big = Math.hypot(o.x2 - o.x1, o.y2 - o.y1) > 4; else big = o.w > 3 && o.h > 3;
        if (big) { push(); objs.push(o); if (tool !== 'pen') { setTool('select'); selId = o.id; syncProps(); } }
      }
      render(); upd();
    }
    svg.addEventListener('pointerup', endDrag); svg.addEventListener('pointercancel', endDrag);
    svg.addEventListener('dblclick', function (e) { var g = e.target.closest && e.target.closest('[data-id]'), o = g && find(g.getAttribute('data-id')); if (o && o.type === 'text') { selId = o.id; openEditor(o, [o.x, o.y]); } });

    /* ---------- inline text editor ---------- */
    function openEditor(o, p) {
      closeEditor(true); var size = o ? o.size : P.size, sc2 = sc();
      var ta = h('textarea', { 'aria-label': 'Text', rows: '1', style: { position: 'absolute', left: (p[0] * sc2) + 'px', top: (p[1] * sc2) + 'px', minWidth: '140px', fontSize: (size * sc2) + 'px', lineHeight: '1.2', fontFamily: FONT_CSS[o ? o.font : P.font], color: o ? o.color : P.color, background: 'rgba(255,255,255,.85)', border: '1px dashed #1d4ed8', padding: '0', margin: '0', outline: 'none', resize: 'both', overflow: 'hidden', zIndex: 5 } });
      ta.value = o ? o.text : ''; editing = { o: o, p: p, ta: ta }; stage.appendChild(ta); setTimeout(function () { ta.focus(); ta.select(); }, 20);
      ta.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.preventDefault(); closeEditor(true); } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); closeEditor(false); } e.stopPropagation(); });
      ta.addEventListener('blur', function () { setTimeout(function () { if (editing && editing.ta === ta) closeEditor(false); }, 0); });
      render();
    }
    function closeEditor(cancel) {
      if (!editing) return; var ed = editing; editing = null; var txt = ed.ta.value; if (ed.ta.parentNode) ed.ta.parentNode.removeChild(ed.ta);
      if (!cancel && txt.trim()) {
        push();
        if (ed.o) { ed.o.text = txt; } else { var o = { id: PT.uid(), pg: cur, type: 'text', x: ed.p[0], y: ed.p[1], text: txt, size: P.size, color: P.color, font: P.font, opacity: P.opacity, sw: 0 }; objs.push(o); selId = o.id; setTool('select'); }
      }
      render(); upd();
    }

    /* ---------- images ---------- */
    async function addImage() {
      var f = fileIn.files[0]; fileIn.value = ''; if (!f) return; var ok = PT.validate([f], 'image', 25); if (!ok.length) return;
      try {
        var img = await PT.readImage(ok[0]), w = Math.min(vw * 0.35, img.width), o = { id: PT.uid(), pg: cur, type: 'image', img: img, url: PT.imageThumb(img), w: w, h: w * img.height / img.width, opacity: P.opacity, sw: 0 };
        o.x = (vw - o.w) / 2; o.y = (vh - o.h) / 2; push(); objs.push(o); selId = o.id; setTool('select'); render(); upd(); PT.notify('Image added. Drag it into place; use the round handle to resize.', 'ok');
      } catch (e) { PT.notify(PT.friendlyError(e), 'err'); }
    }

    /* ---------- toolbar + properties ---------- */
    var toolBtns = {};
    function setTool(t) { closeEditor(false); tool = t; Object.keys(toolBtns).forEach(function (k) { toolBtns[k].classList.toggle('on', k === t); toolBtns[k].setAttribute('aria-pressed', k === t); }); svg.style.cursor = t === 'select' ? 'default' : t === 'text' ? 'text' : 'crosshair'; if (t !== 'select') selId = null; render(); }
    var tools = h('div', { class: 'edtools', role: 'toolbar', 'aria-label': 'Edit tools' }, TOOLS.map(function (t) {
      var b = h('button', { type: 'button', class: 'btn ghost' + (t[0] === 'select' ? ' on' : ''), 'aria-pressed': t[0] === 'select', onclick: function () { if (t[0] === 'image') { setTool('select'); fileIn.click(); } else setTool(t[0]); } }, t[1]); toolBtns[t[0]] = b; return b;
    }));
    function field(label, ctl) { return h('label', { class: 'note', style: { display: 'flex', alignItems: 'center', gap: '6px' } }, label, ctl); }
    var pc = {};
    pc.color = h('input', { type: 'color', value: P.color, 'aria-label': 'Colour' }); pc.fill = h('input', { type: 'color', value: P.fill, 'aria-label': 'Fill colour' }); pc.fillOn = h('input', { type: 'checkbox', 'aria-label': 'Fill shapes' });
    pc.sw = h('input', { type: 'number', class: 'in', min: '1', max: '40', value: P.sw, style: { width: '64px', minHeight: '34px', padding: '2px 6px' }, 'aria-label': 'Line width' });
    pc.size = h('input', { type: 'number', class: 'in', min: '6', max: '200', value: P.size, style: { width: '70px', minHeight: '34px', padding: '2px 6px' }, 'aria-label': 'Font size' });
    pc.font = h('select', { class: 'in', style: { width: 'auto', minHeight: '34px', padding: '2px 6px' }, 'aria-label': 'Font' }, [['Helvetica', 'Helvetica'], ['HelveticaBold', 'Helvetica Bold'], ['TimesRoman', 'Times'], ['Courier', 'Courier']].map(function (f) { return h('option', { value: f[0] }, f[1]); }));
    pc.opacity = h('input', { type: 'range', min: '10', max: '100', value: P.opacity, style: { width: '90px' }, 'aria-label': 'Opacity' });
    function prop(k, v) { P[k] = v; var o = find(selId); if (!o) return; var pre = snap(); if (k === 'fillOn') o.fill = v ? P.fill : null; else if (k === 'fill') { if (o.fill) o.fill = v; } else o[k] = v; if (['size', 'font'].indexOf(k) >= 0 && o.type !== 'text') return; push(pre); render(); }
    pc.color.oninput = function () { prop('color', pc.color.value); }; pc.fill.oninput = function () { prop('fill', pc.fill.value); }; pc.fillOn.onchange = function () { prop('fillOn', pc.fillOn.checked); };
    pc.sw.onchange = function () { prop('sw', parseFloat(pc.sw.value) || 1); }; pc.size.onchange = function () { prop('size', parseFloat(pc.size.value) || 20); }; pc.font.onchange = function () { prop('font', pc.font.value); }; pc.opacity.onchange = function () { prop('opacity', parseInt(pc.opacity.value, 10)); };
    function syncProps() { var o = find(selId); if (!o) return; if (o.color) pc.color.value = o.color; if (o.type === 'text') { pc.size.value = o.size; pc.font.value = o.font; } if (o.sw) pc.sw.value = o.sw; pc.opacity.value = o.opacity || 100; pc.fillOn.checked = !!o.fill; if (o.fill) pc.fill.value = o.fill; }
    var bUndo = h('button', { type: 'button', class: 'btn ghost sm', onclick: undo, title: 'Undo (Ctrl+Z)' }, 'Undo'), bRedo = h('button', { type: 'button', class: 'btn ghost sm', onclick: redo, title: 'Redo (Ctrl+Y)' }, 'Redo');
    var bDel = h('button', { type: 'button', class: 'btn danger sm', onclick: del, title: 'Delete selected (Delete key)' }, 'Delete selected'), bFront = h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { var o = find(selId); if (o) { push(); objs = objs.filter(function (x) { return x !== o; }).concat(o); render(); } } }, 'Bring to front');
    var bClear = h('button', { type: 'button', class: 'btn ghost sm', onclick: function () { if (objs.some(function (o) { return o.pg === cur; })) { push(); objs = objs.filter(function (o) { return o.pg !== cur; }); selId = null; render(); upd(); } } }, 'Clear this page');
    function del() { var o = find(selId); if (!o) { PT.notify('Select an item first (Select tool, then click it).', 'warn'); return; } push(); objs = objs.filter(function (x) { return x !== o; }); selId = null; render(); upd(); }
    var props = h('div', { class: 'bar' }, field('Colour', pc.color), field('Fill', h('span', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } }, pc.fillOn, pc.fill)), field('Line', pc.sw), field('Text size', pc.size), field('Font', pc.font), field('Opacity', pc.opacity),
      h('span', { class: 'sep' }), bUndo, bRedo, bFront, bDel, bClear);
    var saveBtn = h('button', { type: 'button', class: 'btn big', onclick: save }, 'Save PDF'), count = h('span', { class: 'note' });
    function upd() { bUndo.disabled = !hist.length; bRedo.disabled = !redoS.length; bDel.disabled = !selId; bFront.disabled = !selId; count.textContent = objs.length ? objs.length + ' item(s) added (' + objs.filter(function (o) { return o.pg === cur; }).length + ' on this page)' : 'Nothing added yet.'; saveBtn.disabled = !objs.length; }
    area.appendChild(fileIn); area.appendChild(tools); area.appendChild(props); area.appendChild(strip); area.appendChild(wrap);
    area.appendChild(h('p', { class: 'hint' }, 'Tip: pick a tool, then click or drag on the page. Use Select to move items; double-click text to edit it. White-out hides what is under it visually (the original is not erased from the file).'));
    area.appendChild(h('div', { class: 'row', style: { marginTop: '12px' } }, saveBtn, count));
    document.addEventListener('keydown', function (e) {
      if (!document.body.contains(stage) || editing || document.querySelector('.ov')) return; var tg = e.target.tagName; if (tg === 'INPUT' || tg === 'TEXTAREA' || tg === 'SELECT') return;
      var ctrl = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
      if (e.key === 'Delete' || e.key === 'Backspace') { if (selId) { e.preventDefault(); del(); } } else if (e.key === 'Escape') { selId = null; setTool('select'); } else if (ctrl && k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); } else if (ctrl && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); redo(); }
    });
    window.addEventListener('resize', function () { render(); });
    go(0);

    /* ---------- write everything into the PDF ---------- */
    async function save() {
      closeEditor(false);
      var out = await PT.run('Saving your edits...', async function (b) {
        var doc = await PT.loadLib(info), pages = doc.getPages(), fonts = {}, rgb = function (c) { var v = PT.hexToRgb(c); return PDFLib.rgb(v.r, v.g, v.b); }, done = 0;
        async function font(name) { return fonts[name] || (fonts[name] = await doc.embedFont(PDFLib.StandardFonts[name])); }
        var pagesWith = Array.from(new Set(objs.map(function (o) { return o.pg; })));
        for (var pi = 0; pi < pagesWith.length; pi++) {
          var p = pagesWith[pi], pg = pages[p], v = PT.viewSize(pg), map = PT.viewMap(v.cb, v.rot), phi = PDFLib.degrees(v.rot);
          b.check(); b.set(pi / pagesWith.length, 'Page ' + (p + 1)); await PT.tick();
          var list = objs.filter(function (o) { return o.pg === p; });
          for (var oi = 0; oi < list.length; oi++) {
            var o = list[oi], op = (o.opacity == null ? 100 : o.opacity) / 100, u, bb;
            if (o.type === 'text') {
              var f = await font(o.font), ls = lines(o), okTxt = ls.every(function (l) { return PT.canEncode(f, l); });
              if (okTxt) ls.forEach(function (l, k) { u = map(o.x, o.y + o.size * 0.8 + k * o.size * 1.2); pg.drawText(l, { x: u[0], y: u[1], size: o.size, font: f, color: rgb(o.color), opacity: op, rotate: phi }); });
              else { var png = await PT.textToPng(o.text, { px: 96, color: o.color, bold: o.font === 'HelveticaBold' }), im = await doc.embedPng(png.bytes), w = png.width * o.size / 96, hh = png.height * o.size / 96; u = map(o.x, o.y + hh); pg.drawImage(im, { x: u[0], y: u[1], width: w, height: hh, opacity: op, rotate: phi }); }
            } else if (o.type === 'image') { var im2 = await PT.embedImage(doc, o.img); u = map(o.x, o.y + o.h); pg.drawImage(im2, { x: u[0], y: u[1], width: o.w, height: o.h, opacity: op, rotate: phi }); }
            else if (o.type === 'rect' || o.type === 'highlight' || o.type === 'whiteout') { u = map(o.x, o.y + o.h); var ro = { x: u[0], y: u[1], width: o.w, height: o.h, rotate: phi, opacity: o.fill ? op : undefined, borderOpacity: op }; if (o.fill) ro.color = rgb(o.fill); if (o.sw > 0) { ro.borderColor = rgb(o.color); ro.borderWidth = o.sw; } pg.drawRectangle(ro); }
            else if (o.type === 'ellipse') { u = map(o.x + o.w / 2, o.y + o.h / 2); var eo = { x: u[0], y: u[1], xScale: o.w / 2, yScale: o.h / 2, rotate: phi, opacity: o.fill ? op : undefined, borderOpacity: op }; if (o.fill) eo.color = rgb(o.fill); if (o.sw > 0) { eo.borderColor = rgb(o.color); eo.borderWidth = o.sw; } pg.drawEllipse(eo); }
            else if (o.type === 'line' || o.type === 'arrow') {
              var segs = [[o.x1, o.y1, o.x2, o.y2]]; if (o.type === 'arrow') segs = segs.concat(arrowHead(o));
              segs.forEach(function (s) { var a = map(s[0], s[1]), c = map(s[2], s[3]); pg.drawLine({ start: { x: a[0], y: a[1] }, end: { x: c[0], y: c[1] }, thickness: o.sw, color: rgb(o.color), opacity: op, lineCap: PDFLib.LineCapStyle.Round }); });
            } else if (o.type === 'pen') {
              var pts = o.pts.map(function (q) { return map(q[0], q[1]); });
              for (var k2 = 1; k2 < pts.length; k2++) pg.drawLine({ start: { x: pts[k2 - 1][0], y: pts[k2 - 1][1] }, end: { x: pts[k2][0], y: pts[k2][1] }, thickness: o.sw, color: rgb(o.color), opacity: op, lineCap: PDFLib.LineCapStyle.Round });
            }
            done++;
          }
        }
        b.set(0.93, 'Saving...'); await PT.tick(); return await PT.saveDoc(doc);
      }, { cancel: true });
      if (!out) return;
      PT.result(area, { title: 'Your edited PDF is ready', files: [{ name: PT.outName(info.name, 'edited'), blob: PT.pdfBlob(out) }], stats: [{ k: 'items added', v: String(objs.length) }, { k: 'size', v: PT.fmtBytes(out.length) }] });
    }
  });
})();

},
"protect":function(){
/* PROTECT PDF - encrypt a PDF with a password (AES-256) in your browser. The password is never sent anywhere. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  PT.single(host, { label: 'Select PDF file', hint: 'or drop a PDF here' }, function (info, thumbs, area) {
    var pw = h('input', { class: 'in', type: 'password', autocomplete: 'new-password', 'aria-label': 'Password' });
    var pw2 = h('input', { class: 'in', type: 'password', autocomplete: 'new-password', 'aria-label': 'Repeat password' });
    var show = h('input', { type: 'checkbox', onchange: function () { pw.type = pw2.type = show.checked ? 'text' : 'password'; } });
    var meter = h('p', { class: 'hint' });
    pw.addEventListener('input', function () {
      var v = pw.value, s = (v.length >= 12 ? 2 : v.length >= 8 ? 1 : 0) + (/[A-Z]/.test(v) && /[a-z]/.test(v) ? 1 : 0) + (/\d/.test(v) ? 1 : 0) + (/[^A-Za-z0-9]/.test(v) ? 1 : 0);
      meter.textContent = !v ? '' : s >= 4 ? 'Strong password.' : s >= 3 ? 'Good. Longer is better.' : 'Weak: use 10+ characters with letters, numbers and symbols.';
    });
    var perms = {
      print: h('input', { type: 'checkbox', checked: true }), copy: h('input', { type: 'checkbox' }), edit: h('input', { type: 'checkbox' }),
      annotate: h('input', { type: 'checkbox' }), forms: h('input', { type: 'checkbox', checked: true }), assemble: h('input', { type: 'checkbox' })
    };
    function chk(k, t) { return h('label', { class: 'chk' }, perms[k], t); }
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Protect PDF');
    area.appendChild(h('div', { class: 'split2' },
      h('div', { class: 'opts' }, h('h3', null, 'Password to open the PDF'),
        h('div', { class: 'fld' }, h('label', null, 'Password'), pw), meter, h('div', { class: 'fld' }, h('label', null, 'Repeat password'), pw2),
        h('label', { class: 'chk' }, show, 'Show password'),
        h('p', { class: 'msg warn' }, h('b', null, 'Write it down. '), 'If you forget the password nobody can recover the file - not you, not us. The password never leaves your browser.')),
      h('div', { class: 'opts' }, h('h3', null, 'What people may do once it is open'), chk('print', 'Print'), chk('copy', 'Copy text and images'), chk('edit', 'Change the document'), chk('annotate', 'Add comments / annotations'), chk('forms', 'Fill in form fields'), chk('assemble', 'Insert, delete or rotate pages'),
        h('p', { class: 'hint' }, 'Encryption: AES-256. These permissions are honoured by well-behaved PDF programs; the password itself is the real protection.'))));
    area.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go));
    async function run() {
      if (pw.value.length < 4) { PT.notify('Use a password with at least 4 characters (10 or more is better).', 'warn'); return; }
      if (pw.value !== pw2.value) { PT.notify('The two passwords are not the same.', 'err'); return; }
      var password = pw.value;
      var out = await PT.run('Encrypting...', async function (b) {
        var doc = await PT.loadLib(info);
        b.set(0.5, 'Encrypting with AES-256...'); await PT.tick();
        // The owner password controls permissions. We generate a random one so only the user password opens the file.
        var rnd = crypto.getRandomValues(new Uint8Array(24)), owner = Array.from(rnd, function (x) { return x.toString(16).padStart(2, '0'); }).join('');
        doc.encrypt({ userPassword: password, ownerPassword: owner, algorithm: 'AES-256',
          permissions: { printing: perms.print.checked ? 'highResolution' : false, copying: perms.copy.checked, modifying: perms.edit.checked, annotating: perms.annotate.checked, fillingForms: perms.forms.checked, contentAccessibility: true, documentAssembly: perms.assemble.checked } });
        return await PT.saveDoc(doc, { useObjectStreams: false });
      });
      if (!out) return;
      PT.result(area, { title: 'Your PDF is protected', files: [{ name: PT.outName(info.name, 'protected'), blob: PT.pdfBlob(out) }], stats: [{ k: 'encryption', v: 'AES-256' }, { k: 'size', v: PT.fmtBytes(out.length) }],
        note: 'Test it: open the file and check that it asks for the password. Keep the original unprotected file somewhere safe.' });
    }
  });
})();

},
"unlock":function(){
/* UNLOCK PDF - removes the password from a PDF, but ONLY when you type the correct password.
   We never try to guess or bypass a password. A wrong password is simply rejected. */
(function () {
  'use strict';
  var h = PT.h, host = PT.mount();
  function start() {
    PT.clear(host);
    host.appendChild(h('p', { class: 'note', style: { marginTop: 0 } }, 'This tool removes the password from a PDF you are allowed to open. You must know the password: it cannot crack or bypass protection.'));
    host.appendChild(PT.dropzone({ kind: 'pdf', multiple: false, label: 'Select protected PDF', hint: 'or drop it here', onFiles: function (f) { open(f[0]); } }));
  }
  async function open(file) {
    var bytes = await PT.run('Reading file...', async function () {
      PT.ensureEngines(true, true);
      var u8 = await PT.readBytes(file), probe = await PDFLib.PDFDocument.load(u8, { ignoreEncryption: true, updateMetadata: false, throwOnInvalidObject: false });
      return { u8: u8, encrypted: probe.isEncrypted };
    });
    if (!bytes) return;
    if (!bytes.encrypted) {
      PT.clear(host);
      host.appendChild(h('div', { class: 'msg warn' }, '"' + file.name + '" is not password-protected, so there is nothing to unlock. You can use it with any other tool.'));
      host.appendChild(h('div', { class: 'row', style: { marginTop: '12px' } }, h('button', { type: 'button', class: 'btn ghost', onclick: start }, 'Choose another file')));
      return;
    }
    PT.clear(host);
    var pw = h('input', { class: 'in', type: 'password', autocomplete: 'off', 'aria-label': 'Password', onkeydown: function (e) { if (e.key === 'Enter') run(); } });
    var show = h('input', { type: 'checkbox', onchange: function () { pw.type = show.checked ? 'text' : 'password'; } });
    var msg = h('div');
    var go = h('button', { type: 'button', class: 'btn big', onclick: run }, 'Unlock PDF');
    host.appendChild(h('div', { class: 'fitem', style: { marginBottom: '12px' } }, h('div', { class: 'meta' }, h('div', { class: 'nm' }, file.name), h('div', { class: 'sub' }, 'Protected · ' + PT.fmtBytes(file.size))), h('button', { type: 'button', class: 'btn ghost sm', onclick: start }, 'Choose another file')));
    host.appendChild(h('div', { class: 'opts' }, h('h3', null, 'Enter the password'), h('div', { class: 'fld' }, h('label', null, 'Password'), pw), h('label', { class: 'chk' }, show, 'Show password'), msg));
    host.appendChild(h('div', { class: 'row', style: { marginTop: '14px' } }, go));
    async function run() {
      PT.clear(msg);
      var password = pw.value;
      if (!password) { msg.appendChild(h('p', { class: 'msg err' }, 'Type the password first.')); return; }
      var out = await PT.run('Checking password...', async function (b) {
        // 1. verify the password with pdf.js (it only accepts the right user or owner password)
        try { var info = await PT.openPdf(bytes.u8, { name: file.name, password: password, askPassword: false }); info.destroy(); }
        catch (e) { if (e && e.name === 'PasswordException') { throw Object.assign(new Error('That password is not correct, so the file was not unlocked. Check it and try again (upper/lower case matters).'), { friendly: true }); } throw e; }
        // 2. rewrite the file without encryption
        b.set(0.6, 'Removing the password...'); await PT.tick();
        var doc = await PT.loadLib({ bytes: bytes.u8, password: password });
        return await PT.saveDoc(doc, { useObjectStreams: false });
      });
      if (!out) return;
      PT.result(host, { title: 'Password removed', files: [{ name: PT.outName(file.name, 'unlocked'), blob: PT.pdfBlob(out) }], stats: [{ k: 'size', v: PT.fmtBytes(out.length) }], note: 'Anyone with this file can now open it. Use Protect PDF to add a password again.' });
    }
  }
  start();
})();

}
 };
 var TITLES={"home": "PDF Tools - Free, private PDF tools | Binjal Halwai", "organize": "Organize PDF pages | PDF Tools", "merge": "Merge PDF files | PDF Tools", "split": "Split PDF | PDF Tools", "rotate": "Rotate PDF pages | PDF Tools", "crop": "Crop PDF | PDF Tools", "compress": "Compress PDF | PDF Tools", "repair": "Repair PDF | PDF Tools", "jpg-to-pdf": "JPG to PDF | PDF Tools", "pdf-to-jpg": "PDF to JPG | PDF Tools", "word-to-pdf": "Word to PDF | PDF Tools", "watermark": "Add watermark to PDF | PDF Tools", "page-numbers": "Add page numbers to PDF | PDF Tools", "edit": "Edit PDF | PDF Tools", "protect": "Protect PDF with a password | PDF Tools", "unlock": "Unlock PDF | PDF Tools"};
 function route(){var r=(typeof window.__PDF_ROUTE==='string'?window.__PDF_ROUTE:(location.hash||'#/')).replace(/^#\/?/,'').replace(/\/$/,'');return TOOLJS[r]?r:'home';}
 /* inside AI Photoshop the tool links ask the parent page to load the chosen tool (works from file:// too) */
 document.addEventListener('click',function(e){
   if(window.parent===window) return;
   var a=e.target.closest&&e.target.closest('a[href]'); if(!a) return;
   var href=a.getAttribute('href')||'';
   if(/^#\//.test(href)){ e.preventDefault(); parent.postMessage({pdfNav:href},'*'); return; }
   /* A link that only points inside this page (such as the "Skip to main content" link) must not navigate: this page is shown through
      srcdoc, where a plain #name link would load the whole parent site (the NewsPortal) inside the frame. Move focus there instead. */
   if(href.charAt(0)==='#'){ e.preventDefault(); var id=href.slice(1), t=id&&document.getElementById(id); if(t){ if(!t.hasAttribute('tabindex')) t.setAttribute('tabindex','-1'); t.focus(); if(t.scrollIntoView) t.scrollIntoView(); } return; }
   /* links with an explicit target (the "NewsPortal" link uses _top) are left alone; any other web link opens in a new tab, never inside the frame */
   if(a.target) return;
   if(/^https?:/i.test(a.href)){ e.preventDefault(); window.open(a.href,'_blank','noopener'); }
 },true);
 var cur=route();
 document.getElementById('main').innerHTML=document.getElementById('p-'+cur).innerHTML;
 document.title=TITLES[cur];
 Array.prototype.forEach.call(document.querySelectorAll('#vnav a'),function(a){var on=a.getAttribute('data-r')===cur;a.classList.toggle('on',on);if(on)a.setAttribute('aria-current','page');});
 try{TOOLJS[cur]();}catch(e){console.error(e);}
 /* each page starts clean: reload on navigation so no listeners or files from the previous tool remain */
 window.addEventListener('hashchange',function(){ if(route()!==cur){ if(window.parent!==window) parent.postMessage({pdfNav:location.hash},'*'); else location.reload(); } });
})();
