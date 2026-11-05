"""Browser tests for PDF Tools (the PDF Tools tab inside AI STUDIO).

Setup (once, Windows):   pip install playwright reportlab pillow pypdf
                         python -m playwright install chromium
Run:   1) build the site:  python nepse_news_hub.py publish
       2) serve it:        python -m http.server 8765 --directory public_site
       3) test it:         python pdf-tools/tests/smoke.py http://localhost:8765/index.html

Prints PASS / FAIL for every check and exits with 1 if anything failed. Nothing is uploaded anywhere: the files used are
small test PDFs and an image created in a temporary folder.
"""
import io, re, sys, tempfile, zipfile, traceback
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/index.html"
TOOLS = ["organize", "merge", "split", "rotate", "crop", "compress", "repair", "jpg-to-pdf", "pdf-to-jpg",
         "word-to-pdf", "watermark", "page-numbers", "edit", "protect", "unlock"]
results = []

def report(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS  " if ok else "FAIL  ") + name + (("  -> " + detail) if detail else ""), flush=True)

# ---------------------------------------------------------------- fixtures
def make_fixtures(d: Path):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    def pdf(path, pages, label):
        c = canvas.Canvas(str(path), pagesize=A4)
        for i in range(pages):
            c.setFont("Helvetica", 28); c.drawString(100, 700, f"{label} page {i + 1}"); c.rect(80, 80, 400, 600); c.showPage()
        c.save()
    pdf(d / "a.pdf", 3, "Alpha"); pdf(d / "b.pdf", 2, "Beta")
    Image.new("RGB", (600, 400), (200, 60, 60)).save(d / "img.jpg")
    w = PdfWriter()
    for pg in PdfReader(str(d / "a.pdf")).pages: w.add_page(pg)
    w.encrypt("secret123"); w.write(open(d / "locked.pdf", "wb"))

# ---------------------------------------------------------------- helpers
def open_pdf_tools(p, label_suffix=""):
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)[:160]))
    pg.goto(BASE); pg.wait_for_timeout(1200)
    pg.get_by_text("AI STUDIO").first.click(); pg.wait_for_timeout(400)
    pg.locator("button.aitool", has_text="PDF Tools").first.click(); pg.wait_for_timeout(1800)
    return b, pg, errors

def tool_frame(pg):
    for f in pg.frames:
        if f != pg.main_frame and (f.url == "about:srcdoc" or "localhost" in f.url or f.url.startswith("http")):
            return f
    return None

def frame_ok(pg):
    f = tool_frame(pg)
    return bool(f) and f.url == "about:srcdoc"

def go_tool(pg, t):
    f = tool_frame(pg)
    f.locator(f"#vnav a[data-r='{t}']").first.click()
    pg.wait_for_timeout(1500)
    return tool_frame(pg)

def upload(pg, f, files, wait_for=None):
    f.locator("input[type=file]").first.set_input_files([str(x) for x in files])
    if wait_for: f.get_by_role("button", name=wait_for).first.wait_for(timeout=20000)
    else: pg.wait_for_timeout(2500)

def click_button(f, name, exact=False):
    f.get_by_role("button", name=name, exact=exact).first.click()

def download(pg, f, prefer_zip=False):
    """Click the download button of the result panel and return the file bytes."""
    f.locator(".result").first.wait_for(timeout=30000)
    btn = None
    if prefer_zip and f.get_by_role("button", name=re.compile("zip", re.I)).count():
        btn = f.get_by_role("button", name=re.compile("zip", re.I)).first
    if btn is None:
        btn = f.get_by_role("button", name=re.compile(r"^Download")).first
    with pg.expect_download(timeout=20000) as dl:
        btn.click()
    return Path(dl.value.path()).read_bytes()

def pages_of(data):
    from pypdf import PdfReader
    return PdfReader(io.BytesIO(data))

# ---------------------------------------------------------------- tests
def test_navigation(p):
    b, pg, errs = open_pdf_tools(p)
    try:
        bad = []
        for t in TOOLS:
            f = go_tool(pg, t)
            h1 = f.evaluate("(document.querySelector('h1')||{}).textContent||''") if f else ""
            if not frame_ok(pg) or not h1.strip(): bad.append(t)
        report("navigation: all 15 tool pages open inside PDF Tools", not bad, ("problems: " + ", ".join(bad)) if bad else "")
        # home cards
        f = tool_frame(pg)
        f.locator("#vnav a[data-r='home']").first.click(); pg.wait_for_timeout(1200); f = tool_frame(pg)
        n = f.locator("#main a.tcard").count()
        bad = []
        for i in range(n):
            f.locator("#main a.tcard").nth(i).click(); pg.wait_for_timeout(900)
            if not frame_ok(pg): bad.append(i)
            tool_frame(pg).locator("#vnav a[data-r='home']").first.click(); pg.wait_for_timeout(900)
        report(f"navigation: {n} home-page cards keep the tool inside PDF Tools", n >= 15 and not bad, f"cards={n} bad={bad}")
    finally:
        b.close()

def test_stray_links(p):
    # 1) the skip link, used the way a keyboard user does (Tab, then Enter)
    b, pg, errs = open_pdf_tools(p)
    try:
        f = tool_frame(pg)
        f.locator("a.skip").focus(); pg.keyboard.press("Enter"); pg.wait_for_timeout(1500)
        ok = frame_ok(pg)
        extra = "" if ok else " (frame now shows: " + (tool_frame(pg).url[:60] if tool_frame(pg) else "?") + ")"
        report("skip link ('Skip to main content') does not load the NewsPortal inside the frame", ok, extra)
    finally:
        b.close()
    # 2) the six links on the Word to PDF page
    for label, href in [("Merge", "merge"), ("Compress", "compress"), ("Protect", "protect"), ("Watermark", "watermark"),
                        ("number", "page-numbers"), ("Organize", "organize")]:
        b, pg, errs = open_pdf_tools(p)
        try:
            f = go_tool(pg, "word-to-pdf")
            f.locator("#main a", has_text=re.compile("^" + label + "$")).first.click(); pg.wait_for_timeout(2000)
            f = tool_frame(pg)
            title = f.title() if f else ""
            ok = frame_ok(pg) and href.replace("-", " ").split()[0] in title.lower().replace("-", " ")
            report(f"Word to PDF page: '{label}' link opens the {href} tool", ok, f"frame={f.url[:50] if f else None} title={title[:40]!r}")
        finally:
            b.close()

def run_tool(p, tool, fn):
    b, pg, errs = open_pdf_tools(p)
    try:
        fn(pg, go_tool(pg, tool))
        report(f"tool works: {tool}", True)
    except Exception as e:
        msg = (str(e).splitlines() or [""])[0][:150]
        extra = ("; page errors: " + " | ".join(errs[:2])) if errs else ""
        report(f"tool works: {tool}", False, f"{type(e).__name__}: {msg}{extra}")
    finally:
        b.close()

def main():
    tmp = Path(tempfile.mkdtemp(prefix="pdftools-test-")); make_fixtures(tmp)
    A, B, IMG, LOCK = tmp / "a.pdf", tmp / "b.pdf", tmp / "img.jpg", tmp / "locked.pdf"

    def t_merge(pg, f):
        upload(pg, f, [A, B], wait_for="Merge PDF"); click_button(f, "Merge PDF", True)
        assert len(pages_of(download(pg, f)).pages) == 5, "merged PDF should have 5 pages"

    def t_split_extract(pg, f):
        upload(pg, f, [A], wait_for="Extract pages")
        f.locator("input[aria-label='Pages or ranges']").fill("1-2")
        click_button(f, "Extract pages", True)
        assert len(pages_of(download(pg, f)).pages) == 2, "extract 1-2 should give 2 pages"

    def t_split_click_sync(pg, f):
        upload(pg, f, [A], wait_for="Extract pages")
        f.locator(".pcard").nth(2).click()
        assert f.locator("input[aria-label='Pages or ranges']").input_value().replace(" ", "") in ("1,3", "1,3"), "clicking page 3 should add it to the range box"
        click_button(f, "Extract pages", True)
        assert len(pages_of(download(pg, f)).pages) == 2, "pages 1 and 3 -> 2 pages"

    def t_split_each(pg, f):
        upload(pg, f, [A], wait_for="Extract pages")
        f.locator("button[data-m='each']").click(); click_button(f, "Split PDF", True)
        z = zipfile.ZipFile(io.BytesIO(download(pg, f, prefer_zip=True)))
        assert len([n for n in z.namelist() if n.lower().endswith(".pdf")]) == 3, "every page -> 3 PDFs in the zip"

    def t_rotate(pg, f):
        upload(pg, f, [A], wait_for="Rotate PDF")
        f.locator(".pcard").nth(0).click(); f.get_by_role("button", name=re.compile("Right 90")).click()
        assert f.locator(".pcard").nth(0).locator(".tag").inner_text().strip() == "90°", "page 1 should show a 90° badge"
        click_button(f, "Rotate PDF", True)
        r = pages_of(download(pg, f))
        assert r.pages[0].get("/Rotate") == 90 and not r.pages[1].get("/Rotate"), "only page 1 should be rotated"

    def t_crop(pg, f):
        upload(pg, f, [A], wait_for="Crop PDF"); click_button(f, "Crop PDF", True)
        r = pages_of(download(pg, f)); assert len(r.pages) == 3

    def t_compress(pg, f):
        upload(pg, f, [A], wait_for="Compress PDF"); click_button(f, "Compress PDF", True)
        assert len(pages_of(download(pg, f)).pages) == 3

    def t_jpg2pdf(pg, f):
        upload(pg, f, [IMG], wait_for="Convert to PDF"); click_button(f, "Convert to PDF", True)
        assert len(pages_of(download(pg, f)).pages) == 1

    def t_pdf2jpg(pg, f):
        upload(pg, f, [A], wait_for="Convert to JPG"); click_button(f, "Convert to JPG", True)
        z = zipfile.ZipFile(io.BytesIO(download(pg, f, prefer_zip=True)))
        jp = [n for n in z.namelist() if n.lower().endswith(".jpg")]
        assert len(jp) == 3 and z.read(jp[0])[:2] == b"\xff\xd8", "3 real JPG files expected"

    def t_watermark(pg, f):
        upload(pg, f, [A], wait_for="Add watermark"); click_button(f, "Add watermark", True)
        r = pages_of(download(pg, f)); assert len(r.pages) == 3 and "CONFIDENTIAL" in r.pages[0].extract_text(), "watermark text missing"

    def t_pagenum(pg, f):
        upload(pg, f, [A], wait_for="Add page numbers"); click_button(f, "Add page numbers", True)
        r = pages_of(download(pg, f)); assert len(r.pages) == 3 and r.pages[2].extract_text().strip().endswith("3"), "page number 3 missing"

    def t_protect(pg, f):
        upload(pg, f, [A], wait_for="Protect PDF")
        pw = f.locator("input[type=password]"); pw.nth(0).fill("Test#12345"); pw.nth(1).fill("Test#12345")
        click_button(f, "Protect PDF", True)
        assert pages_of(download(pg, f)).is_encrypted, "output should be encrypted"

    def t_unlock(pg, f):
        f.locator("input[type=file]").first.set_input_files([str(LOCK)])
        pw = f.locator("input[type=password]").first; pw.wait_for(timeout=15000); pw.fill("secret123"); pw.press("Enter")
        f.get_by_role("button", name="Unlock PDF").first.wait_for(timeout=20000); click_button(f, "Unlock PDF", True)
        assert not pages_of(download(pg, f)).is_encrypted, "output should not be encrypted"

    def t_organize(pg, f):
        upload(pg, f, [A, B]); f.locator(".pcard").nth(0).wait_for(timeout=20000)
        assert f.locator(".pcard").count() == 5, "organize should show 5 pages from the 2 files"

    def t_repair(pg, f):
        upload(pg, f, [A]); f.get_by_role("button", name=re.compile("repair", re.I)).last.wait_for(timeout=20000)

    with sync_playwright() as p:
        test_navigation(p)
        test_stray_links(p)
        for name, fn in [("merge", t_merge), ("split", t_split_extract), ("split", t_split_click_sync), ("split", t_split_each),
                         ("rotate", t_rotate), ("crop", t_crop), ("compress", t_compress), ("jpg-to-pdf", t_jpg2pdf),
                         ("pdf-to-jpg", t_pdf2jpg), ("watermark", t_watermark), ("page-numbers", t_pagenum),
                         ("protect", t_protect), ("unlock", t_unlock), ("organize", t_organize), ("repair", t_repair)]:
            run_tool(p, name, fn)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
