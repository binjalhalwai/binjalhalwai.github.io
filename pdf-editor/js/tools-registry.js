// Single source of truth for the right-sidebar tool list. status: 'ready' | 'soon'.
// kind 'tool' = activates a canvas tool; kind 'action' (default for items with `run`) = opens a dialog / runs a command.
export const SECTIONS = [
  { id: 'edit', title: 'Edit', open: true, items: [
    { id: 'select', label: 'Select / Move', icon: 'cursor', key: 'V', status: 'ready' },
    { id: 'text', label: 'Add Text', icon: 'text', key: 'T', status: 'ready' },
    { id: 'edittext', label: 'Edit existing PDF text', icon: 'text', status: 'ready', hint: 'Click a line of text on the page' },
    { id: 'find', label: 'Find text', icon: 'search', key: 'Ctrl+F', status: 'ready', run: 'find' },
    { id: 'replace', label: 'Find & replace', icon: 'search', status: 'ready', run: 'replace' },
  ] },
  { id: 'annotate', title: 'Annotate', open: true, items: [
    { id: 'highlight', label: 'Highlight', icon: 'highlighter', key: 'H', status: 'ready' },
    { id: 'draw', label: 'Freehand draw', icon: 'pen', key: 'D', status: 'ready' },
    { id: 'underline', label: 'Underline', icon: 'underline', status: 'ready' },
    { id: 'strike', label: 'Strikethrough', icon: 'strike', status: 'ready' },
    { id: 'rect', label: 'Rectangle', icon: 'square', status: 'ready' },
    { id: 'ellipse', label: 'Ellipse', icon: 'circle', status: 'ready' },
    { id: 'line', label: 'Line', icon: 'line', status: 'ready' },
    { id: 'arrow', label: 'Arrow', icon: 'arrow-right', status: 'ready' },
    { id: 'note', label: 'Comment / note', icon: 'note', status: 'ready' },
    { id: 'eraser', label: 'Eraser (removes added items)', icon: 'x', status: 'ready' },
  ] },
  { id: 'images', title: 'Images', open: true, items: [
    { id: 'image', label: 'Add Image…', icon: 'image', key: 'I', status: 'ready', run: 'image' },
  ] },
  { id: 'signature', title: 'Signature', open: true, items: [
    { id: 'signature', label: 'Sign…', icon: 'signature', key: 'S', status: 'ready', run: 'signature' },
  ] },
  { id: 'forms', title: 'Forms', items: [
    { id: 'f-text', label: 'Add text field', icon: 'text', status: 'ready' },
    { id: 'f-checkbox', label: 'Add checkbox', icon: 'check', status: 'ready' },
    { id: 'f-radio', label: 'Add radio button', icon: 'circle', status: 'ready' },
    { id: 'f-dropdown', label: 'Add dropdown', icon: 'chevron-down', status: 'ready' },
    { id: 'f-date', label: 'Add date field', icon: 'calendar', status: 'ready' },
    { label: 'Fill existing PDF forms: just click a field on the page', icon: 'check', status: 'info' },
  ] },
  { id: 'whiteout', title: 'Whiteout / Redaction', items: [
    { id: 'whiteout', label: 'Whiteout (visual cover only)', icon: 'square', status: 'ready' },
    { id: 'blackout', label: 'Blackout (visual cover only)', icon: 'square', status: 'ready' },
    { id: 'redact', label: 'Secure redaction', icon: 'shield', status: 'ready' },
  ] },
  { id: 'links', title: 'Links', items: [
    { id: 'link', label: 'Add link area', icon: 'link', status: 'ready' },
    { id: 'importlinks', label: 'Edit existing links', icon: 'link', status: 'ready', run: 'importlinks' },
  ] },
  { id: 'pages', title: 'Page management', items: [
    { label: 'Rotate left', icon: 'rotate-left', status: 'ready', run: 'rotate-left' },
    { label: 'Rotate right', icon: 'rotate', status: 'ready', run: 'rotate-right' },
    { label: 'Duplicate page(s)', icon: 'copy', status: 'ready', run: 'duplicate' },
    { label: 'Delete page(s)', icon: 'trash', status: 'ready', run: 'delete' },
    { label: 'Move up / earlier', icon: 'arrow-up', status: 'ready', run: 'move-up' },
    { label: 'Move down / later', icon: 'arrow-down', status: 'ready', run: 'move-down' },
    { label: 'Insert blank pages…', icon: 'file-plus', status: 'ready', run: 'blank' },
    { label: 'Merge PDFs…', icon: 'merge', status: 'ready', run: 'merge' },
    { label: 'Images → pages…', icon: 'image', status: 'ready', run: 'img2pdf' },
    { label: 'Extract pages…', icon: 'scissors', status: 'ready', run: 'extract' },
    { label: 'Split PDF…', icon: 'scissors', status: 'ready', run: 'split' },
    { label: 'Crop pages…', icon: 'crop', status: 'ready', run: 'crop' },
  ] },
  { id: 'tools', title: 'PDF tools', items: [
    { label: 'Header, footer & page numbers…', icon: 'text', status: 'ready', run: 'headerfooter' },
    { label: 'Watermark…', icon: 'stamp', status: 'ready', run: 'watermark' },
    { label: 'Compress…', icon: 'download', status: 'ready', run: 'compress' },
    { label: 'Pages → images (PNG/JPG)…', icon: 'image', status: 'ready', run: 'pdf2img' },
    { label: 'PDF → text file', icon: 'text', status: 'ready', run: 'pdf2txt' },
    { label: 'Flatten forms: tick the box in Download', icon: 'info', status: 'info' },
    { label: 'Protect with password', icon: 'lock', status: 'soon', why: 'Needs an encryption library that is not bundled yet' },
  ] },
  { id: 'ocr', title: 'OCR', items: [{ label: 'Recognize text (scanned pages)…', icon: 'text', status: 'ready', run: 'ocr' }] },
  { id: 'help', title: 'Help', items: [{ label: 'Keyboard shortcuts', icon: 'info', status: 'ready', run: 'help', key: '?' }, { label: 'Privacy & limits', icon: 'lock', status: 'ready', run: 'about' }] },
];

// Left-sidebar page actions.
export const PAGE_ACTIONS = [
  { label: 'Add blank page', icon: 'file-plus', run: 'blank' },
  { label: 'Duplicate page', icon: 'copy', run: 'duplicate' },
  { label: 'Delete page', icon: 'trash', run: 'delete' },
  { label: 'Rotate page', icon: 'rotate', run: 'rotate-right' },
  { label: 'Extract pages', icon: 'scissors', run: 'extract' },
];
