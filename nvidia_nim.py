"""
nvidia_nim.py
=============
NVIDIA NIM Integration – 3-Stage Architectural Drawing Pipeline

Stage 1 – Vision Analysis  : meta/llama-3.2-90b-vision-instruct
           Accepts uploaded sketch / photo / floor plan image (base64 or URL)
           Extracts spatial intent, room labels, dimensions, structural hints

Stage 2 – LLM Manifest     : meta/llama-3.1-70b-instruct  
           Converts vision analysis + project spec → full IBC-compliant
           drawing manifest (JSON) with sheet list, notes, code refs

Stage 3 – Image Generation : stabilityai/stable-diffusion-xl
           Renders a 2D architectural drawing SVG prompt for each sheet type
           Returns base64 PNG per sheet

All endpoints use NVIDIA's cloud-hosted NIM API at:
  https://integrate.api.nvidia.com/v1      (LLM / Vision)
  https://ai.api.nvidia.com/v1/genai/...   (Image Gen)

API key obtained free at: https://build.nvidia.com
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models import (
    DrawingOutput,
    DrawingSet,
    EngineProvider,
    ProjectSpecification,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NVIDIA NIM Endpoint Constants
# ---------------------------------------------------------------------------

NIM_BASE          = "https://integrate.api.nvidia.com/v1"
NIM_CHAT          = f"{NIM_BASE}/chat/completions"
NIM_IMG_GEN       = "https://ai.api.nvidia.com/v1/genai/stabilityai/stable-diffusion-xl"

# Models
MODEL_VISION      = "meta/llama-3.2-90b-vision-instruct"
MODEL_LLM         = "meta/llama-3.1-70b-instruct"
MODEL_NEMOTRON    = "nvidia/llama-3.1-nemotron-70b-instruct"   # fallback

TIMEOUT           = 90.0   # seconds per request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(url: str, payload: Dict[str, Any], api_key: str,
          timeout: float = TIMEOUT) -> Dict[str, Any]:
    """Minimal urllib POST → parsed JSON. Raises on HTTP error or timeout."""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(
            f"NVIDIA NIM HTTP {exc.code} from {url}: {body_text}"
        ) from exc
    except TimeoutError as exc:
        raise TimeoutError(f"NVIDIA NIM timed out after {timeout}s ({url})") from exc


def _extract_text(response: Dict[str, Any]) -> str:
    """Pull assistant text from a /v1/chat/completions response."""
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected NIM response structure: {exc}") from exc


# ---------------------------------------------------------------------------
# Stage 1 – Vision Analysis
# ---------------------------------------------------------------------------

def analyze_sketch(
    image_b64: str,
    media_type: str,
    api_key: str,
) -> str:
    """
    Send a base64-encoded image to Llama 3.2 Vision.
    Returns a structured text description of the architectural sketch.
    """
    logger.info("Stage 1 – Vision analysis with %s", MODEL_VISION)

    data_url = f"data:{media_type};base64,{image_b64}"

    payload = {
        "model": MODEL_VISION,
        "max_tokens": 1024,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert architectural AI that analyses hand-drawn "
                    "sketches, photos, and floor plan images. Extract spatial layout, "
                    "room labels, estimated dimensions, structural elements (walls, "
                    "doors, windows, stairs), and any written annotations. "
                    "Respond with a structured JSON object."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Analyse this architectural sketch or floor plan. "
                            "Return JSON with keys: rooms (list of {name, approx_sqft}), "
                            "structural_elements (list), estimated_total_sqft, "
                            "stories_visible, notes (list of any text annotations), "
                            "sketch_quality (clear/rough/schematic)."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ],
    }

    response = _post(NIM_CHAT, payload, api_key)
    result   = _extract_text(response)
    logger.info("Stage 1 complete – vision analysis returned %d chars", len(result))
    return result


# ---------------------------------------------------------------------------
# Stage 2 – LLM Drawing Manifest
# ---------------------------------------------------------------------------

def generate_manifest(
    spec: ProjectSpecification,
    vision_analysis: Optional[str],
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    Use Llama 3.1 70B to generate a full IBC-compliant drawing manifest.
    Returns a list of drawing dicts ready to become DrawingOutput objects.
    """
    logger.info("Stage 2 – LLM manifest with %s", MODEL_LLM)

    vision_section = ""
    if vision_analysis:
        vision_section = (
            f"\n\n## Uploaded Sketch Analysis\n"
            f"The user uploaded an architectural sketch. Vision AI extracted:\n"
            f"{vision_analysis}\n"
            f"Incorporate these spatial details into the drawing manifest.\n"
        )

    drawing_list = ", ".join(d.value for d in spec.drawing_sets)
    amendments   = "; ".join(spec.jurisdiction.local_amendments) or "None"

    system_prompt = (
        "You are a licensed architect and construction document specialist. "
        "Generate precise, IBC 2023-compliant drawing manifests in JSON format only. "
        "No preamble, no markdown fences — pure JSON array."
    )

    user_prompt = f"""Generate a complete construction drawing manifest for this project:

PROJECT DETAILS
───────────────
Name              : {spec.project_name}
Building Type     : {spec.building_type.value}
Occupancy Group   : {spec.occupancy_group.value}
Construction Type : {spec.construction_type.value}
Primary Code      : {spec.primary_code.value}
Jurisdiction      : {spec.jurisdiction.display_name()}
Adopted Code      : {spec.jurisdiction.adopted_building_code.value}
Local Amendments  : {amendments}
Seismic SDC       : {spec.jurisdiction.seismic_design_category}
Wind Exposure     : {spec.jurisdiction.wind_exposure_category}
Wind Speed        : {spec.jurisdiction.wind_speed_mph or 'N/A'} mph
Snow Load         : {spec.jurisdiction.snow_load_psf or 'N/A'} psf
Gross Area        : {f'{spec.gross_sq_ft:,.0f} sq ft' if spec.gross_sq_ft else 'Not specified'}
Stories           : {spec.num_stories or 'Not specified'}
Height            : {f'{spec.building_height_ft:.1f} ft' if spec.building_height_ft else 'Not specified'}
Occupant Load     : {spec.occupant_load or 'Not specified'}
Sprinklered       : {'Yes (NFPA 13)' if spec.sprinklered else 'No'}
Accessibility     : {spec.accessibility_standard}
Drawing Sets      : {drawing_list}
{vision_section}
INSTRUCTIONS
────────────
Return a JSON array. Each element must have:
  - sheet_number   : string  (e.g. "A1.0", "S2.0", "M3.0")
  - title          : string  (descriptive sheet title)
  - sheet_type     : string  (must match one of: {drawing_list})
  - discipline     : string  (Architecture / Structural / MEP / Civil / Fire Protection)
  - scale          : string  (e.g. "1/8\" = 1'-0\"")
  - key_notes      : array of strings (3-5 IBC-specific notes for this sheet)
  - code_sections  : array of strings (relevant IBC 2023 sections)
  - drawing_prompt : string  (detailed prompt for generating the 2D drawing image,
                              describing what the drawing should visually show,
                              in architectural drafting style)
  - ada_notes      : string  (ADA/accessibility requirements relevant to this sheet)

Generate one sheet entry per requested drawing set. Be technically precise."""

    payload = {
        "model": MODEL_LLM,
        "max_tokens": 4096,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
    }

    response = _post(NIM_CHAT, payload, api_key)
    raw_text = _extract_text(response)

    # Parse JSON – strip any accidental markdown fences
    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
        clean = clean.strip()
    if clean.endswith("```"):
        clean = clean[:-3].strip()

    try:
        drawings = json.loads(clean)
        if not isinstance(drawings, list):
            drawings = drawings.get("drawings", [drawings])
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM manifest JSON: %s\nRaw: %s", exc, clean[:500])
        # Fallback: create basic manifest from spec
        drawings = _fallback_manifest(spec)

    logger.info("Stage 2 complete – %d sheet(s) in manifest", len(drawings))
    return drawings


def _fallback_manifest(spec: ProjectSpecification) -> List[Dict[str, Any]]:
    """Basic manifest when LLM JSON parsing fails."""
    prefixes = {
        DrawingSet.SITE:          ("C", "Civil"),
        DrawingSet.FLOOR_PLAN:    ("A", "Architecture"),
        DrawingSet.ELEVATIONS:    ("A", "Architecture"),
        DrawingSet.SECTIONS:      ("A", "Architecture"),
        DrawingSet.DETAILS:       ("A", "Architecture"),
        DrawingSet.STRUCTURAL:    ("S", "Structural"),
        DrawingSet.MEP:           ("M", "MEP"),
        DrawingSet.FIRE_LIFE:     ("FP", "Fire Protection"),
        DrawingSet.ACCESSIBILITY: ("AC", "Architecture"),
        DrawingSet.FULL_SET:      ("A", "Architecture"),
    }
    result = []
    for i, ds in enumerate(spec.drawing_sets, 1):
        prefix, disc = prefixes.get(ds, ("X", "Architecture"))
        result.append({
            "sheet_number":   f"{prefix}{i}.0",
            "title":          f"{ds.value} – {spec.project_name}",
            "sheet_type":     ds.value,
            "discipline":     disc,
            "scale":          "1/8\" = 1'-0\"",
            "key_notes":      [f"Per {spec.primary_code.value}", "See general notes"],
            "code_sections":  [spec.primary_code.value],
            "drawing_prompt": (
                f"2D architectural {ds.value.lower()} drawing, "
                f"{spec.occupancy_group.value} occupancy, "
                f"{spec.construction_type.value}, technical drafting style, "
                f"clean linework, black and white, scale notation"
            ),
            "ada_notes":      "Verify ADA compliance per ICC A117.1-2017",
        })
    return result


# ---------------------------------------------------------------------------
# Stage 3 – Image Generation
# ---------------------------------------------------------------------------

def generate_drawing_image(
    drawing_prompt: str,
    sheet_number: str,
    api_key: str,
) -> Optional[str]:
    """
    Generate a 2D architectural drawing image via SDXL NIM.
    Returns base64-encoded PNG string, or None if generation fails.
    """
    logger.info("Stage 3 – Image generation for sheet %s", sheet_number)

    full_prompt = (
        f"Professional 2D architectural drawing, {drawing_prompt}, "
        "technical drafting, blueprint style, clean precise linework, "
        "black lines on white background, dimension annotations, "
        "north arrow, scale bar, title block, high resolution"
    )

    payload = {
        "text_prompts": [
            {"text": full_prompt,                         "weight": 1.0},
            {"text": "3D render, photo, sketch, color",   "weight": -1.0},
        ],
        "cfg_scale":     12,
        "sampler":       "K_DPM_2_ANCESTRAL",
        "seed":          42,
        "steps":         30,
        "width":         1024,
        "height":        768,
    }

    try:
        response = _post(NIM_IMG_GEN, payload, api_key, timeout=60.0)
        artifacts = response.get("artifacts", [])
        if artifacts and artifacts[0].get("base64"):
            logger.info("Stage 3 complete – image generated for %s", sheet_number)
            return artifacts[0]["base64"]
    except Exception as exc:
        logger.warning("Image generation failed for sheet %s: %s", sheet_number, exc)

    return None


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class NIMPipelineResult:
    """Full result from running the 3-stage NIM pipeline."""
    drawings:         List[DrawingOutput] = field(default_factory=list)
    vision_analysis:  Optional[str]       = None
    stage_timings:    Dict[str, float]    = field(default_factory=dict)
    errors:           List[str]           = field(default_factory=list)


def run_nim_pipeline(
    spec:             ProjectSpecification,
    api_key:          str,
    image_b64:        Optional[str]  = None,
    image_media_type: str            = "image/jpeg",
    generate_images:  bool           = True,
) -> NIMPipelineResult:
    """
    Run the full 3-stage NVIDIA NIM pipeline.

    Args:
        spec:              Project specification
        api_key:           NVIDIA NIM API key (from build.nvidia.com)
        image_b64:         Optional base64-encoded sketch/floor plan image
        image_media_type:  MIME type of image (image/jpeg or image/png)
        generate_images:   Whether to run Stage 3 image generation

    Returns:
        NIMPipelineResult with DrawingOutput list and diagnostics
    """
    result = NIMPipelineResult()
    t_total = time.time()

    # ── Stage 1: Vision analysis (optional) ─────────────────────────────
    if image_b64:
        t0 = time.time()
        try:
            result.vision_analysis = analyze_sketch(image_b64, image_media_type, api_key)
        except Exception as exc:
            logger.warning("Stage 1 failed: %s – continuing without vision", exc)
            result.errors.append(f"Vision analysis skipped: {exc}")
        result.stage_timings["vision"] = round(time.time() - t0, 2)

    # ── Stage 2: LLM manifest ────────────────────────────────────────────
    t0 = time.time()
    try:
        manifest = generate_manifest(spec, result.vision_analysis, api_key)
    except Exception as exc:
        logger.error("Stage 2 failed: %s – using fallback manifest", exc)
        result.errors.append(f"LLM manifest failed: {exc}")
        manifest = _fallback_manifest(spec)
    result.stage_timings["manifest"] = round(time.time() - t0, 2)

    # ── Stage 3: Image generation ────────────────────────────────────────
    for sheet in manifest:
        t0 = time.time()
        img_b64 = None

        if generate_images and sheet.get("drawing_prompt"):
            try:
                img_b64 = generate_drawing_image(
                    sheet["drawing_prompt"],
                    sheet.get("sheet_number", "X0"),
                    api_key,
                )
            except Exception as exc:
                result.errors.append(
                    f"Image gen failed for {sheet.get('sheet_number')}: {exc}"
                )

        # Resolve DrawingSet enum safely
        sheet_type_str = sheet.get("sheet_type", DrawingSet.FLOOR_PLAN.value)
        try:
            sheet_type = DrawingSet(sheet_type_str)
        except ValueError:
            sheet_type = DrawingSet.FLOOR_PLAN

        drawing = DrawingOutput(
            sheet_type    = sheet_type,
            sheet_number  = sheet.get("sheet_number", "X0.0"),
            title         = sheet.get("title", "Untitled Sheet"),
            format        = "PNG" if img_b64 else "JSON",
            data          = base64.b64decode(img_b64) if img_b64 else None,
            metadata={
                "discipline":    sheet.get("discipline", "Architecture"),
                "scale":         sheet.get("scale", ""),
                "key_notes":     sheet.get("key_notes", []),
                "code_sections": sheet.get("code_sections", []),
                "ada_notes":     sheet.get("ada_notes", ""),
                "has_image":     img_b64 is not None,
                "image_b64":     img_b64 or "",
                "engine":        "NVIDIA NIM",
                "models_used": {
                    "vision":   MODEL_VISION  if image_b64  else None,
                    "llm":      MODEL_LLM,
                    "image_gen": MODEL_LLM if img_b64 else None,
                },
                "generated_at":  datetime.utcnow().isoformat(),
            },
        )
        result.drawings.append(drawing)
        result.stage_timings[f"img_{sheet.get('sheet_number','X')}"] = (
            round(time.time() - t0, 2)
        )

    result.stage_timings["total"] = round(time.time() - t_total, 2)
    logger.info(
        "NIM pipeline complete: %d sheets in %.2fs (errors: %d)",
        len(result.drawings),
        result.stage_timings["total"],
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# Conversational Refinement
# ---------------------------------------------------------------------------

def refine_drawing(
    sheet:        DrawingOutput,
    instruction:  str,
    history:      List[Dict[str, str]],
    api_key:      str,
) -> Tuple[str, DrawingOutput]:
    """
    Allow the user to iteratively refine a generated drawing via chat.
    Returns (assistant_reply, updated_DrawingOutput).
    """
    logger.info("Refining sheet %s: '%s'", sheet.sheet_number, instruction[:60])

    context = (
        f"You are refining architectural drawing sheet {sheet.sheet_number}: "
        f"'{sheet.title}'. Current notes: {sheet.metadata.get('key_notes', [])}. "
        f"Apply the user's requested change and return the updated drawing_prompt "
        f"and key_notes as JSON: {{\"drawing_prompt\": \"...\", \"key_notes\": [...], "
        f"\"reply\": \"...\"}}"
    )

    messages = [{"role": "system", "content": context}]
    messages.extend(history)
    messages.append({"role": "user", "content": instruction})

    payload = {
        "model":       MODEL_LLM,
        "max_tokens":  1024,
        "temperature": 0.3,
        "messages":    messages,
    }

    response = _post(NIM_CHAT, payload, api_key)
    raw      = _extract_text(response)

    try:
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        updates = json.loads(clean)
    except json.JSONDecodeError:
        return raw, sheet

    # Apply updates to the drawing output
    if "key_notes" in updates:
        sheet.metadata["key_notes"]     = updates["key_notes"]
    if "drawing_prompt" in updates:
        sheet.metadata["drawing_prompt"] = updates["drawing_prompt"]

    return updates.get("reply", raw), sheet
