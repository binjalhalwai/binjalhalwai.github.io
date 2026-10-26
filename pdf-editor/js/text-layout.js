// Deterministic word-wrapping shared by the PDF exporter (pdf-lib metrics) and the Unicode raster fallback (canvas metrics).
export const BASELINE = { helvetica: 0.946, times: 0.9375, courier: 0.8665 }; // baseline offset within a 1.2em line box
export const LINE_H = 1.2;

/** Greedy wrap that mimics CSS `white-space: pre-wrap; overflow-wrap: break-word`. measure(str) -> width. */
export function wrapLines(text, maxW, measure) {
  const out = [];
  for (const para of String(text).replace(/\r/g, '').replace(/\t/g, '    ').split('\n')) {
    if (para === '') { out.push(''); continue; }
    const tokens = para.match(/ +|[^ ]+/g);
    let line = '';
    for (const tok of tokens) {
      if (tok[0] === ' ') { line += tok; continue; }
      const trial = line + tok;
      if (measure(trial) <= maxW) { line = trial; continue; }
      if (line.trim() !== '') out.push(line.replace(/ +$/, ''));
      line = '';
      if (measure(tok) <= maxW) { line = tok; continue; }
      for (const ch of tok) {                       // very long word: break by character
        if (line && measure(line + ch) > maxW) { out.push(line); line = ch; } else line += ch;
      }
    }
    out.push(line.replace(/ +$/, ''));
  }
  return out;
}
