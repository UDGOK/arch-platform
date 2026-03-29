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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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




# ---------------------------------------------------------------------------
# Debug endpoint - diagnose import and config issues on Vercel
# ---------------------------------------------------------------------------

@app.get("/api/debug")
def debug_info():
    """Returns import status and environment info for diagnosing Vercel issues."""
    import sys, os
    results = {"python": sys.version, "platform": sys.platform, "imports": {}, "env": {}}

    libs = [
        ("fastapi", "fastapi"),
        ("compliance", "compliance"),
        ("models", "models"),
        ("orchestrator", "orchestrator"),
        ("numpy", "numpy"),
        ("httpx", "httpx"),
        ("reportlab", "reportlab"),
        ("ezdxf", "ezdxf"),
        ("pdfplumber", "pdfplumber"),
        ("pymupdf", "fitz"),
        ("ifcopenshell", "ifcopenshell"),
        ("export_engine", "export_engine"),
        ("rag_engine", "rag_engine"),
        ("triton_client", "triton_client"),
        ("file_parser", "file_parser"),
        ("nvidia_nim", "nvidia_nim"),
    ]
    for name, mod in libs:
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", getattr(m, "version", "ok"))
            results["imports"][name] = str(v)[:30]
        except Exception as e:
            results["imports"][name] = f"FAIL: {str(e)[:80]}"

    results["job_count"] = len(_job_store)
    results["registered_engines"] = [e.value for e in EngineRegistry.available()]
    return results

# Static frontend
_static = Path(__file__).parent.parent / "public"

# Serve index.html at root - works on Vercel and locally
@app.get("/", include_in_schema=False)
def serve_index():
    idx = _static / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return {"status": "ok", "message": "Architectural AI Platform API", "docs": "/api/docs"}

if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

from fastapi.responses import Response as FastAPIResponse

class ExportRequest(BaseModel):
    job: Dict[str, Any]


@app.post("/api/export/pdf")
def export_pdf(req: ExportRequest):
    """Generate permit-ready PDF from completed job. Returns application/pdf."""
    try:
        from export_engine import PDFExporter
        pdf_bytes = PDFExporter(req.job).generate()
    except Exception as exc:
        logger.exception("PDF export error")
        raise HTTPException(500, detail=f"PDF export failed: {exc}")
    project = req.job.get("project_name","project").replace(" ","_")[:40]
    return FastAPIResponse(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{project}_construction_documents.pdf"'},
    )


@app.post("/api/export/dxf/{sheet_index}")
def export_dxf_sheet(sheet_index: int, req: ExportRequest):
    """Generate single DXF sheet. sheet_index = 0-based index into drawings[]."""
    drawings = req.job.get("drawings", [])
    if sheet_index >= len(drawings):
        raise HTTPException(404, detail=f"Sheet {sheet_index} out of range.")
    try:
        from export_engine import DXFExporter
        dxf_bytes = DXFExporter(req.job).generate_sheet(drawings[sheet_index])
    except Exception as exc:
        logger.exception("DXF export error")
        raise HTTPException(500, detail=f"DXF export failed: {exc}")
    drw = drawings[sheet_index]
    num = drw.get("sheet_number","X0").replace("/","_").replace(" ","_")
    stype = drw.get("sheet_type","Sheet").replace(" ","_")
    return FastAPIResponse(
        content=dxf_bytes, media_type="application/dxf",
        headers={"Content-Disposition": f'attachment; filename="{num}_{stype}.dxf"'},
    )


@app.post("/api/export/package")
def export_package(req: ExportRequest):
    """Generate ZIP with PDF + all DXF sheets + manifest.json."""
    try:
        from export_engine import build_export_package
        zip_bytes = build_export_package(req.job)
    except Exception as exc:
        logger.exception("Package export error")
        raise HTTPException(500, detail=f"Package export failed: {exc}")
    project = req.job.get("project_name","project").replace(" ","_")[:40]
    return FastAPIResponse(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project}_construction_set.zip"'},
    )


# ---------------------------------------------------------------------------
# NeMo Retriever RAG endpoints
# ---------------------------------------------------------------------------

class RAGSearchRequest(BaseModel):
    query:   str = Field(..., min_length=3)
    top_k:   int = Field(default=5, ge=1, le=10)
    rerank:  bool = True
    api_key: str = ""


@app.post("/api/rag/search")
def rag_search(req: RAGSearchRequest):
    """
    Semantic search over the IBC 2023 corpus via NeMo Retriever.
    Uses nvidia/nv-embedqa-e5-v5 + nvidia/nv-rerankqa-mistral-4b-v3.
    Falls back to TF-IDF keyword search when no API key is provided.
    """
    from rag_engine import get_retriever
    retriever = get_retriever(req.api_key)
    try:
        hits = retriever.retrieve(req.query, top_k=req.top_k, rerank=req.rerank)
    except Exception as exc:
        raise HTTPException(500, detail=f"RAG search failed: {exc}")
    return {
        "query":   req.query,
        "top_k":   req.top_k,
        "results": hits,
        "mode":    "nemo-retriever" if req.api_key else "tfidf-fallback",
        "models": {
            "embed":  "nvidia/nv-embedqa-e5-v5",
            "rerank": "nvidia/nv-rerankqa-mistral-4b-v3",
        },
    }


@app.post("/api/validate/rag")
def validate_rag(req: DispatchRequest):
    """
    Compliance check augmented with NeMo Retriever RAG citations.
    Adds INFO-level findings with retrieved IBC section text.
    """
    spec = _build_spec(req)
    from rag_engine import get_retriever, RAGComplianceEngine
    retriever = get_retriever(req.api_key)
    engine    = RAGComplianceEngine(retriever)
    try:
        report = engine.validate(spec)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    return {
        "project_id":        spec.project_id,
        "project_name":      spec.project_name,
        "rag_mode":          "nemo-retriever" if req.api_key else "tfidf-fallback",
        "compliance_report": _compliance_dict(report),
    }


@app.get("/api/rag/corpus")
def rag_corpus():
    """List all IBC 2023 sections in the knowledge base."""
    from rag_engine import IBC_CORPUS
    return {
        "total_chunks": len(IBC_CORPUS),
        "chapters":     list({c["chapter"] for c in IBC_CORPUS}),
        "sections":     [{"id": c["id"], "section": c["section"],
                          "chapter": c["chapter"]} for c in IBC_CORPUS],
    }


# ---------------------------------------------------------------------------
# Triton / metrics endpoint
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
def get_metrics():
    """Triton client metrics: request counts, success rate, p95 latency, circuit breaker."""
    from triton_client import get_triton_client
    return {
        "triton_client": get_triton_client().metrics(),
        "service": "arch-platform",
        "version": "2.0.0",
    }


# ---------------------------------------------------------------------------
# Multi-format drawing upload endpoints
# ---------------------------------------------------------------------------

ALLOWED_UPLOAD_TYPES = {
    "application/pdf":                          ".pdf",
    "application/octet-stream":                 None,   # auto-detect from filename
    "application/dxf":                          ".dxf",
    "image/vnd.dxf":                            ".dxf",
    "application/acad":                         ".dwg",
    "application/x-dwg":                        ".dwg",
    "application/x-autocad":                    ".dwg",
    "application/vnd.ms-pki.stl":               ".rvt",
}

MAX_UPLOAD_MB = 100


@app.post("/api/upload/drawing")
async def upload_drawing(
    file:               UploadFile = File(...),
    aps_client_id:      str = Form(default=""),
    aps_client_secret:  str = Form(default=""),
):
    """
    Upload and parse a drawing file. Supported formats:
      .dwg / .dxf  — AutoCAD (ezdxf)
      .pdf         — PDF drawings (pdfplumber + PyMuPDF → PNG preview)
      .ifc         — IFC BIM model (ifcopenshell)
      .rvt         — Revit (Autodesk APS or text-scan fallback)

    Returns ParsedDrawing JSON including:
      - rooms, layers, text annotations, dimensions
      - structural elements
      - preview_image_b64 (PNG, for feeding into Vision AI)
      - summary string ready for LLM prompt injection
    """
    filename = file.filename or "drawing"
    ext      = ("." + filename.rsplit(".",1)[-1]).lower() if "." in filename else ""

    allowed_exts = {".dwg", ".dxf", ".pdf", ".ifc", ".rvt"}
    if ext not in allowed_exts:
        raise HTTPException(
            400,
            detail=(
                f"Unsupported file extension '{ext}'. "
                f"Allowed: {', '.join(sorted(allowed_exts))}"
            ),
        )

    raw = await file.read()
    size_mb = len(raw) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(413, detail=f"File too large ({size_mb:.1f} MB). Max {MAX_UPLOAD_MB} MB.")

    logger.info("Drawing upload: %s  %.1f MB  ext=%s", filename, size_mb, ext)

    from file_parser import parse_drawing_file
    try:
        parsed = parse_drawing_file(
            raw, filename,
            aps_client_id=aps_client_id,
            aps_client_secret=aps_client_secret,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    except Exception as exc:
        logger.exception("File parse error")
        raise HTTPException(500, detail=f"Parse error: {exc}")

    return parsed.to_dict()


@app.get("/api/upload/formats")
def supported_formats():
    """List all supported upload file formats with guidance."""
    return {
        "supported_formats": [
            {
                "extension": ".dwg",
                "name":      "AutoCAD Drawing",
                "parser":    "ezdxf",
                "extracts":  ["layers", "text", "rooms", "dimensions", "extents", "preview_PNG"],
                "max_size":  "100 MB",
                "notes":     "Supports DWG and DXF (ASCII or binary). All AutoCAD versions.",
            },
            {
                "extension": ".dxf",
                "name":      "Drawing Exchange Format",
                "parser":    "ezdxf",
                "extracts":  ["layers", "text", "rooms", "dimensions", "extents", "preview_PNG"],
                "max_size":  "100 MB",
                "notes":     "ASCII or binary DXF. Export from AutoCAD, Revit, Rhino, SketchUp.",
            },
            {
                "extension": ".pdf",
                "name":      "PDF Drawing Set",
                "parser":    "pdfplumber + PyMuPDF",
                "extracts":  ["text", "rooms", "dimensions", "page_count", "preview_PNG"],
                "max_size":  "100 MB",
                "notes":     "Text-layer PDFs give best results. Scanned PDFs use OCR preview only.",
            },
            {
                "extension": ".ifc",
                "name":      "Industry Foundation Classes (BIM)",
                "parser":    "ifcopenshell",
                "extracts":  ["spaces", "storeys", "structural_elements", "element_counts"],
                "max_size":  "100 MB",
                "notes":     "Export from Revit: File → Export → IFC (IFC 2x3 or IFC4)",
            },
            {
                "extension": ".rvt",
                "name":      "Autodesk Revit",
                "parser":    "Autodesk APS (requires credentials) or text-scan fallback",
                "extracts":  ["rooms", "text_scan"],
                "max_size":  "100 MB",
                "notes":     (
                    "Full parsing requires APS credentials (free at aps.autodesk.com). "
                    "Without credentials: text-scan fallback. "
                    "Recommended: export IFC from Revit for full parsing."
                ),
            },
        ],
        "workflow": (
            "1. Upload drawing → GET parsed data + preview PNG. "
            "2. Preview PNG is auto-fed into NIM Vision (Stage 1). "
            "3. Extracted rooms/dims/layers injected into LLM manifest prompt (Stage 2). "
            "4. Generate IBC-compliant drawing set."
        ),
    }


# ---------------------------------------------------------------------------
# Floor Plan Generator endpoint
# ---------------------------------------------------------------------------

class FloorPlanRequest(BaseModel):
    description:   str  = Field(..., min_length=10, description="Natural language floor plan description")
    project_name:  str  = "My Project"
    building_type: str  = "Commercial"
    primary_code:  str  = "IBC 2023"
    jurisdiction:  str  = ""
    api_key:       str  = ""   # NVIDIA NIM key for LLM parsing


@app.post("/api/generate/floorplan")
def generate_floorplan(req: FloorPlanRequest):
    """
    Generate a real 2D floor plan from a text description.

    Pipeline:
      1. LLM (Llama 3.1 via NVIDIA NIM) parses description → room program
      2. Python layout algorithm places rooms with IBC-compliant dimensions
      3. Python SVG generator draws walls, doors, windows, dimensions, labels
      4. ezdxf exports AutoCAD DXF

    Returns SVG string + base64 DXF + room list + occupant loads.
    """
    from floorplan_generator import generate_floor_plan
    import base64

    try:
        fp = generate_floor_plan(
            description   = req.description,
            project_name  = req.project_name,
            building_type = req.building_type,
            primary_code  = req.primary_code,
            jurisdiction  = req.jurisdiction,
            api_key       = req.api_key,
        )
    except Exception as exc:
        logger.exception("Floor plan generation error")
        raise HTTPException(500, detail=f"Floor plan generation failed: {exc}")

    if not fp.svg_data:
        raise HTTPException(422, detail="Could not generate floor plan from description. Add more room details.")

    return {
        "project_name":   fp.project_name,
        "building_type":  fp.building_type,
        "building_w_ft":  fp.building_w,
        "building_d_ft":  fp.building_d,
        "total_sqft":     fp.total_sqft,
        "occupant_load":  fp.occupant_load,
        "room_count":     len(fp.rooms),
        "rooms": [{
            "name":         r.name,
            "sqft":         r.sqft,
            "width_ft":     r.width,
            "depth_ft":     r.depth,
            "zone":         r.zone,
            "occupant_load": r.occupant_load,
        } for r in fp.rooms],
        "svg_data":       fp.svg_data,
        "dxf_b64":        base64.b64encode(fp.dxf_data).decode() if fp.dxf_data else "",
        "parser_mode":    "llm" if req.api_key else "keyword",
        "warnings":       fp.warnings,
        "program":        fp.program,
    }
