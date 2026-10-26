// Used ONLY by the single-file build (build-standalone.mjs): the PDF.js worker is embedded and started from a Blob,
// so the page works when opened directly from disk (file://).
import * as pdfjsLib from '../vendor/pdfjs/pdf.min.mjs';
/* global __PDFJS_WORKER_SRC__ */
pdfjsLib.GlobalWorkerOptions.workerSrc = URL.createObjectURL(new Blob([__PDFJS_WORKER_SRC__], { type: 'text/javascript' }));
export const DOC_OPTS = { isEvalSupported: false, enableXfa: false };
export default pdfjsLib;
