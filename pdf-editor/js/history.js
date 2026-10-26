// Undo/redo via compact snapshots of the page/object model (images are referenced by id).
import { state, emit } from './state.js';
const MAX = 100;
let undoStack = [], redoStack = [], present = null;
const snap = () => structuredClone(state.doc.pages);

export function reset() { undoStack = []; redoStack = []; present = state.doc ? snap() : null; state.dirty = false; emit('history'); }
export function commit(label = 'Edit') {
  if (!state.doc) return;
  undoStack.push(present);
  if (undoStack.length > MAX) undoStack.shift();
  present = snap();
  redoStack = [];
  state.dirty = true;
  emit('history', { label });
  emit('changed');
}
function restore() {
  state.doc.pages = structuredClone(present);
  if (!state.selection || !state.doc.pages.some(p => p.objects.some(o => o.id === state.selection))) state.selection = null;
  state.dirty = true;
  emit('doc:restore');
  emit('history');
  emit('changed');
}
export function undo() { if (!undoStack.length) return false; redoStack.push(present); present = undoStack.pop(); restore(); return true; }
export function redo() { if (!redoStack.length) return false; undoStack.push(present); present = redoStack.pop(); restore(); return true; }
export const canUndo = () => undoStack.length > 0;
export const canRedo = () => redoStack.length > 0;
