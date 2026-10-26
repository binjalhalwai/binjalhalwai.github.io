// Loads the locally-vendored PDF.js (no CDN) and exposes shared options.
import * as pdfjsLib from '../vendor/pdfjs/pdf.min.mjs';
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/pdf.worker.min.mjs', import.meta.url).href;
export const DOC_OPTS = {
  cMapUrl: new URL('../vendor/pdfjs/cmaps/', import.meta.url).href,
  cMapPacked: true,
  standardFontDataUrl: new URL('../vendor/pdfjs/standard_fonts/', import.meta.url).href,
  isEvalSupported: false,   // works under the strict CSP
  enableXfa: false,
};
export default pdfjsLib;
