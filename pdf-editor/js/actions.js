// Maps action ids from the tool registry to functions. Page actions work on the selected thumbnails (or the current page).
import { state } from './state.js';
import * as ops from './page-ops.js';
import { pickImage } from './tools-image.js';
import { openSignatureDialog } from './tools-signature.js';
import * as pg from './tools-pages.js';
import * as tools from './pdf-tools.js';
import { openOcrDialog } from './ocr.js';
import { openFind } from './find.js';
import { importExistingLinks } from './tools-links.js';
import { openHelp, openAbout } from './help.js';

export const ACTIONS = {
  image: pickImage, signature: openSignatureDialog,
  find: () => openFind('find'), replace: () => openFind('replace'),
  importlinks: importExistingLinks,
  'rotate-left': () => ops.rotatePages(ops.targetIds(), 3),
  'rotate-right': () => ops.rotatePages(ops.targetIds(), 1),
  duplicate: () => ops.duplicatePages(ops.targetIds()),
  delete: () => ops.deletePagesConfirm(ops.targetIds()),
  'move-up': () => ops.moveStep(ops.targetIds(), -1),
  'move-down': () => ops.moveStep(ops.targetIds(), 1),
  blank: pg.openInsertBlankDialog, merge: pg.openMergeDialog, img2pdf: pg.openImagesToPagesDialog,
  extract: pg.openExtractDialog, split: pg.openSplitDialog, crop: pg.openCropDialog,
  headerfooter: pg.openHeaderFooterDialog, watermark: pg.openWatermarkDialog,
  compress: tools.openCompressDialog, pdf2img: tools.openPdfToImagesDialog, pdf2txt: tools.openPdfToTextDialog,
  ocr: openOcrDialog, help: openHelp, about: openAbout,
};
export const run = id => { if (state.doc && ACTIONS[id]) return ACTIONS[id](); };
