"""
server.py  (v2 – NVIDIA NIM Integration)
=========================================
Endpoints:
  GET  /healthz
  GET  /api/meta
  POST /api/validate
  POST /api/dispatch          – Mock / Claude / OpenAI engines
  POST /api/dispatch/nim      – Full 3-stage NVIDIA NIM pipeline
  POST /api/upload            – Sketch / floor plan image upload
  POST /api/chat/refine       – Conversational drawing refinement
  GET  /api/nim/models        – NIM model info
  GET  /api/jobs/{id}
  GET  /api/jobs
"""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from compliance import ComplianceEngine, JurisdictionLoader
from models import (
    BuildingType, CodeVersion, ConstructionType, DrawingSet,
    EngineProvider, GenerationJob, OccupancyGroup, ProjectSpecification,
)
from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arch_platform.server")

app = FastAPI(
    title="Architectural AI Platform",
    description="IBC 2023 + NVIDIA NIM Drawing Generation",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

EngineRegistry.register(MockDrawingEngine())
_dispatcher        = EngineDispatcher()
_compliance_engine = ComplianceEngine()
_job_store: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class JurisdictionOverride(BaseModel):
    state: str = ""; county: str = ""; city: str = ""; zip_code: str = ""
    seismic_design_category: str = "B"; wind_exposure_category: str = "B"
    wind_speed_mph: Optional[float] = None; snow_load_psf: Optional[float] = None
    frost_depth_in: Optional[float] = None; flood_zone: Optional[str] = None
    local_amendments: List[str] = []


class DispatchRequest(BaseModel):
    project_name:           str  = Field(..., min_length=1, max_length=200)
    building_type:          str  = "Commercial"
    occupancy_group:        str  = "B"
    construction_type:      str  = "Type II-A"
    jurisdiction_preset:    str  = "Chicago, IL"
    jurisdiction_override:  Optional[JurisdictionOverride] = None
    primary_code:           str  = "IBC 2023"
    drawing_sets:           List[str] = ["Floor Plan", "Exterior Elevations"]
    engine_provider:        str  = "Mock Engine (Testing)"
    api_key:                str  = ""
    gross_sq_ft:            Optional[float] = None
    num_stories:            Optional[int]   = None
    building_height_ft:     Optional[float] = None
    occupant_load:          Optional[int]   = None
    sprinklered:            bool = False
    accessibility_standard: str  = "ADA / ICC A117.1-2017"
    additional_notes:       str  = ""
    image_b64:              Optional[str] = None
    image_media_type:       str  = "image/jpeg"
    generate_images:        bool = True


class RefineRequest(BaseModel):
    sheet_number:   str
    sheet_title:    str
    instruction:    str = Field(..., min_length=3)
    key_notes:      List[str] = []
    drawing_prompt: str = ""
    history:        List[Dict[str, str]] = []
    api_key:        str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _enum(cls, val: str, label: str):
    try:
        return cls(val)
    except ValueError:
        raise HTTPException(400, detail=f"Invalid {label}: '{val}'")


def _build_spec(req: DispatchRequest) -> ProjectSpecification:
    building_type     = _enum(BuildingType,     req.building_type,     "building_type")
    occupancy_group   = _enum(OccupancyGroup,   req.occupancy_group,   "occupancy_group")
    construction_type = _enum(ConstructionType, req.construction_type, "construction_type")
    primary_code      = _enum(CodeVersion,      req.primary_code,      "primary_code")
    engine_provider   = _enum(EngineProvider,   req.engine_provider,   "engine_provider")
    drawing_sets      = [_enum(DrawingSet, ds, "drawing_set") for ds in req.drawing_sets]

    if req.jurisdiction_override and req.jurisdiction_override.city:
        from models import Jurisdiction
        j = Jurisdiction(
            state=req.jurisdiction_override.state,
            county=req.jurisdiction_override.county,
            city=req.jurisdiction_override.city,
            zip_code=req.jurisdiction_override.zip_code,
            adopted_building_code=primary_code,
            seismic_design_category=req.jurisdiction_override.seismic_design_category,
            wind_exposure_category=req.jurisdiction_override.wind_exposure_category,
            wind_speed_mph=req.jurisdiction_override.wind_speed_mph,
            snow_load_psf=req.jurisdiction_override.snow_load_psf,
            frost_depth_in=req.jurisdiction_override.frost_depth_in,
            flood_zone=req.jurisdiction_override.flood_zone,
            local_amendments=req.jurisdiction_override.local_amendments,
        )
    else:
        try:
            j = JurisdictionLoader.load(req.jurisdiction_preset)
        except KeyError:
            raise HTTPException(
                400, detail=f"Unknown jurisdiction preset '{req.jurisdiction_preset}'"
            )

    return ProjectSpecification(
        project_name=req.project_name, building_type=building_type,
        occupancy_group=occupancy_group, construction_type=construction_type,
        jurisdiction=j, primary_code=primary_code, drawing_sets=drawing_sets,
        engine_provider=engine_provider, gross_sq_ft=req.gross_sq_ft,
        num_stories=req.num_stories, building_height_ft=req.building_height_ft,
        occupant_load=req.occupant_load, sprinklered=req.sprinklered,
        accessibility_standard=req.accessibility_standard,
        additional_notes=req.additional_notes,
    )


def _compliance_dict(report) -> dict:
    return {
        "is_compliant":  report.is_compliant,
        "blocking_count": report.blocking_count,
        "warning_count":  report.warning_count,
        "summary":        report.summary(),
        "findings": [{
            "rule_id": f.rule_id, "severity": f.severity.value,
            "code_section": f.code_section, "description": f.description,
            "recommendation": f.recommendation, "is_blocking": f.is_blocking(),
        } for f in report.findings],
    }


def _job_to_dict(job: GenerationJob) -> dict:
    return {
        "job_id":           job.job_id,
        "status":           job.status.value,
        "engine":           job.engine_provider.value,
        "created_at":       job.created_at.isoformat(),
        "completed_at":     job.completed_at.isoformat() if job.completed_at else None,
        "duration_seconds": job.duration_seconds(),
        "error_message":    job.error_message,
        "compliance_report": _compliance_dict(job.compliance_report) if job.compliance_report else None,
        "drawings": [{
            "sheet_number": d.sheet_number, "title": d.title,
            "sheet_type": d.sheet_type.value, "format": d.format,
            "url": d.url, "available": d.is_available(),
            "has_image": d.metadata.get("has_image", False),
            "image_b64": d.metadata.get("image_b64", ""),
            "metadata": {k: v for k, v in d.metadata.items()
                         if k not in ("generated_at", "image_b64")},
        } for d in job.drawings],
        "drawing_count": len(job.drawings),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health():
    return {"status": "ok", "service": "arch-platform", "version": "2.0.0"}


@app.get("/api/meta")
def get_meta():
    return {
        "building_types": [e.value for e in BuildingType],
        "occupancy_groups": {
            bt.value: [og.value for og in OccupancyGroup if (
                bt == BuildingType.COMMERCIAL and og.value not in ("R-1","R-2","R-3","R-4")
            ) or (
                bt == BuildingType.RESIDENTIAL and og.value in ("R-1","R-2","R-3","R-4")
            )] for bt in BuildingType
        },
        "construction_types":   [e.value for e in ConstructionType],
        "code_versions":        [e.value for e in CodeVersion],
        "drawing_sets":         [e.value for e in DrawingSet],
        "engine_providers":     [e.value for e in EngineProvider],
        "jurisdiction_presets": JurisdictionLoader.available_jurisdictions(),
        "jurisdiction_details": {
            name: JurisdictionLoader.load(name).to_dict()
            for name in JurisdictionLoader.available_jurisdictions()
        },
        "nim_models": {
            "vision":    "meta/llama-3.2-90b-vision-instruct",
            "llm":       "meta/llama-3.1-70b-instruct",
            "image_gen": "stabilityai/stable-diffusion-xl",
        },
    }


@app.post("/api/validate")
def validate_spec(req: DispatchRequest):
    spec = _build_spec(req)
    try:
        report = _compliance_engine.validate(spec)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    return {
        "project_id": spec.project_id,
        "project_name": spec.project_name,
        "compliance_report": _compliance_dict(report),
    }


@app.post("/api/dispatch")
def dispatch_job(req: DispatchRequest):
    spec = _build_spec(req)
    if req.api_key and req.engine_provider != EngineProvider.MOCK.value:
        from orchestrator import ClaudeDrawingEngine, NvidiaDrawingEngine, OpenAIDrawingEngine
        engine_map = {
            EngineProvider.CLAUDE.value: lambda: ClaudeDrawingEngine(api_key=req.api_key),
            EngineProvider.NVIDIA.value: lambda: NvidiaDrawingEngine(api_key=req.api_key),
            EngineProvider.OPENAI.value: lambda: OpenAIDrawingEngine(api_key=req.api_key),
        }
        f = engine_map.get(req.engine_provider)
        if f: EngineRegistry.register(f())
    job    = _dispatcher.dispatch(spec)
    result = _job_to_dict(job)
    _job_store[job.job_id] = result
    return result


@app.post("/api/dispatch/nim")
def dispatch_nim(req: DispatchRequest):
    """Full 3-stage NVIDIA NIM pipeline: Vision → LLM Manifest → Image Gen."""
    if not req.api_key:
        raise HTTPException(
            400, detail="NVIDIA API key required. Get one free at build.nvidia.com"
        )
    spec = _build_spec(req)
    try:
        report = _compliance_engine.validate(spec)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    if not report.is_compliant:
        blocking = [f"{f.severity.value}: {f.description}"
                    for f in report.findings if f.is_blocking()]
        raise HTTPException(422, detail={"message": "Compliance failed",
                                          "blocking": blocking})
    from nvidia_nim import run_nim_pipeline
    try:
        nim = run_nim_pipeline(
            spec             = spec,
            api_key          = req.api_key,
            image_b64        = req.image_b64,
            image_media_type = req.image_media_type,
            generate_images  = req.generate_images,
        )
    except Exception as exc:
        logger.exception("NIM pipeline error")
        raise HTTPException(500, detail=f"NIM pipeline error: {exc}")

    result = {
        "project_id":      spec.project_id,
        "project_name":    spec.project_name,
        "status":          "completed" if not nim.errors else "partial",
        "engine":          "NVIDIA NIM",
        "stage_timings":   nim.stage_timings,
        "pipeline_errors": nim.errors,
        "compliance_report": _compliance_dict(report),
        "vision_analysis": nim.vision_analysis,
        "drawings": [{
            "sheet_number":  d.sheet_number,
            "title":         d.title,
            "sheet_type":    d.sheet_type.value,
            "format":        d.format,
            "available":     d.is_available(),
            "has_image":     d.metadata.get("has_image", False),
            "image_b64":     d.metadata.get("image_b64", ""),
            "discipline":    d.metadata.get("discipline", ""),
            "scale":         d.metadata.get("scale", ""),
            "key_notes":     d.metadata.get("key_notes", []),
            "code_sections": d.metadata.get("code_sections", []),
            "ada_notes":     d.metadata.get("ada_notes", ""),
            "models_used":   d.metadata.get("models_used", {}),
        } for d in nim.drawings],
        "drawing_count": len(nim.drawings),
    }
    _job_store[spec.project_id] = result
    return result


@app.post("/api/upload")
async def upload_sketch(file: UploadFile = File(...)):
    """Accept sketch image (JPG/PNG ≤ 10 MB), return base64 for use in /api/dispatch/nim."""
    allowed = {"image/jpeg", "image/jpg", "image/png"}
    ct = (file.content_type or "").lower()
    if ct not in allowed:
        raise HTTPException(400, detail=f"Unsupported type '{ct}'. Use JPG or PNG.")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="File exceeds 10 MB limit.")
    return {
        "filename":     file.filename,
        "media_type":   ct,
        "size_bytes":   len(raw),
        "image_b64":    base64.b64encode(raw).decode("utf-8"),
        "ready":        True,
    }


@app.post("/api/chat/refine")
def refine_drawing_endpoint(req: RefineRequest):
    """Conversational refinement of a drawing sheet via Llama 3.1."""
    from nvidia_nim import refine_drawing, MODEL_LLM
    from models import DrawingOutput, DrawingSet
    drawing = DrawingOutput(
        sheet_type=DrawingSet.FLOOR_PLAN,
        sheet_number=req.sheet_number,
        title=req.sheet_title,
        metadata={"key_notes": req.key_notes, "drawing_prompt": req.drawing_prompt},
    )
    try:
        reply, updated = refine_drawing(
            sheet=drawing, instruction=req.instruction,
            history=req.history, api_key=req.api_key,
        )
    except Exception as exc:
        raise HTTPException(500, detail=f"Refinement error: {exc}")
    return {
        "reply":          reply,
        "sheet_number":   updated.sheet_number,
        "key_notes":      updated.metadata.get("key_notes", []),
        "drawing_prompt": updated.metadata.get("drawing_prompt", ""),
        "model":          MODEL_LLM,
    }


@app.get("/api/nim/models")
def nim_models():
    from nvidia_nim import MODEL_VISION, MODEL_LLM, NIM_IMG_GEN
    return {
        "stage_1_vision":     MODEL_VISION,
        "stage_2_llm":        MODEL_LLM,
        "stage_3_image_gen":  "stabilityai/stable-diffusion-xl",
        "image_gen_endpoint": NIM_IMG_GEN,
        "api_base":           "https://integrate.api.nvidia.com/v1",
        "free_tier_url":      "https://build.nvidia.com",
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in _job_store:
        raise HTTPException(404, detail=f"Job '{job_id}' not found.")
    return _job_store[job_id]


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": list(_job_store.values()), "count": len(_job_store)}


# Static frontend
_static = Path(__file__).parent.parent / "public"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_index():
        return FileResponse(str(_static / "index.html"))
