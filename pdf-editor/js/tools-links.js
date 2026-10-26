// Import the PDF's existing link annotations as editable link objects (so they can be edited or deleted).
import { state } from './state.js';
import * as history from './history.js';
import { getPageProxy } from './pdf-renderer.js';
import { userToView } from './page-geom.js';
import { insertObject, newLinkObject } from './objects.js';
import { toast } from './utils.js';
import { isSafeUrl } from './pdf-export.js';

export async function importExistingLinks() {
  const doc = state.doc; let added = 0, skipped = 0, pagesDone = 0;
  const t = toast('Reading existing links…', 'info', 60000);
  try {
    for (const m of doc.pages) {
      if (m.kind !== 'pdf' || m.linksImported) continue;
      const pg = await getPageProxy(m), src = doc.sources.get(m.src);
      const anns = (await pg.getAnnotations({ intent: 'display' })).filter(a => a.subtype === 'Link' && a.rect);
      m.linkNums = [];
      for (const a of anns) {
        const num = parseInt(String(a.id), 10);
        const p1 = userToView(m.view, m.rot, a.rect[0], a.rect[1]), p2 = userToView(m.view, m.rot, a.rect[2], a.rect[3]);
        const o = newLinkObject(Math.min(p1[0], p2[0]), Math.min(p1[1], p2[1]), Math.max(4, Math.abs(p1[0] - p2[0])), Math.max(4, Math.abs(p1[1] - p2[1])));
        if (a.url && isSafeUrl(a.url)) { o.url = a.url; }
        else if (a.dest || a.unsafeUrl === undefined) {
          let idx = -1;
          try {
            let dest = a.dest; if (typeof dest === 'string') dest = await src.pdf.getDestination(dest);
            if (Array.isArray(dest) && dest[0] && typeof dest[0] === 'object') idx = await src.pdf.getPageIndex(dest[0]);
          } catch { /* unresolved */ }
          const target = doc.pages.find(p => p.src === m.src && p.srcIndex === idx);
          if (!target) { skipped++; continue; }                       // unsupported link kinds are left untouched in the file
          o.kind = 'page'; o.destPage = target.id;
        } else { skipped++; continue; }
        if (Number.isFinite(num)) m.linkNums.push(num);
        o.orig = true; insertObject(m.id, o); added++;
      }
      m.linksImported = true; pagesDone++;
    }
  } catch (e) { console.warn('link import failed:', e && e.name); toast('Could not read the links of this PDF.', 'error'); }
  finally { t.remove(); }
  if (added || pagesDone) history.commit('Import links');
  toast(added ? `Loaded ${added} existing link${added > 1 ? 's' : ''} (blue dashed boxes). Select one to edit or delete it.${skipped ? ` ${skipped} unsupported link(s) were left unchanged.` : ''}` : 'No editable links found in this document.', added ? 'ok' : 'info', 6000);
}
