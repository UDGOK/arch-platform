import re

with open(r"C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py", 'r', encoding='utf-8') as f:
    content = f.read()

# Improve PDF export error handling with traceback
old_pdf = '''@app.post("/api/export/pdf")
def export_pdf(req: ExportRequest):
    """Generate permit-ready PDF from completed job. Returns application/pdf."""
    try:
        from export_engine import PDFExporter
        pdf_bytes = PDFExporter(req.job).generate()
    except Exception as exc:
        logger.exception("PDF export error")
        raise HTTPException(500, detail=f"PDF export failed: {exc}")'''

new_pdf = '''@app.post("/api/export/pdf")
def export_pdf(req: ExportRequest):
    """Generate permit-ready PDF from completed job. Returns application/pdf."""
    import traceback
    try:
        from export_engine import PDFExporter
        pdf_bytes = PDFExporter(req.job).generate()
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"PDF export error: {exc}\\n{tb}")
        raise HTTPException(500, detail=f"PDF export failed: {exc.__class__.__name__}: {exc}")'''

content = content.replace(old_pdf, new_pdf)

# Improve Package export error handling with traceback
old_pkg = '''@app.post("/api/export/package")
def export_package(req: ExportRequest):
    """Generate ZIP with PDF + all DXF sheets + manifest.json."""
    try:
        from export_engine import build_export_package
        zip_bytes = build_export_package(req.job)
    except Exception as exc:
        logger.exception("Package export error")
        raise HTTPException(500, detail=f"Package export failed: {exc}")'''

new_pkg = '''@app.post("/api/export/package")
def export_package(req: ExportRequest):
    """Generate ZIP with PDF + all DXF sheets + manifest.json."""
    import traceback
    try:
        from export_engine import build_export_package
        zip_bytes = build_export_package(req.job)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"Package export error: {exc}\\n{tb}")
        raise HTTPException(500, detail=f"Package export failed: {exc.__class__.__name__}: {exc}")'''

content = content.replace(old_pkg, new_pkg)

with open(r"C:\Users\Yasir\.minimax-agent\projects\5\arch-platform\api\server.py", 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
