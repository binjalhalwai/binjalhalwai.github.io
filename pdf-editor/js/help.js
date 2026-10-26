import { h, showModal } from './utils.js';
const KEYS = [['Ctrl + Z / Ctrl + Y', 'Undo / redo'], ['Ctrl + S', 'Download PDF'], ['Ctrl + F', 'Find text'], ['Ctrl + P', 'Print'], ['Ctrl + D', 'Duplicate selected item'], ['Delete', 'Delete selected item'], ['Esc', 'Cancel tool / deselect / close find'], ['Arrow keys', 'Nudge selected item (Shift = 10×)'], ['Ctrl + + / − / 0', 'Zoom in / out / fit width'], ['V T D H I S', 'Select, Text, Draw, Highlight, Image, Sign'], ['Click / Ctrl+Click / Shift+Click thumbnail', 'Select page / toggle / range'], ['Drag thumbnail', 'Reorder pages'], ['?', 'This help']];
export function openHelp() {
  showModal({ title: 'Keyboard shortcuts', content: h('dl', { class: 'stats' }, KEYS.flatMap(([k, d]) => [h('dt', {}, k), h('dd', {}, d)])), actions: [{ label: 'Close', value: true, primary: true }] });
}
export function openAbout() {
  showModal({ title: 'Privacy & limits', content: [
    h('p', {}, 'Your PDF is opened and edited entirely in this browser tab. It is never uploaded, and no analytics look at your documents.'),
    h('ul', { class: 'filelist' },
      h('li', {}, 'Whiteout / blackout only cover content visually; the original stays in the file. Use Secure redaction to remove it.'),
      h('li', {}, 'Signatures are visual images, not certificate-based digital signatures.'),
      h('li', {}, 'Editing existing text places a patch and new text over the original; the old text remains underneath.'),
      h('li', {}, 'Password-protected or unusual PDFs may only be saved as a flattened (image) copy.'),
      h('li', {}, 'Password protection (encrypting a PDF) is not available yet.')),
  ], actions: [{ label: 'Close', value: true, primary: true }] });
}
