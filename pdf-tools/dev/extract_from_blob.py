"""One-time tool: split the original PDF Tools blob (PDF_TOOLS_GZ in nepse_news_hub.py) into real source files.

Usage (from the project root):   python pdf-tools/dev/extract_from_blob.py

Reads  backup/pdf_tools_original_blob.txt  (the untouched original) and writes pdf-tools/src/.
Each inline <script>/<style> body becomes its own file; the HTML keeps a token like @@INLINE:js/tools.js@@ where
the body was. pdf-tools/build.py puts the bodies back, so the round trip is byte-for-byte identical.
"""
import base64, gzip, hashlib, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "pdf-tools" / "src"
# order of the inline blocks in the original file -> file name
NAMES = ["js/theme-init.js", "js/embed-sync.js", "css/pdf-tools.css", "vendor/pdf-lib.js", "vendor/pdfjs.js",
         "vendor/pdfjs-worker-loader.js", "js/common.js", "js/tools.js"]

def main():
    blob = (ROOT / "backup" / "pdf_tools_original_blob.txt").read_text(encoding="utf-8")
    html = gzip.decompress(base64.b64decode(blob)).decode("utf-8")
    blocks = list(re.finditer(r"<(script|style)\b([^>]*)>(.*?)</\1>", html, re.S))
    if len(blocks) != len(NAMES):
        sys.exit(f"Expected {len(NAMES)} inline blocks, found {len(blocks)}: stop (the blob is not the expected one).")
    out, last = [], 0
    for m, name in zip(blocks, NAMES):
        p = SRC / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(m.group(3), encoding="utf-8", newline="")
        out.append(html[last:m.start(3)] + f"@@INLINE:{name}@@")
        last = m.end(3)
    out.append(html[last:])
    (SRC / "index.template.html").write_text("".join(out), encoding="utf-8", newline="")
    print("original HTML sha256:", hashlib.sha256(html.encode("utf-8")).hexdigest())
    print("wrote", len(NAMES) + 1, "files to", SRC)

if __name__ == "__main__":
    main()
