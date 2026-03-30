#!/usr/bin/env python3
"""Add diagnostic endpoint to test PDF export"""

with open('api/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the export_pdf function and add diagnostic before it
diagnostic = '''

@app.get("/api/debug/pdf-test")
def debug_pdf_test():
    """Test if PDF generation works with minimal data."""
    import traceback
    try:
        from export_engine import PDFExporter
        test_job = {
            "project_name": "Test Project",
            "gross_sq_ft": 10000,
            "num_stories": 1,
            "building_type": "Commercial",
            "jurisdiction": "Chicago, IL",
            "occupancy_type": "B",
            "rooms": [
                {"name": "Office", "zone": "perimeter", "width_ft": 30, "depth_ft": 40, "sqft": 1200},
                {"name": "Conference", "zone": "perimeter", "width_ft": 20, "depth_ft": 20, "sqft": 400},
            ]
        }
        pdf_bytes = PDFExporter(test_job).generate()
        return {"status": "ok", "pdf_size": len(pdf_bytes)}
    except Exception as exc:
        tb = traceback.format_exc()
        return {"status": "error", "error": str(exc), "traceback": tb}

'''

# Find position to insert - after the imports section around line 60
insert_marker = '''class ExportRequest(BaseModel):
    job: Dict[str, Any]'''

content = content.replace(insert_marker, diagnostic + insert_marker)

with open('api/server.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Added diagnostic endpoint /api/debug/pdf-test")
