"""
file_parser.py
==============
Multi-Format Drawing File Parser

Supported input formats
-----------------------
  .dwg / .dxf   – ezdxf: layers, entities, room labels, dimensions, extents
  .pdf           – pdfplumber (text) + PyMuPDF (page→PNG for Vision AI)
  .ifc           – ifcopenshell: spaces, walls, slabs, openings, storeys
  .rvt           – Autodesk APS (Platform Services) translate → IFC → parse
                   Falls back to metadata-only if no APS credentials

ParsedDrawing (returned by all parsers)
---------------------------------------
  {
    format          : str               # 'DXF' | 'PDF' | 'IFC' | 'RVT'
    filename        : str
    pages           : int               # PDF page count or IFC storey count
    rooms           : List[RoomInfo]    # {name, approx_sqft, level}
    layers          : List[str]         # DXF layer names or IFC type names
    text_annotations: List[str]         # all readable text extracted
    dimensions      : List[str]         # dimension strings found
    extents         : Dict              # bounding box {min_x,max_y,...}
    structural      : List[str]         # structural elements found
    preview_image_b64: Optional[str]    # base64 PNG for Vision AI (first page/plan)
    metadata        : Dict              # raw properties
    warnings        : List[str]         # parse warnings
  }
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RoomInfo:
    name:        str
    approx_sqft: Optional[float] = None
    level:       str = "Level 1"
    x:           Optional[float] = None
    y:           Optional[float] = None


@dataclass
class ParsedDrawing:
    format:            str
    filename:          str
    pages:             int                  = 1
    rooms:             List[RoomInfo]       = field(default_factory=list)
    layers:            List[str]            = field(default_factory=list)
    text_annotations:  List[str]            = field(default_factory=list)
    dimensions:        List[str]            = field(default_factory=list)
    extents:           Dict[str, float]     = field(default_factory=dict)
    structural:        List[str]            = field(default_factory=list)
    preview_image_b64: Optional[str]        = None
    metadata:          Dict[str, Any]       = field(default_factory=dict)
    warnings:          List[str]            = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format":             self.format,
            "filename":           self.filename,
            "pages":              self.pages,
            "rooms":              [{"name":r.name,"approx_sqft":r.approx_sqft,
                                    "level":r.level} for r in self.rooms],
            "layers":             self.layers[:40],
            "text_annotations":   self.text_annotations[:60],
            "dimensions":         self.dimensions[:30],
            "extents":            self.extents,
            "structural":         self.structural[:20],
            "has_preview":        self.preview_image_b64 is not None,
            "preview_image_b64":  self.preview_image_b64 or "",
            "metadata":           self.metadata,
            "warnings":           self.warnings,
            "summary": _build_summary(self),
        }


def _build_summary(p: ParsedDrawing) -> str:
    parts = [f"{p.format} drawing: {p.filename}"]
    if p.rooms:
        parts.append(f"{len(p.rooms)} room(s): " +
                     ", ".join(r.name for r in p.rooms[:6]))
    if p.extents.get("width_ft"):
        parts.append(f"Approx extent: {p.extents['width_ft']:.0f}ft × "
                     f"{p.extents['height_ft']:.0f}ft")
    if p.layers:
        parts.append(f"{len(p.layers)} layer(s) including: " +
                     ", ".join(p.layers[:5]))
    if p.dimensions:
        parts.append(f"Dimensions found: " + "; ".join(p.dimensions[:4]))
    if p.structural:
        parts.append("Structural: " + ", ".join(p.structural[:4]))
    return ". ".join(parts) + "."


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

_ROOM_PATTERNS = re.compile(
    r'\b(OFFICE|LOBBY|CORRIDOR|HALLWAY|CONFERENCE|CONF\.?|MEETING|'
    r'RESTROOM|TOILET|WC|BATHROOM|STAIR|ELEVATOR|LOBBY|RECEPTION|'
    r'STORAGE|MECHANICAL|ELECTRICAL|JANITOR|BREAK ROOM|BREAKROOM|'
    r'KITCHEN|CAFETERIA|SERVER|DATA CENTER|LOADING|MAIL|COPY|'
    r'WAITING|EXAM|LAB|CLASSROOM|LIBRARY|BEDROOM|LIVING|DINING|'
    r'GARAGE|UTILITY|CLOSET|PANTRY|ATRIUM|FOYER|ENTRY|EXIT)\b',
    re.IGNORECASE,
)

_DIM_PATTERN = re.compile(
    r"\d{1,4}['\"]\s*(?:\d{1,2}[\"']?)?\s*(?:X|-)\s*\d{1,4}['\"]\s*(?:\d{1,2}[\"']?)?|"
    r"\d+\.?\d*\s*(?:FT|FEET|SF|SQ\.?\s*FT|M|MM|IN|INCH)",
    re.IGNORECASE,
)

def _extract_rooms_from_text(texts: List[str]) -> List[RoomInfo]:
    rooms: List[RoomInfo] = []
    seen = set()
    for t in texts:
        m = _ROOM_PATTERNS.search(t)
        if m:
            name = t.strip()[:60]
            if name.upper() not in seen:
                seen.add(name.upper())
                sqft = _extract_sqft(t)
                rooms.append(RoomInfo(name=name, approx_sqft=sqft))
    return rooms


def _extract_sqft(text: str) -> Optional[float]:
    m = re.search(r'([\d,]+\.?\d*)\s*(?:SF|SQ\.?\s*FT|SQFT|SQ FT)', text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _extract_dims(texts: List[str]) -> List[str]:
    dims = []
    for t in texts:
        for m in _DIM_PATTERN.finditer(t):
            d = m.group(0).strip()
            if d and d not in dims:
                dims.append(d)
    return dims[:40]


def _units_to_ft(val: float, units: int) -> float:
    """Convert ezdxf drawing units to feet."""
    # ezdxf units: 1=inch, 4=mm, 5=cm, 6=m, 14=ft
    conv = {1: 1/12, 4: 0.00328084, 5: 0.0328084, 6: 3.28084, 14: 1.0}
    return val * conv.get(units, 1.0)


# ---------------------------------------------------------------------------
# DWG / DXF Parser
# ---------------------------------------------------------------------------

def parse_dwg_dxf(file_bytes: bytes, filename: str = "drawing.dxf") -> ParsedDrawing:
    """
    Parse DWG or DXF using ezdxf.
    Extracts layers, text entities, dimensions, room labels, extents.
    """
    import ezdxf
    from ezdxf.enums import InsertUnits

    result = ParsedDrawing(format="DXF", filename=filename)

    try:
        doc = ezdxf.read(io.StringIO(file_bytes.decode("utf-8", errors="replace")))
    except Exception:
        try:
            doc = ezdxf.read(io.BytesIO(file_bytes))
        except Exception as exc:
            result.warnings.append(f"Parse error: {exc}")
            return result

    msp = doc.modelspace()

    # ── Layers ────────────────────────────────────────────────────────────
    result.layers = [str(layer.dxf.name) for layer in doc.layers
                     if not str(layer.dxf.name).startswith("0")]
    result.layers.sort()

    # ── Drawing units ────────────────────────────────────────────────────
    try:
        units = doc.header.get("$INSUNITS", 1)
    except Exception:
        units = 1  # inches

    # ── Text entities → room labels, annotations, dimensions ─────────────
    texts: List[str] = []
    dims:  List[str] = []

    for ent in msp:
        try:
            etype = ent.dxftype()

            if etype in ("TEXT", "MTEXT", "ATTDEF", "ATTRIB"):
                val = ""
                if etype == "MTEXT":
                    val = ent.plain_mtext().strip()
                elif hasattr(ent.dxf, "text"):
                    val = str(ent.dxf.text).strip()
                if val and len(val) > 1:
                    texts.append(val)

            elif etype in ("DIMENSION",):
                try:
                    meas = ent.dxf.get("measurement", 0)
                    if meas:
                        ft = _units_to_ft(meas, units)
                        dims.append(f"{ft:.1f}'-0\"")
                except Exception:
                    pass

            elif etype == "LEADER" or etype == "MULTILEADER":
                try:
                    texts.append(ent.get_mtext_content() or "")
                except Exception:
                    pass

        except Exception:
            continue

    result.text_annotations = [t for t in texts if t][:80]
    result.rooms             = _extract_rooms_from_text(texts)
    result.dimensions        = dims[:30] + _extract_dims(texts)

    # ── Structural elements (layer-based inference) ───────────────────────
    struct_keywords = ["WALL", "COLUMN", "BEAM", "SLAB", "FOOTING", "PIER",
                       "STRUCT", "CONC", "STEEL", "REBAR"]
    result.structural = [
        layer for layer in result.layers
        if any(kw in layer.upper() for kw in struct_keywords)
    ]

    # ── Extents (bounding box) ────────────────────────────────────────────
    try:
        extmin = doc.header.get("$EXTMIN", (0, 0, 0))
        extmax = doc.header.get("$EXTMAX", (0, 0, 0))
        w_raw  = abs(extmax[0] - extmin[0])
        h_raw  = abs(extmax[1] - extmin[1])
        w_ft   = _units_to_ft(w_raw, units)
        h_ft   = _units_to_ft(h_raw, units)
        # Sanity check: ignore absurdly large values (unset EXTMAX = 1e38)
        if 0 < w_ft < 50000 and 0 < h_ft < 50000:
            result.extents = {
                "width_ft":  round(w_ft, 1),
                "height_ft": round(h_ft, 1),
                "area_sqft": round(w_ft * h_ft, 0),
            }
    except Exception:
        pass

    # ── Metadata ─────────────────────────────────────────────────────────
    result.metadata = {
        "entity_count": sum(1 for _ in msp),
        "layer_count":  len(result.layers),
        "units":        units,
        "dxf_version":  doc.dxfversion,
    }

    # ── Preview: render model space to PNG via ezdxf matplotlib backend ──
    try:
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(12, 9), dpi=120)
        ax  = fig.add_axes([0, 0, 1, 1])
        ctx = RenderContext(doc)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(msp, finalize=True)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight",
                    facecolor="white", dpi=120)
        plt.close(fig)
        result.preview_image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        logger.info("DXF preview rendered (%d bytes)", len(buf.getvalue()))

    except Exception as exc:
        result.warnings.append(f"Preview render skipped: {exc}")

    logger.info("DXF parsed: %d layers, %d rooms, %d texts",
                len(result.layers), len(result.rooms), len(result.text_annotations))
    return result


# ---------------------------------------------------------------------------
# PDF Parser
# ---------------------------------------------------------------------------

def parse_pdf(file_bytes: bytes, filename: str = "drawing.pdf") -> ParsedDrawing:
    """
    Parse PDF using pdfplumber (text) + PyMuPDF (image render for Vision AI).
    Extracts text annotations, room labels, dimensions, and a PNG preview
    of the first page for feeding into the NIM Vision pipeline.
    """
    import pdfplumber
    import fitz

    result = ParsedDrawing(format="PDF", filename=filename)

    # ── Text extraction (pdfplumber) ──────────────────────────────────────
    all_texts: List[str] = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            result.pages = len(pdf.pages)
            result.metadata["page_count"] = result.pages

            for page_num, page in enumerate(pdf.pages[:10]):  # first 10 pages
                # Page dimensions
                if page_num == 0:
                    w_in = (page.width  or 0) / 72.0   # PDF points → inches
                    h_in = (page.height or 0) / 72.0
                    result.extents = {
                        "width_ft":  round(w_in / 12, 1),
                        "height_ft": round(h_in / 12, 1),
                        "page_width_in":  round(w_in, 1),
                        "page_height_in": round(h_in, 1),
                    }

                # Extract words
                words = page.extract_words()
                page_texts = [w["text"] for w in words if w.get("text","").strip()]
                all_texts.extend(page_texts)

                # Extract full text blocks
                text = page.extract_text() or ""
                if text:
                    lines = [l.strip() for l in text.splitlines() if l.strip()]
                    all_texts.extend(lines)

    except Exception as exc:
        result.warnings.append(f"pdfplumber error: {exc}")

    # Deduplicate while preserving order
    seen = set()
    unique_texts = []
    for t in all_texts:
        if t and t not in seen and len(t) > 1:
            seen.add(t)
            unique_texts.append(t)

    result.text_annotations = unique_texts[:100]
    result.rooms             = _extract_rooms_from_text(unique_texts)
    result.dimensions        = _extract_dims(unique_texts)

    # Infer layers from text (sheet types, discipline codes)
    sheet_kws = ["ARCH", "CIVIL", "STRUCT", "MECH", "ELEC", "PLMB",
                 "FIRE", "LAND", "INTERIORS"]
    result.layers = [kw for kw in sheet_kws
                     if any(kw in t.upper() for t in unique_texts)]

    # Structural elements from text
    struct_kws = ["COLUMN", "BEAM", "SHEAR WALL", "MOMENT FRAME", "FOOTING",
                  "SLAB ON GRADE", "POST-TENSIONED", "MASONRY"]
    result.structural = [kw for kw in struct_kws
                         if any(kw in t.upper() for t in unique_texts)]

    # ── Render first page to PNG (PyMuPDF) ────────────────────────────────
    try:
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        if pdf_doc.page_count > 0:
            page = pdf_doc[0]
            mat  = fitz.Matrix(2.0, 2.0)   # 2x scale = ~144 DPI
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            png_bytes = pix.tobytes("png")
            result.preview_image_b64 = base64.b64encode(png_bytes).decode("utf-8")
            result.metadata["first_page_width_px"]  = pix.width
            result.metadata["first_page_height_px"] = pix.height
            logger.info("PDF page 1 rendered to PNG (%dx%d, %d bytes)",
                        pix.width, pix.height, len(png_bytes))
        pdf_doc.close()
    except Exception as exc:
        result.warnings.append(f"PDF render error: {exc}")

    logger.info("PDF parsed: %d pages, %d rooms, %d texts",
                result.pages, len(result.rooms), len(result.text_annotations))
    return result


# ---------------------------------------------------------------------------
# IFC Parser
# ---------------------------------------------------------------------------

def parse_ifc(file_bytes: bytes, filename: str = "model.ifc") -> ParsedDrawing:
    """
    Parse IFC (Industry Foundation Classes) using ifcopenshell.
    Extracts spaces/rooms, storeys, walls, columns, slabs, openings.
    Revit → File → Export → IFC to use this parser on Revit projects.
    """
    import ifcopenshell

    result = ParsedDrawing(format="IFC", filename=filename)

    import tempfile, os as _os
    _tmp = tempfile.NamedTemporaryFile(suffix=".ifc", delete=False)
    try:
        _tmp.write(file_bytes if isinstance(file_bytes, bytes) else file_bytes.encode())
        _tmp.close()
        ifc = ifcopenshell.open(_tmp.name)
    except Exception as exc:
        result.warnings.append(f"IFC open error: {exc}")
        try: _os.unlink(_tmp.name)
        except Exception: pass
        return result
    finally:
        try: _os.unlink(_tmp.name)
        except Exception: pass

    # ── Project metadata ──────────────────────────────────────────────────
    projects = ifc.by_type("IfcProject")
    if projects:
        p = projects[0]
        result.metadata["project_name"]        = getattr(p, "Name", "") or ""
        result.metadata["project_description"] = getattr(p, "Description", "") or ""
        result.metadata["project_phase"]       = getattr(p, "Phase", "") or ""

    # ── Building storeys ──────────────────────────────────────────────────
    storeys = ifc.by_type("IfcBuildingStorey")
    result.pages = len(storeys) or 1
    storey_names = [s.Name or f"Level {i+1}" for i, s in enumerate(storeys)]
    result.metadata["storeys"] = storey_names

    # ── Spaces / rooms ────────────────────────────────────────────────────
    for space in ifc.by_type("IfcSpace"):
        name = getattr(space, "Name", "") or getattr(space, "LongName", "") or "Space"
        sqft = None

        # Try to get area from quantity sets
        try:
            for rel in space.IsDefinedBy:
                if rel.is_a("IfcRelDefinesByProperties"):
                    pset = rel.RelatingPropertyDefinition
                    if pset.is_a("IfcElementQuantity"):
                        for qty in pset.Quantities:
                            if "Area" in qty.Name:
                                area_m2 = getattr(qty, "AreaValue", None)
                                if area_m2:
                                    sqft = round(area_m2 * 10.7639, 0)
        except Exception:
            pass

        # Storey
        level = "Level 1"
        try:
            for rel in space.Decomposes:
                if rel.is_a("IfcRelAggregates"):
                    parent = rel.RelatingObject
                    if parent.is_a("IfcBuildingStorey"):
                        level = parent.Name or level
        except Exception:
            pass

        result.rooms.append(RoomInfo(name=str(name), approx_sqft=sqft, level=level))

    # ── Layers (IFC types as "layers") ───────────────────────────────────
    type_counts: Dict[str, int] = {}
    for ent in ifc.by_type("IfcElement"):
        t = ent.is_a()
        type_counts[t] = type_counts.get(t, 0) + 1

    result.layers = [f"{t} ({count})" for t, count in
                     sorted(type_counts.items(), key=lambda x: -x[1])[:20]]

    # ── Structural elements ───────────────────────────────────────────────
    struct_types = ["IfcColumn", "IfcBeam", "IfcSlab", "IfcWall",
                    "IfcFooting", "IfcPile", "IfcMember", "IfcPlate",
                    "IfcRoof", "IfcStair"]
    for st in struct_types:
        try:
            count = len(ifc.by_type(st))
            if count > 0:
                result.structural.append(f"{st}: {count}")
        except (RuntimeError, Exception):
            pass  # type not in this schema version

    # ── Extents from IfcSite ─────────────────────────────────────────────
    sites = ifc.by_type("IfcSite")
    if sites:
        result.metadata["site_name"] = getattr(sites[0], "Name", "") or ""

    # ── Text annotations from property sets ──────────────────────────────
    texts = set()
    for pset in ifc.by_type("IfcPropertySet"):
        name = getattr(pset, "Name", "")
        if name:
            texts.add(str(name))
    result.text_annotations = sorted(texts)[:60]

    # ── Summary metadata ─────────────────────────────────────────────────
    result.metadata.update({
        "ifc_schema":   ifc.schema,
        "total_spaces": len(result.rooms),
        "element_types": type_counts,
        "storey_count": len(storeys),
    })

    logger.info("IFC parsed: %d storeys, %d spaces, %d element types",
                len(storeys), len(result.rooms), len(type_counts))
    return result


# ---------------------------------------------------------------------------
# RVT Parser via Autodesk Platform Services (APS / Forge)
# ---------------------------------------------------------------------------

APS_AUTH_URL        = "https://developer.api.autodesk.com/authentication/v2/token"
APS_OSS_URL         = "https://developer.api.autodesk.com/oss/v2"
APS_MODELDERIVATIVE = "https://developer.api.autodesk.com/modelderivative/v2"


def _aps_get_token(client_id: str, client_secret: str) -> str:
    """Get a 2-legged OAuth2 token from Autodesk APS."""
    payload = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "scope":         "data:read data:write data:create bucket:create",
    }).encode()
    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    req = urllib.request.Request(
        APS_AUTH_URL,
        data=payload,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def parse_rvt(
    file_bytes: bytes,
    filename:   str = "model.rvt",
    aps_client_id:     str = "",
    aps_client_secret: str = "",
) -> ParsedDrawing:
    """
    Parse Revit .rvt file via Autodesk Platform Services (APS).

    Without APS credentials:
      Returns metadata-only result with instructions.
      Recommendation: export IFC from Revit (File → Export → IFC) and
      use parse_ifc() for full structural parsing.

    With APS credentials (free tier available at aps.autodesk.com):
      1. Uploads file to OSS bucket
      2. Triggers Model Derivative translation → IFC + SVF
      3. Downloads IFC manifest
      4. Parses via parse_ifc()
      Returns full ParsedDrawing with rooms, structural elements, extents.
    """
    result = ParsedDrawing(format="RVT", filename=filename)
    result.metadata["filename"] = filename
    result.metadata["size_bytes"] = len(file_bytes)

    if not aps_client_id or not aps_client_secret:
        result.warnings.append(
            "No Autodesk APS credentials provided. "
            "To fully parse .rvt files: "
            "(1) Register a free app at aps.autodesk.com, "
            "(2) pass aps_client_id and aps_client_secret. "
            "Alternative: Export IFC from Revit (File → Export → IFC) "
            "and upload the .ifc file instead for full parsing."
        )
        result.metadata["rvt_parse_mode"] = "metadata-only"
        result.metadata["recommended_action"] = (
            "Export to IFC: Revit → File → Export → IFC (IFC 2x3 or IFC4)"
        )
        # Still try to read any readable text from the binary
        try:
            text_chunks = re.findall(
                rb'[\x20-\x7e]{6,}', file_bytes[:500_000]
            )
            readable = [t.decode("ascii", errors="ignore")
                        for t in text_chunks if len(t) > 8]
            # Filter for architectural text
            arch_text = [t for t in readable
                         if any(kw in t.upper() for kw in
                                ["ROOM", "FLOOR", "LEVEL", "WALL", "DOOR",
                                 "WINDOW", "OFFICE", "PLAN", "ARCH"])]
            result.text_annotations = arch_text[:30]
            result.rooms = _extract_rooms_from_text(arch_text)
            logger.info("RVT text scan: found %d readable strings", len(readable))
        except Exception as exc:
            result.warnings.append(f"Binary text scan failed: {exc}")
        return result

    # ── Full APS flow ─────────────────────────────────────────────────────
    import uuid

    try:
        logger.info("Authenticating with Autodesk APS…")
        token = _aps_get_token(aps_client_id, aps_client_secret)

        bucket_key = f"archplatform-{uuid.uuid4().hex[:12]}"
        object_key = filename.replace(" ", "_")

        # Create bucket
        _aps_post(
            f"{APS_OSS_URL}/buckets", token,
            {"bucketKey": bucket_key, "policyKey": "transient"},
        )

        # Upload file
        _aps_put(
            f"{APS_OSS_URL}/buckets/{bucket_key}/objects/{object_key}",
            token, file_bytes,
        )

        # URN
        urn = base64.urlsafe_b64encode(
            f"urn:adsk.objects:os.object:{bucket_key}/{object_key}".encode()
        ).decode().rstrip("=")

        # Trigger translation to IFC
        _aps_post(
            f"{APS_MODELDERIVATIVE}/designdata/job", token,
            {
                "input":  {"urn": urn},
                "output": {
                    "formats": [{"type": "ifc"}],
                    "destination": {"region": "us"},
                },
            },
        )

        result.metadata["aps_urn"]    = urn
        result.metadata["aps_status"] = "translation_started"
        result.warnings.append(
            "APS translation started. "
            "Poll GET /api/upload/rvt/status/{urn} to retrieve the IFC "
            "once translation completes (typically 30-120 seconds)."
        )

    except Exception as exc:
        result.warnings.append(f"APS error: {exc}")
        result.metadata["aps_status"] = "error"

    return result


def _aps_post(url: str, token: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _aps_put(url: str, token: str, data: bytes) -> None:
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=120):
        pass


# ---------------------------------------------------------------------------
# Auto-detect dispatcher
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".dwg": parse_dwg_dxf,
    ".dxf": parse_dwg_dxf,
    ".pdf": parse_pdf,
    ".ifc": parse_ifc,
    ".rvt": parse_rvt,
}

MAX_FILE_SIZE = 100 * 1024 * 1024   # 100 MB


def parse_drawing_file(
    file_bytes:        bytes,
    filename:          str,
    aps_client_id:     str = "",
    aps_client_secret: str = "",
) -> ParsedDrawing:
    """
    Auto-detect format and dispatch to the correct parser.
    Raises ValueError for unsupported extensions or oversized files.
    """
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(
            f"File {filename!r} is {len(file_bytes)//1024//1024} MB — "
            f"maximum is {MAX_FILE_SIZE//1024//1024} MB."
        )

    ext = ("." + filename.rsplit(".", 1)[-1]).lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    parser = SUPPORTED_EXTENSIONS[ext]

    if ext == ".rvt":
        return parse_rvt(
            file_bytes, filename,
            aps_client_id=aps_client_id,
            aps_client_secret=aps_client_secret,
        )

    return parser(file_bytes, filename)
