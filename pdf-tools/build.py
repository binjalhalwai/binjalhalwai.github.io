"""Build PDF Tools from the real source files in pdf-tools/src/.

  py pdf-tools\build.py                 -> writes pdf-tools/dist/pdf-tools.html and pdf-tools/dist/pdf-tools.gz.b64
  py pdf-tools\build.py --write-python  -> also puts the new blob into the PDF_TOOLS_GZ line of nepse_news_hub.py
                                           (only that one line changes; line endings are kept; the original blob is in backup/)
  py pdf-tools\build.py --check         -> rebuild and compare with backup/pdf_tools_original_blob.txt (no files written)
"""
import argparse, base64, gzip, hashlib, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "pdf-tools" / "src"
DIST = ROOT / "pdf-tools" / "dist"
PY = ROOT / "nepse_news_hub.py"

def build_html() -> str:
    tpl = (SRC / "index.template.html").read_text(encoding="utf-8")
    def put(m):
        f = SRC / m.group(1)
        if not f.is_file():
            sys.exit("Missing source file: %s" % f)
        return f.read_text(encoding="utf-8")
    return re.sub(r"@@INLINE:([\w./-]+)@@", put, tpl)

def to_blob(html: str) -> str:
    # mtime=0 makes the output reproducible (same source -> same blob)
    return base64.b64encode(gzip.compress(html.encode("utf-8"), 9, mtime=0)).decode("ascii")

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-python", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    html = build_html()
    if a.check:
        orig = gzip.decompress(base64.b64decode((ROOT / "backup" / "pdf_tools_original_blob.txt").read_text(encoding="utf-8"))).decode("utf-8")
        print("rebuilt HTML sha256 :", sha(html)); print("original HTML sha256:", sha(orig))
        print("IDENTICAL" if html == orig else "DIFFERENT (expected once you have changed the source)"); sys.exit(0)
    blob = to_blob(html)
    DIST.mkdir(exist_ok=True)
    (DIST / "pdf-tools.html").write_text(html, encoding="utf-8", newline="")
    (DIST / "pdf-tools.gz.b64").write_text(blob, encoding="utf-8", newline="")
    print("built %s bytes of HTML -> blob %s chars" % (format(len(html.encode('utf-8')), ","), format(len(blob), ",")))
    if a.write_python:
        raw = PY.read_bytes()   # bytes in, bytes out: nothing but the blob changes, whatever the line endings are
        pat = re.compile(rb'^(PDF_TOOLS_GZ = ")[^"]+(")', re.M)
        if len(pat.findall(raw)) != 1:
            sys.exit("Could not find exactly one PDF_TOOLS_GZ line in nepse_news_hub.py: nothing changed.")
        PY.write_bytes(pat.sub(lambda m: m.group(1) + blob.encode("ascii") + m.group(2), raw, count=1))
        print("updated PDF_TOOLS_GZ in", PY.name)

if __name__ == "__main__":
    main()
