#!/usr/bin/env python3
"""Add debug endpoint to test PDF with actual NIM data structure"""

with open('api/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add a debug endpoint that tests with NIM-like data
debug_nim = '''

@app.post("/api/debug/pdf-nim")
def debug_pdf_nim(req: ExportRequest):
    """Test PDF export with NIM-style job data."""
    import traceback
    try:
        from export_engine import PDFExporter
        pdf_bytes = PDFExporter(req.job).generate()
        return {"status": "ok", "pdf_size": len(pdf_bytes)}
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"Debug NIM PDF error: {exc}\\n{tb}")
        return {"status": "error", "error": str(exc), "type": type(exc).__name__, "traceback": tb}

'''

# Find position to insert - after the existing debug endpoint
content = content.replace(
    '@app.get("/api/debug/pdf-test")',
    debug_nim + '@app.get("/api/debug/pdf-test")'
)

with open('api/server.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Added /api/debug/pdf-nim endpoint")
