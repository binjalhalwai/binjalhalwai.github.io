// Renders the right-sidebar tool list, left-sidebar page actions and the mobile quick bar.
import { state, on, setTool } from './state.js';
import { h, $, icon } from './utils.js';
import { SECTIONS, PAGE_ACTIONS } from './tools-registry.js';
import { ACTIONS, run } from './actions.js';

const buttons = new Map();
const TOOL_IDS = new Set();
for (const s of SECTIONS) for (const it of s.items) if (it.id && !it.run && it.status === 'ready') TOOL_IDS.add(it.id);

export function activate(id) {
  if (!state.doc) return;
  if (ACTIONS[id]) { run(id); return; }
  setTool(state.tool === id && id !== 'select' ? 'select' : id);
}

export function init() {
  const host = $('#tool-sections');
  for (const sec of SECTIONS) {
    const items = h('div', { class: 'titems' });
    const head = h('button', { class: 'thead', type: 'button', 'aria-expanded': String(!!sec.open) }, sec.title, icon('chevron-down'));
    const box = h('div', { class: `tsec${sec.open ? '' : ' collapsed'}` }, head, items);
    head.addEventListener('click', () => { const c = box.classList.toggle('collapsed'); head.setAttribute('aria-expanded', String(!c)); });
    for (const it of sec.items) {
      if (it.status === 'info') { items.append(h('p', { class: 'tinfo' }, icon(it.icon, 'ic tic'), it.label)); continue; }
      const soon = it.status === 'soon';
      const b = h('button', { class: 'titem', type: 'button', disabled: soon, title: soon ? `Coming soon — ${it.why || ''}` : (it.hint || it.label) },
        icon(it.icon, 'ic tic'), h('span', {}, it.label), soon ? h('span', { class: 'soon' }, 'Soon') : (it.key ? h('span', { class: 'k' }, it.key) : null));
      if (!soon) {
        b.addEventListener('click', () => { if (!state.doc) return; if (it.run) { run(it.run); document.body.classList.remove('show-right'); } else { activate(it.id); } });
        if (it.id && !it.run) buttons.set(it.id, b);
      }
      items.append(b);
    }
    host.append(box);
  }
  // Left sidebar page actions
  const foot = $('#page-actions');
  for (const a of PAGE_ACTIONS) {
    const b = h('button', { class: 'mini-btn', type: 'button', title: a.label, 'aria-label': a.label }, icon(a.icon));
    b.addEventListener('click', () => run(a.run));
    foot.append(b);
  }
  // Mobile quick bar
  const q = $('#quickbar');
  for (const [id, label, ic] of [['select', 'Select', 'cursor'], ['text', 'Text', 'text'], ['draw', 'Draw', 'pen'], ['highlight', 'Mark', 'highlighter'], ['image', 'Image', 'image'], ['signature', 'Sign', 'signature']]) {
    const b = h('button', { type: 'button', 'aria-label': label }, icon(ic), label);
    b.addEventListener('click', () => activate(id));
    b.dataset.tool = id; q.append(b);
  }
  const sync = () => {
    for (const [id, b] of buttons) b.classList.toggle('active', id === state.tool);
    q.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.tool === state.tool));
    const v = $('#viewer');
    v.className = v.className.replace(/\btool-\S+/g, '').trim();
    v.classList.add(`tool-${state.tool}`);
    v.classList.toggle('placing', !!state.pending);
  };
  on('tool', sync); on('pending', sync); on('doc:open', sync);
  sync();
}
