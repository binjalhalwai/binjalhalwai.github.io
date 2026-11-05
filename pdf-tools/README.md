# PDF Tools - source

The PDF Tools app used to exist only as a compressed blob (`PDF_TOOLS_GZ`) inside `nepse_news_hub.py`.
These are the real, editable source files, extracted from that blob. `backup/pdf_tools_original_blob.txt` is the untouched original.

    pdf-tools/src/index.template.html   page shell (all 15 tool page templates); @@INLINE:...@@ marks where a file is put back
    pdf-tools/src/js/common.js          shared helpers (window.PT): file handling, thumbnails, PT.PageGrid, results, downloads
    pdf-tools/src/js/tools.js           the 15 tools + the router and the link handler
    pdf-tools/src/css/pdf-tools.css     styles
    pdf-tools/src/vendor/               pdf-lib (MIT) and pdf.js 3.11.174 (Apache-2.0), unchanged; keep their licence headers
    pdf-tools/build.py                  source -> blob
    pdf-tools/tests/smoke.py            browser tests (needs: pip install playwright reportlab pillow pypdf; python -m playwright install chromium)
    pdf-tools/dev/extract_from_blob.py  the one-time extractor (kept for the record)

## Change PDF Tools

1. Edit files in `pdf-tools/src/`.
2. `python pdf-tools/build.py --write-python`   (rebuilds the blob and replaces ONLY the `PDF_TOOLS_GZ = "..."` line in nepse_news_hub.py)
3. `python nepse_news_hub.py publish`, then serve `public_site` and run `python pdf-tools/tests/smoke.py http://localhost:8765/index.html`
4. Commit `pdf-tools/src` together with the updated `nepse_news_hub.py`. Do not commit `pdf-tools/dist/` (generated).

`python pdf-tools/build.py --check` rebuilds from source and compares with the original backup (before any edits it prints IDENTICAL).
