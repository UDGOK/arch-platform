#!/usr/bin/env python3
"""Update server.py with Supabase integration"""
import re

# Read server.py
with open('api/server.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add Supabase import after orchestrator import
old_import = 'from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine\n'
new_import = '''from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine
from supabase_client import save_job, get_job as get_job_from_db
'''
content = content.replace(old_import, new_import)

# 2. Add save_job call after _job_store assignment in NIM dispatch
old_store = '''    _job_store[spec.project_id] = result
    return result


@app.post("/api/upload")'''

new_store = '''    _job_store[spec.project_id] = result
    # Persist to Supabase for reliability
    save_job(spec.project_id, result, result.get("project_name", ""))
    return result


@app.post("/api/upload")'''

content = content.replace(old_store, new_store)

# 3. Update get_job to use Supabase
old_get_job = '''@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in _job_store:
        raise HTTPException(404, detail=f"Job '{job_id}' not found.")
    return _job_store[job_id]'''

new_get_job = '''@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    # Try in-memory store first
    if job_id in _job_store:
        return _job_store[job_id]
    # Try Supabase
    job = get_job_from_db(job_id)
    if job:
        _job_store[job_id] = job  # Cache in memory
        return job
    raise HTTPException(404, detail=f"Job '{job_id}' not found.")'''

content = content.replace(old_get_job, new_get_job)

# 4. Update export_pdf_by_id
old_pdf_id = '''@app.get("/api/export/{job_id}/pdf")
def export_pdf_by_id(job_id: str):
    """Export PDF by job ID (avoids large request body)."""
    if job_id not in _job_store:
        raise HTTPException(404, detail=f"Job {job_id} not found")
    job = _job_store[job_id]'''

new_pdf_id = '''@app.get("/api/export/{job_id}/pdf")
def export_pdf_by_id(job_id: str):
    """Export PDF by job ID (avoids large request body)."""
    # Try in-memory store first
    if job_id in _job_store:
        job = _job_store[job_id]
    else:
        # Try Supabase
        job = get_job_from_db(job_id)
        if not job:
            raise HTTPException(404, detail=f"Job {job_id} not found")'''

content = content.replace(old_pdf_id, new_pdf_id)

# 5. Update export_package_by_id
old_pkg_id = '''@app.get("/api/export/{job_id}/package")
def export_package_by_id(job_id: str):
    """Export ZIP package by job ID (avoids large request body)."""
    if job_id not in _job_store:
        raise HTTPException(404, detail=f"Job {job_id} not found")
    job = _job_store[job_id]'''

new_pkg_id = '''@app.get("/api/export/{job_id}/package")
def export_package_by_id(job_id: str):
    """Export ZIP package by job ID (avoids large request body)."""
    # Try in-memory store first
    if job_id in _job_store:
        job = _job_store[job_id]
    else:
        # Try Supabase
        job = get_job_from_db(job_id)
        if not job:
            raise HTTPException(404, detail=f"Job {job_id} not found")'''

content = content.replace(old_pkg_id, new_pkg_id)

# Write updated content
with open('api/server.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("server.py updated successfully!")
print("Changes made:")
print("  1. Added Supabase import")
print("  2. Added save_job call after NIM dispatch")
print("  3. Updated get_job to use Supabase fallback")
print("  4. Updated export_pdf_by_id to use Supabase fallback")
print("  5. Updated export_package_by_id to use Supabase fallback")
