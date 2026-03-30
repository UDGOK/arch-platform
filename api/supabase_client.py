"""Supabase client for persistent job storage"""
import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger("arch_platform.supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")

def _get_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def save_job(job_id: str, job_data: dict, project_name: str = "") -> bool:
    """Save job to Supabase. Returns True on success."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase not configured, using in-memory store")
        return False
    
    try:
        # Clean job_data - remove problematic fields
        clean_data = _clean_job_for_db(job_data)
        
        payload = {
            "id": job_id,
            "project_name": project_name or job_data.get("project_name", ""),
            "job_data": json.dumps(clean_data),
            "status": "completed"
        }
        
        url = f"{SUPABASE_URL}/rest/v1/arch_jobs"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=_get_headers(),
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201):
                logger.info(f"Job {job_id} saved to Supabase")
                return True
            else:
                logger.error(f"Failed to save job: {resp.status}")
                return False
                
    except Exception as exc:
        logger.error(f"Supabase save error: {exc}")
        return False

def get_job(job_id: str) -> dict | None:
    """Get job from Supabase by ID. Returns job dict or None."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    
    try:
        url = f"{SUPABASE_URL}/rest/v1/arch_jobs?id=eq.{job_id}&select=*"
        req = urllib.request.Request(
            url,
            headers=_get_headers(),
            method="GET"
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                results = json.loads(resp.read().decode("utf-8"))
                if results:
                    job_data = results[0].get("job_data", {})
                    if isinstance(job_data, str):
                        job_data = json.loads(job_data)
                    return job_data
        return None
        
    except Exception as exc:
        logger.error(f"Supabase get error: {exc}")
        return None

def _clean_job_for_db(job_data: dict) -> dict:
    """Remove problematic fields before saving to DB."""
    import copy
    cleaned = copy.deepcopy(job_data)
    
    def remove_null_fields(obj):
        if isinstance(obj, dict):
            # Remove keys with null values at this level
            keys_to_remove = [k for k, v in obj.items() if v is None]
            for k in keys_to_remove:
                del obj[k]
            # Recurse
            for v in obj.values():
                remove_null_fields(v)
        elif isinstance(obj, list):
            for item in obj:
                remove_null_fields(item)
    
    remove_null_fields(cleaned)
    return cleaned
