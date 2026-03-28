"""
server.py
=========
FastAPI application – Architectural AI Platform REST API.

Endpoints:
  GET  /api/meta          – Return enums, jurisdiction presets, drawing sets
  POST /api/validate      – Run compliance check only
  POST /api/dispatch      – Full compliance + drawing generation
  GET  /api/jobs/{job_id} – Retrieve a completed job by ID
  GET  /healthz           – Health check
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure local api/ package is on path when running from repo root
sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from compliance import ComplianceEngine, JurisdictionLoader
from models import (
    BuildingType,
    CodeVersion,
    ConstructionType,
    DrawingSet,
    EngineProvider,
    GenerationJob,
    OccupancyGroup,
    ProjectSpecification,
)
from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arch_platform.server")

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Architectural AI Platform",
    description="IBC 2023 Compliant Drawing Generation API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register mock engine on startup; real engines injected via /api/dispatch
EngineRegistry.register(MockDrawingEngine())
_dispatcher = EngineDispatcher()
_compliance_engine = ComplianceEngine()

# In-memory job store (swap for Redis/DB in production)
_job_store: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class JurisdictionOverride(BaseModel):
    state: str = ""
    county: str = ""
    city: str = ""
    zip_code: str = ""
    seismic_design_category: str = "B"
    wind_exposure_category: str = "B"
    wind_speed_mph: Optional[float] = None
    snow_load_psf: Optional[float] = None
    frost_depth_in: Optional[float] = None
    flood_zone: Optional[str] = None
    local_amendments: List[str] = []


class DispatchRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    building_type: str = Field(..., description="'Commercial' or 'Residential'")
    occupancy_group: str = Field(..., description="IBC 2023 occupancy group, e.g. 'B'")
    construction_type: str = Field(..., description="e.g. 'Type II-A'")
    jurisdiction_preset: str = Field(
        default="Chicago, IL",
        description="Name matching a JurisdictionLoader preset",
    )
    jurisdiction_override: Optional[JurisdictionOverride] = None
    primary_code: str = Field(default="IBC 2023")
    drawing_sets: List[str] = Field(
        default=["Floor Plan", "Exterior Elevations"],
        description="List of DrawingSet values",
    )
    engine_provider: str = Field(default="Mock Engine (Testing)")
    api_key: str = Field(default="", description="Engine API key (not stored)")
    gross_sq_ft: Optional[float] = None
    num_stories: Optional[int] = None
    building_height_ft: Optional[float] = None
    occupant_load: Optional[int] = None
    sprinklered: bool = False
    accessibility_standard: str = "ADA / ICC A117.1-2017"
    additional_notes: str = ""


def _build_spec(req: DispatchRequest) -> ProjectSpecification:
    """Map a DispatchRequest to a ProjectSpecification, raising HTTPException on bad input."""

    def _enum(cls, val: str, label: str):
        try:
            return cls(val)
        except ValueError:
            valid = [e.value for e in cls]
            raise HTTPException(
                400,
                detail=f"Invalid {label} '{val}'. Valid options: {valid}",
            )

    building_type     = _enum(BuildingType, req.building_type, "building_type")
    occupancy_group   = _enum(OccupancyGroup, req.occupancy_group, "occupancy_group")
    construction_type = _enum(ConstructionType, req.construction_type, "construction_type")
    primary_code      = _enum(CodeVersion, req.primary_code, "primary_code")
    engine_provider   = _enum(EngineProvider, req.engine_provider, "engine_provider")

    try:
        drawing_sets = [_enum(DrawingSet, ds, "drawing_set") for ds in req.drawing_sets]
    except HTTPException:
        valid_ds = [ds.value for ds in DrawingSet]
        raise HTTPException(400, detail=f"Invalid drawing set. Valid: {valid_ds}")

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
                400,
                detail=(
                    f"Unknown jurisdiction preset '{req.jurisdiction_preset}'. "
                    f"Available: {JurisdictionLoader.available_jurisdictions()}"
                ),
            )

    return ProjectSpecification(
        project_name=req.project_name,
        building_type=building_type,
        occupancy_group=occupancy_group,
        construction_type=construction_type,
        jurisdiction=j,
        primary_code=primary_code,
        drawing_sets=drawing_sets,
        engine_provider=engine_provider,
        gross_sq_ft=req.gross_sq_ft,
        num_stories=req.num_stories,
        building_height_ft=req.building_height_ft,
        occupant_load=req.occupant_load,
        sprinklered=req.sprinklered,
        accessibility_standard=req.accessibility_standard,
        additional_notes=req.additional_notes,
    )


def _job_to_dict(job: GenerationJob) -> dict:
    """Serialise a GenerationJob to a plain dict for JSON responses."""
    report = None
    if job.compliance_report:
        report = {
            "is_compliant": job.compliance_report.is_compliant,
            "blocking_count": job.compliance_report.blocking_count,
            "warning_count": job.compliance_report.warning_count,
            "summary": job.compliance_report.summary(),
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "severity": f.severity.value,
                    "code_section": f.code_section,
                    "description": f.description,
                    "recommendation": f.recommendation,
                    "is_blocking": f.is_blocking(),
                }
                for f in job.compliance_report.findings
            ],
        }

    drawings = [
        {
            "sheet_number": d.sheet_number,
            "title": d.title,
            "sheet_type": d.sheet_type.value,
            "format": d.format,
            "url": d.url,
            "available": d.is_available(),
            "metadata": {k: v for k, v in d.metadata.items() if k != "generated_at"},
        }
        for d in job.drawings
    ]

    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "engine": job.engine_provider.value,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_seconds": job.duration_seconds(),
        "error_message": job.error_message,
        "compliance_report": report,
        "drawings": drawings,
        "drawing_count": len(drawings),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def health() -> dict:
    return {"status": "ok", "service": "arch-platform"}


@app.get("/api/meta")
def get_meta() -> dict:
    """Return all valid enum values and jurisdiction presets for the frontend."""
    return {
        "building_types": [e.value for e in BuildingType],
        "occupancy_groups": {
            bt.value: [og.value for og in OccupancyGroup
                       if (bt == BuildingType.COMMERCIAL and og.value not in ("R-1","R-2","R-3","R-4"))
                       or (bt == BuildingType.RESIDENTIAL and og.value in ("R-1","R-2","R-3","R-4"))]
            for bt in BuildingType
        },
        "construction_types": [e.value for e in ConstructionType],
        "code_versions": [e.value for e in CodeVersion],
        "drawing_sets": [e.value for e in DrawingSet],
        "engine_providers": [e.value for e in EngineProvider],
        "jurisdiction_presets": JurisdictionLoader.available_jurisdictions(),
        "jurisdiction_details": {
            name: JurisdictionLoader.load(name).to_dict()
            for name in JurisdictionLoader.available_jurisdictions()
        },
    }


@app.post("/api/validate")
def validate_spec(req: DispatchRequest) -> dict:
    """Run compliance check only – does not dispatch to any AI engine."""
    spec = _build_spec(req)
    try:
        report = _compliance_engine.validate(spec)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))

    return {
        "project_id": spec.project_id,
        "project_name": spec.project_name,
        "compliance_report": {
            "is_compliant": report.is_compliant,
            "blocking_count": report.blocking_count,
            "warning_count": report.warning_count,
            "summary": report.summary(),
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "severity": f.severity.value,
                    "code_section": f.code_section,
                    "description": f.description,
                    "recommendation": f.recommendation,
                    "is_blocking": f.is_blocking(),
                }
                for f in report.findings
            ],
        },
    }


@app.post("/api/dispatch")
def dispatch_job(req: DispatchRequest) -> dict:
    """Validate + dispatch to AI engine. Returns the completed GenerationJob."""
    spec = _build_spec(req)

    # Register engine with provided API key
    if req.api_key and req.engine_provider != EngineProvider.MOCK.value:
        from orchestrator import (
            ClaudeDrawingEngine,
            NvidiaDrawingEngine,
            OpenAIDrawingEngine,
        )
        engine_map = {
            EngineProvider.CLAUDE.value: lambda: ClaudeDrawingEngine(api_key=req.api_key),
            EngineProvider.NVIDIA.value: lambda: NvidiaDrawingEngine(api_key=req.api_key),
            EngineProvider.OPENAI.value: lambda: OpenAIDrawingEngine(api_key=req.api_key),
        }
        factory = engine_map.get(req.engine_provider)
        if factory:
            EngineRegistry.register(factory())

    job = _dispatcher.dispatch(spec)
    result = _job_to_dict(job)
    _job_store[job.job_id] = result
    return result


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    if job_id not in _job_store:
        raise HTTPException(404, detail=f"Job '{job_id}' not found.")
    return _job_store[job_id]


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": list(_job_store.values()), "count": len(_job_store)}


# ---------------------------------------------------------------------------
# Serve static frontend (production)
# ---------------------------------------------------------------------------
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def serve_index():
        return FileResponse(str(_static_dir / "index.html"))
