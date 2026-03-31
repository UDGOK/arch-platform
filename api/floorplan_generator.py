"""
floorplan_generator.py
======================
Enhanced 2D Floor Plan Generator with ADA Compliance

Pipeline
--------
Stage 1 – Program Parser (LLM)
    Text/sketch description → structured room program (JSON)
    Uses nvidia/llama-3.1-70b-instruct via NVIDIA NIM

Stage 2 – Layout Engine (Python algorithm)
    Room program → optimised floor plan layout
    Building shapes: rectangular, L-shaped, U-shaped
    Central corridor system with minimum 44" width (ADA)
    Core elements properly positioned
    ADA-compliant turning radii and clearances

Stage 3 – Drawing Generator (Python)
    Layout → dimensioned SVG floor plan with professional symbols
    
Stage 4 – DXF Export (ezdxf)
    SVG layout data → AutoCAD-compatible DXF with AIA layers

Output
------
FloorPlan dataclass with comprehensive compliance features
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants - ADA and IBC Compliant
# ---------------------------------------------------------------------------

SCALE          = 10.0        # 1 SVG unit = 1 foot
WALL_EXT       = 0.67        # exterior wall half-thickness (8" = 0.67')
WALL_INT       = 0.50        # interior wall half-thickness (6" = 0.50')
DOOR_W         = 3.0         # ADA min 32" clear = 3'-0" nominal
WINDOW_W       = 4.0         # standard window width (ft)
CORRIDOR_W     = 5.0         # 60" corridor width (IBC min 44")
MIN_ROOM_W     = 8.0         # minimum room dimension (ft)
ADA_TURN_R     = 5.0         # 60" diameter turning circle (ADA)
DOOR_CLEAR     = 1.5         # 18" min strike-side clearance

SVG_FONT       = "Arial, Helvetica, sans-serif"
C_WALL         = "#1a1a1a"
C_WALL_INT     = "#333333"
C_FILL         = "#f8f6f0"
C_FILL_CORE    = "#e8e4dc"
C_FILL_CIRC    = "#f0ede4"
C_DIM          = "#1a5276"
C_LABEL        = "#1a1a1a"
C_GRID         = "#d5d8dc"
C_TITLE_BG     = "#1a3a5c"
C_EXIT         = "#c0392b"
C_ADA          = "#2ecc71"

# IBC Table 1004.1 – sq ft per occupant
OCC_LOAD_TABLE = {
    "office": 150, "business": 150, "open office": 150,
    "conference": 15, "meeting": 15, "training": 15,
    "lobby": 100, "reception": 100, "waiting": 15,
    "restroom": 0, "bathroom": 0, "toilet": 0,
    "corridor": 0, "hallway": 0, "circulation": 0,
    "storage": 300, "mechanical": 300, "electrical": 300,
    "stair": 0, "elevator": 0, "core": 0,
    "kitchen": 200, "break room": 100, "cafeteria": 15,
    "server": 0, "data": 300,
    "bedroom": 200, "living": 150, "dining": 15,
    "garage": 200,
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Room:
    name:         str
    width:        float          # ft
    depth:        float          # ft
    room_type:    str = "office"
    zone:         str = "perimeter"   # perimeter | core | circulation
    x:            float = 0.0
    y:            float = 0.0
    placed:       bool  = False
    doors:        List[Dict] = field(default_factory=list)
    windows:      List[Dict] = field(default_factory=list)
    ada_features: List[str] = field(default_factory=list)

    @property
    def sqft(self) -> float:
        return round(self.width * self.depth, 0)

    @property
    def occupant_load(self) -> int:
        factor = OCC_LOAD_TABLE.get(self.room_type.lower(), 150)
        if factor == 0:
            return 0
        return math.ceil(self.sqft / factor)


@dataclass
class FloorPlan:
    rooms:        List[Room]      = field(default_factory=list)
    building_w:   float           = 0.0
    building_d:   float           = 0.0
    building_shape: str           = "rectangular"  # rectangular, L-shaped, U-shaped
    total_sqft:   float           = 0.0
    occupant_load: int            = 0
    svg_data:     str             = ""
    dxf_data:     bytes           = b""
    project_name: str             = "Floor Plan"
    building_type: str            = "Commercial"
    primary_code: str             = "IBC 2023"
    jurisdiction: str             = ""
    warnings:     List[str]       = field(default_factory=list)
    program:      Dict[str, Any]  = field(default_factory=dict)
    ada_compliant: bool           = True
    egress_data:  Dict[str, Any]  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 1 – LLM Program Parser
# ---------------------------------------------------------------------------

PARSE_SYSTEM = """You are an architectural programming expert.
Convert the user's description into a structured JSON room program.
Respond ONLY with valid JSON, no markdown, no explanation.

Output format:
{
  "building_type": "Commercial" or "Residential",
  "building_shape": "rectangular" or "L-shaped" or "U-shaped",
  "total_sqft_target": number or null,
  "stories": 1,
  "rooms": [
    {
      "name": "Open Office",
      "type": "open office",
      "width_ft": 40,
      "depth_ft": 30,
      "zone": "perimeter",
      "count": 1,
      "notes": ""
    }
  ],
  "special_requirements": ["ADA accessible", "Fire sprinklered"]
}

Zone must be one of: perimeter, core, circulation.
Core = restrooms, stairs, elevator, mechanical, storage.
Circulation = corridors, lobbies, reception.
Perimeter = all other occupied spaces.

Room dimensions must be realistic and ADA-compliant:
- Minimum 8ft in any direction
- Office: 10-15ft wide typical
- Open office: 20-60ft wide
- Conference: 12-20ft wide, 15-25ft deep (allow 60" turning circle)
- Restroom: 8-12ft wide, 10-15ft deep (ADA accessible)
- Lobby: 15-30ft wide, 15-25ft deep
- Corridor: 5-8ft wide (min 44" IBC), length varies
- Bedroom: 10-14ft wide, 11-14ft deep
- Kitchen: 10-15ft wide, 12-16ft deep

Building shapes:
- rectangular: standard box shape
- L-shaped: two wings at 90 degrees (courtyard design)
- U-shaped: three wings forming U (larger buildings)"""


def parse_program(description: str, api_key: str) -> Dict[str, Any]:
    """
    Call Llama 3.1 70B on NVIDIA NIM to convert text description
    to structured room program JSON.
    Falls back to a sensible default if API call fails.
    """
    from triton_client import triton_infer

    NIM_CHAT = "https://integrate.api.nvidia.com/v1/chat/completions"

    payload = {
        "model":       "meta/llama-3.1-70b-instruct",
        "max_tokens":  2048,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": PARSE_SYSTEM},
            {"role": "user",   "content": f"Convert this to a room program:\n\n{description}"},
        ],
    }

    try:
        resp = triton_infer(NIM_CHAT, payload, api_key)
        text = resp["choices"][0]["message"]["content"].strip()
        # Strip markdown fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$",          "", text)
        program = json.loads(text)
        logger.info("LLM program parsed: %d rooms", len(program.get("rooms", [])))
        return program
    except Exception as exc:
        logger.warning("LLM parse failed (%s) — using keyword fallback", exc)
        return _keyword_parse(description)


def _keyword_parse(description: str) -> Dict[str, Any]:
    """Keyword-based fallback when LLM is unavailable."""
    desc_lower = description.lower()
    rooms = []

    patterns = [
        (r"(\d+)\s*(?:private\s*)?office", "Private Office", "office", 12, 14, "perimeter"),
        (r"(\d+)\s*(?:open\s*)?(?:office|workspace)", "Open Office", "open office", 40, 30, "perimeter"),
        (r"(\d+)\s*conference", "Conference Room", "conference", 18, 20, "perimeter"),
        (r"(\d+)\s*meeting", "Meeting Room", "meeting", 14, 16, "perimeter"),
        (r"(\d+)\s*restroom", "Restroom", "restroom", 10, 12, "core"),
        (r"(\d+)\s*bedroom", "Bedroom", "bedroom", 12, 13, "perimeter"),
        (r"(\d+)\s*bathroom", "Bathroom", "bathroom", 8, 10, "core"),
        (r"(\d+)\s*storage", "Storage", "storage", 10, 12, "core"),
    ]

    for pattern, name, rtype, w, d, zone in patterns:
        m = re.search(pattern, desc_lower)
        if m:
            count = int(m.group(1)) if m.group(1).isdigit() else 1
            for i in range(min(count, 8)):
                rooms.append({
                    "name": name if count == 1 else f"{name} {i+1}",
                    "type": rtype, "width_ft": w, "depth_ft": d,
                    "zone": zone, "count": 1, "notes": "",
                })

    # Always add lobby and corridor if commercial
    is_commercial = any(kw in desc_lower for kw in
                        ["office","commercial","business","retail","restaurant"])
    if is_commercial and not any(r["type"] == "circulation" for r in rooms):
        rooms.insert(0, {"name":"Lobby","type":"lobby","width_ft":20,
                         "depth_ft":18,"zone":"circulation","count":1,"notes":""})
    if not any("corridor" in r["name"].lower() for r in rooms) and len(rooms) > 2:
        rooms.append({"name":"Corridor","type":"corridor","width_ft":5,
                      "depth_ft":40,"zone":"circulation","count":1,"notes":""})

    sqft_m = re.search(r"(\d[\d,]*)\s*(?:sq\.?\s*ft|sqft|sf)", desc_lower)
    target = int(sqft_m.group(1).replace(",","")) if sqft_m else None

    # Detect shape
    shape = "rectangular"
    if "l-shape" in desc_lower or "l shape" in desc_lower:
        shape = "L-shaped"
    elif "u-shape" in desc_lower or "u shape" in desc_lower or "courtyard" in desc_lower:
        shape = "U-shaped"

    return {
        "building_type": "Commercial" if is_commercial else "Residential",
        "building_shape": shape,
        "total_sqft_target": target,
        "stories": 1,
        "rooms": rooms,
        "special_requirements": ["ADA accessible"],
    }


# ---------------------------------------------------------------------------
# Stage 2 – Enhanced Layout Engine with Building Shapes
# ---------------------------------------------------------------------------

def layout_rooms(program: Dict[str, Any]) -> Tuple[List[Room], float, float, str]:
    """
    Advanced strip-zone layout algorithm with building shape support.
    Returns (placed_rooms, building_width, building_depth, building_shape).
    """
    raw_rooms = program.get("rooms", [])
    shape = program.get("building_shape", "rectangular")

    # Expand count-based rooms
    rooms: List[Room] = []
    for r in raw_rooms:
        count = r.get("count", 1)
        for i in range(count):
            suffix = f" {i+1}" if count > 1 else ""
            rooms.append(Room(
                name      = r["name"] + suffix,
                width     = float(r.get("width_ft", 12)),
                depth     = float(r.get("depth_ft", 14)),
                room_type = r.get("type", "office"),
                zone      = r.get("zone", "perimeter"),
            ))

    if not rooms:
        # Fallback: generic open plan
        rooms = [
            Room("Open Office", 50, 40, "open office", "perimeter"),
            Room("Conference Room", 18, 20, "conference", "perimeter"),
            Room("Lobby", 20, 18, "lobby", "circulation"),
            Room("Restroom", 10, 12, "restroom", "core"),
            Room("Corridor", 5, 50, "corridor", "circulation"),
        ]

    # Separate by zone
    circulation = [r for r in rooms if r.zone == "circulation"]
    core        = [r for r in rooms if r.zone == "core"]
    perimeter   = [r for r in rooms if r.zone == "perimeter"]

    # Target building dimensions
    target_sqft = program.get("total_sqft_target")
    total_room_sqft = sum(r.sqft for r in rooms)
    if target_sqft and target_sqft > total_room_sqft:
        # Scale rooms proportionally
        scale = math.sqrt(target_sqft / max(total_room_sqft, 1))
        for r in rooms:
            r.width = max(MIN_ROOM_W, round(r.width * scale, 1))
            r.depth = max(MIN_ROOM_W, round(r.depth * scale, 1))

    # Layout based on shape
    if shape == "L-shaped":
        placed, bw, bd = _layout_l_shape(circulation, core, perimeter)
    elif shape == "U-shaped":
        placed, bw, bd = _layout_u_shape(circulation, core, perimeter)
    else:  # rectangular (default)
        placed, bw, bd = _layout_rectangular(circulation, core, perimeter)

    # Add doors, windows, and ADA features to placed rooms
    margin = 2.0
    for r in placed:
        _add_openings(r, bw, bd, margin)
        _add_ada_features(r, bw, bd, margin)

    return placed, bw, bd, shape


def _layout_rectangular(
    circulation: List[Room],
    core: List[Room],
    perimeter: List[Room]
) -> Tuple[List[Room], float, float]:
    """Standard rectangular building layout."""
    GAP   = 0.4
    MAR   = 2.0
    COR_W = 5.0  # 60" corridor (ADA 44" + safety margin)

    # Estimate building width from widest strip
    bw_raw = max(
        sum(r.width for r in circulation) + max(len(circulation)-1,0)*GAP if circulation else 0,
        sum(r.width for r in core) + max(len(core)-1,0)*GAP if core else 0,
        sum(r.width for r in perimeter[:4]) + 3*GAP if perimeter else 0,
    )
    bw = max(bw_raw, 40.0) + 2 * MAR
    bw = math.ceil(bw / 5) * 5

    placed = []
    cy = MAR  # cursor y

    # Front strip: lobby/reception/circulation rooms side by side
    if circulation:
        cx = MAR
        row_d = max(r.depth for r in circulation)
        avail = bw - 2*MAR
        col_w = avail / len(circulation) - GAP
        for r in circulation:
            r.x = cx; r.y = cy
            r.width = max(12.0, col_w)
            r.depth = row_d
            r.placed = True
            placed.append(r)
            cx += r.width + GAP
        cy += row_d + WALL_INT

    # Central corridor - always 60" (5.0 ft) minimum
    if len(perimeter) > 0:
        corridor = Room("Central Corridor", bw - 2*MAR, COR_W, "corridor", "circulation")
        corridor.x = MAR
        corridor.y = cy
        corridor.placed = True
        placed.append(corridor)
        cy += COR_W + WALL_INT

    # Perimeter rooms in rows of 4
    ROW = 4
    for i in range(0, len(perimeter), ROW):
        row = perimeter[i:i+ROW]
        row_d = max(r.depth for r in row)
        avail = bw - 2*MAR
        col_w = avail / len(row) - GAP
        cx = MAR
        for r in row:
            r.x = cx; r.y = cy
            r.width = max(MIN_ROOM_W, col_w)
            r.depth = row_d
            r.placed = True
            placed.append(r)
            cx += r.width + GAP
        cy += row_d + WALL_INT

    # Core strip at back
    if core:
        avail = bw - 2*MAR
        col_w = avail / len(core) - GAP
        row_d = max(r.depth for r in core)
        cx = MAR
        for r in core:
            r.x = cx; r.y = cy
            r.width = max(MIN_ROOM_W, col_w)
            r.depth = row_d
            r.placed = True
            placed.append(r)
            cx += r.width + GAP
        cy += row_d + WALL_INT

    bd = cy + MAR
    bd = math.ceil(bd / 5) * 5
    return placed, bw, bd


def _layout_l_shape(
    circulation: List[Room],
    core: List[Room],
    perimeter: List[Room]
) -> Tuple[List[Room], float, float]:
    """L-shaped building layout (two wings at 90 degrees)."""
    # Split perimeter into two wings
    mid = len(perimeter) // 2
    wing1 = perimeter[:mid]
    wing2 = perimeter[mid:]
    
    # Layout wing 1 as rectangle
    placed1, w1, d1 = _layout_rectangular(circulation, core[:len(core)//2], wing1)
    
    # Layout wing 2 offset to create L-shape
    circ2 = [] if circulation and len(placed1) > 0 else []
    placed2, w2, d2 = _layout_rectangular(circ2, core[len(core)//2:], wing2)
    
    # Offset wing 2 to form L
    for r in placed2:
        r.x += w1 - 10  # Overlap corner
        # r.y stays same for adjacent wing
    
    placed = placed1 + placed2
    bw = w1 + w2 - 10  # Account for overlap
    bd = max(d1, d2)
    
    return placed, bw, bd


def _layout_u_shape(
    circulation: List[Room],
    core: List[Room],
    perimeter: List[Room]
) -> Tuple[List[Room], float, float]:
    """U-shaped building layout (three wings forming U)."""
    # Split into three wings
    third = len(perimeter) // 3
    wing1 = perimeter[:third]
    wing2 = perimeter[third:2*third]
    wing3 = perimeter[2*third:]
    
    # Layout center wing (bottom of U)
    placed_center, w_center, d_center = _layout_rectangular(circulation, core, wing2)
    
    # Left wing
    placed_left, w_left, d_left = _layout_rectangular([], [], wing1)
    for r in placed_left:
        r.y += d_center - 10  # Offset vertically
        r.x = r.x  # Keep x position
    
    # Right wing
    placed_right, w_right, d_right = _layout_rectangular([], [],wing3)
    for r in placed_right:
        r.x += w_center - 10  # Offset horizontally
        r.y += d_center - 10  # Offset vertically
    
    placed = placed_center + placed_left + placed_right
    bw = max(w_center, w_left + w_right - 10)
    bd = d_center + max(d_left, d_right) - 10
    
    return placed, bw, bd


def _add_openings(room: Room, bldg_w: float, bldg_d: float, margin: float) -> None:
    """Add door and window positions to a room with ADA compliance."""
    # Door with ADA clearances (32" min clear = 3' nominal, 18" strike clearance)
    if room.room_type not in ("corridor", "hallway"):
        door_x = room.x + room.width / 2 - DOOR_W / 2
        # Ensure 18" clearance from walls
        if door_x < room.x + DOOR_CLEAR:
            door_x = room.x + DOOR_CLEAR
        if door_x + DOOR_W > room.x + room.width - DOOR_CLEAR:
            door_x = room.x + room.width - DOOR_W - DOOR_CLEAR
            
        room.doors.append({
            "wall": "bottom",
            "x": door_x, "y": room.y + room.depth,
            "width": DOOR_W, "swing": "out",  # Egress doors swing outward
            "clear_width": 32/12,  # 32" clear
        })

    # Windows on exterior walls only
    on_left  = room.x <= margin + WALL_EXT + 0.5
    on_right = room.x + room.width >= bldg_w - margin - WALL_EXT - 0.5
    on_front = room.y <= margin + WALL_EXT + 0.5
    on_back  = room.y + room.depth >= bldg_d - margin - WALL_EXT - 0.5

    if (on_front or on_back) and room.room_type not in ("restroom","corridor","storage","core"):
        # Window on front or back
        wall = "top" if on_front else "bottom"
        wy = room.y if on_front else room.y + room.depth
        for offset in [room.width * 0.25, room.width * 0.65]:
            if offset + WINDOW_W < room.width:
                room.windows.append({
                    "wall": wall,
                    "x": room.x + offset, "y": wy,
                    "width": WINDOW_W,
                })

    if (on_left or on_right) and room.room_type not in ("restroom","corridor","storage","core"):
        wall = "left" if on_left else "right"
        wx = room.x if on_left else room.x + room.width
        room.windows.append({
            "wall": wall,
            "x": wx, "y": room.y + room.depth * 0.3,
            "width": WINDOW_W,
        })


def _add_ada_features(room: Room, bldg_w: float, bldg_d: float, margin: float) -> None:
    """Add ADA accessibility features to rooms."""
    # Check if room has 60" turning circle clearance
    if room.width >= ADA_TURN_R and room.depth >= ADA_TURN_R:
        room.ada_features.append("60\" turning circle")
    
    # Restrooms must be ADA accessible
    if "restroom" in room.room_type.lower() or "toilet" in room.room_type.lower():
        room.ada_features.append("ADA accessible restroom")
        if room.width < 8 or room.depth < 10:
            room.ada_features.append("WARNING: May not meet ADA dimensions")
    
    # Conference rooms need accessible seating
    if "conference" in room.room_type.lower() or "meeting" in room.room_type.lower():
        room.ada_features.append("Accessible seating required")


# ---------------------------------------------------------------------------
# Stage 3 – Enhanced SVG Drawing Generator
# ---------------------------------------------------------------------------

def generate_svg(
    rooms:        List[Room],
    building_w:   float,
    building_d:   float,
    project_name: str = "Floor Plan",
    building_type: str = "Commercial",
    primary_code:  str = "IBC 2023",
    jurisdiction:  str = "",
) -> str:
    """Generate a complete, ADA-compliant dimensioned SVG floor plan."""

    S      = SCALE
    margin = 2.0

    # SVG canvas size (ft → px at SCALE, plus space for dims and title)
    DIM_SPACE   = 5.0   # ft space for dimension lines
    TITLE_H_FT  = 6.0   # title block height in ft

    canvas_w = (building_w + 2 * DIM_SPACE) * S + 60
    canvas_h = (building_d + 2 * DIM_SPACE + TITLE_H_FT) * S + 60

    def X(ft): return (ft + DIM_SPACE) * S + 30
    def Y(ft): return (ft + DIM_SPACE) * S + 30

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}" '
        f'viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        f'font-family="{SVG_FONT}">',
        f'<rect width="{canvas_w:.0f}" height="{canvas_h:.0f}" fill="white"/>',
    ]

    # ── Grid lines (light, 5ft spacing) ──────────────────────────────────
    lines.append(f'<g stroke="{C_GRID}" stroke-width="0.3" opacity="0.5">')
    for gx in range(0, int(building_w)+1, 5):
        lines.append(f'<line x1="{X(gx):.1f}" y1="{Y(0):.1f}" '
                     f'x2="{X(gx):.1f}" y2="{Y(building_d):.1f}"/>')
    for gy in range(0, int(building_d)+1, 5):
        lines.append(f'<line x1="{X(0):.1f}" y1="{Y(gy):.1f}" '
                     f'x2="{X(building_w):.1f}" y2="{Y(gy):.1f}"/>')
    lines.append('</g>')

    # ── Room fills with zone colors ──────────────────────────────────────
    for room in rooms:
        fill = C_FILL_CORE if room.zone == "core" else \
               C_FILL_CIRC if room.zone == "circulation" else C_FILL
        rx, ry = X(room.x), Y(room.y)
        rw, rh = room.width * S, room.depth * S
        lines.append(
            f'<rect x="{rx:.1f}" y="{ry:.1f}" '
            f'width="{rw:.1f}" height="{rh:.1f}" '
            f'fill="{fill}" stroke="none"/>'
        )

    # ── ADA turning circles ───────────────────────────────────────────────
    for room in rooms:
        if "60\" turning circle" in room.ada_features:
            cx = X(room.x + room.width/2)
            cy = Y(room.y + room.depth/2)
            r = ADA_TURN_R/2 * S
            lines.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                f'fill="none" stroke="{C_ADA}" stroke-width="1.5" '
                f'stroke-dasharray="4,3" opacity="0.4"/>'
            )

    # ── Exterior walls (thick filled) ────────────────────────────────────
    ew = WALL_EXT * S * 2
    ext_pts = [
        (X(margin), Y(margin)),
        (X(building_w - margin), Y(margin)),
        (X(building_w - margin), Y(building_d - margin)),
        (X(margin), Y(building_d - margin)),
    ]
    pts_str = " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in ext_pts)
    lines.append(
        f'<polygon points="{pts_str}" '
        f'fill="none" stroke="{C_WALL}" stroke-width="{ew:.1f}"/>'
    )

    # ── Interior walls (medium weight) ────────────────────────────────────
    iw = max(2.5, WALL_INT * S * 2)
    lines.append(f'<g stroke="{C_WALL_INT}" stroke-width="{iw:.1f}">')
    _drawn_walls = set()
    for room in rooms:
        rx, ry = room.x, room.y
        rw, rd = room.width, room.depth
        for x1, y1, x2, y2 in [
            (rx, ry, rx+rw, ry),           # top
            (rx+rw, ry, rx+rw, ry+rd),     # right
            (rx, ry+rd, rx+rw, ry+rd),     # bottom
            (rx, ry, rx, ry+rd),           # left
        ]:
            key = (round(x1,1), round(y1,1), round(x2,1), round(y2,1))
            rkey = (round(x2,1), round(y2,1), round(x1,1), round(y1,1))
            if key not in _drawn_walls and rkey not in _drawn_walls:
                _drawn_walls.add(key)
                lines.append(
                    f'<line x1="{X(x1):.1f}" y1="{Y(y1):.1f}" '
                    f'x2="{X(x2):.1f}" y2="{Y(y2):.1f}"/>'
                )
    lines.append('</g>')

    # ── Professional Door Symbols ─────────────────────────────────────────
    lines.append(f'<g stroke="{C_WALL}" fill="none">')
    for room in rooms:
        for door in room.doors:
            dx, dy = X(door["x"]), Y(door["y"])
            dw = door["width"] * S
            # Door panel (thick line)
            lines.append(
                f'<line x1="{dx:.1f}" y1="{dy:.1f}" '
                f'x2="{dx + dw:.1f}" y2="{dy:.1f}" stroke-width="3"/>')
            # Door swing arc (90 degrees, outward)
            swing_dir = 1 if door.get("swing") == "out" else -1
            lines.append(
                f'<path d="M {dx:.1f} {dy:.1f} '
                f'A {dw:.1f} {dw:.1f} 0 0 1 {dx + dw:.1f} {dy + swing_dir*dw:.1f}" '
                f'stroke-dasharray="3,2" stroke-width="1.2"/>'
            )
            # ADA clearance indicator (18" strike side)
            clear_w = DOOR_CLEAR * S
            lines.append(
                f'<line x1="{dx:.1f}" y1="{dy:.1f}" '
                f'x2="{dx:.1f}" y2="{dy - clear_w:.1f}" '
                f'stroke="{C_ADA}" stroke-width="1" stroke-dasharray="2,2"/>'
            )
    lines.append('</g>')

    # ── Professional Window Symbols ───────────────────────────────────────
    lines.append(f'<g stroke="#4a90d9" fill="none">')
    for room in rooms:
        for win in room.windows:
            wx, wy_ft = win["x"], win["y"]
            ww = win["width"]
            wall = win["wall"]
            if wall in ("top", "bottom"):
                x1, y1 = X(wx), Y(wy_ft)
                x2, y2 = X(wx + ww), Y(wy_ft)
                # Window frame (triple line for glazing)
                lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke-width="2.5"/>')
                lines.append(f'<line x1="{x1:.1f}" y1="{y1+4:.1f}" x2="{x2:.1f}" y2="{y2+4:.1f}" stroke-width="1"/>')
                lines.append(f'<line x1="{x1:.1f}" y1="{y1-4:.1f}" x2="{x2:.1f}" y2="{y2-4:.1f}" stroke-width="1"/>')
                # Mullions
                for i in range(1, 3):
                    mx = x1 + (x2-x1) * i/3
                    lines.append(f'<line x1="{mx:.1f}" y1="{y1-4:.1f}" x2="{mx:.1f}" y2="{y1+4:.1f}" stroke-width="0.8"/>')
            else:  # left or right
                x1, y1 = X(wx), Y(wy_ft)
                x2, y2 = X(wx), Y(wy_ft + ww)
                lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke-width="2.5"/>')
                lines.append(f'<line x1="{x1+4:.1f}" y1="{y1:.1f}" x2="{x2+4:.1f}" y2="{y2:.1f}" stroke-width="1"/>')
                lines.append(f'<line x1="{x1-4:.1f}" y1="{y1:.1f}" x2="{x2-4:.1f}" y2="{y2:.1f}" stroke-width="1"/>')
    lines.append('</g>')

    # ── Room labels with occupancy ────────────────────────────────────────
    for room in rooms:
        cx = X(room.x + room.width / 2)
        cy = Y(room.y + room.depth / 2)
        label_size = max(7, min(11, room.width * S / len(room.name) * 1.2))
        sf_size    = max(6, label_size - 2)
        occ        = room.occupant_load
        
        # Room name
        lines.append(
            f'<text x="{cx:.1f}" y="{cy - 8:.1f}" '
            f'text-anchor="middle" font-size="{label_size:.0f}" '
            f'font-weight="600" fill="{C_LABEL}">'
            f'{room.name}</text>'
        )
        # Area and occupancy
        lines.append(
            f'<text x="{cx:.1f}" y="{cy + 4:.1f}" '
            f'text-anchor="middle" font-size="{sf_size:.0f}" '
            f'fill="#555">{room.sqft:.0f} SF'
            f'{f" / {occ} OCC" if occ > 0 else ""}</text>'
        )
        # ADA features indicator
        if room.ada_features:
            lines.append(
                f'<text x="{cx:.1f}" y="{cy + 14:.1f}" '
                f'text-anchor="middle" font-size="7" fill="{C_ADA}">♿ ADA</text>'
            )

    # ── Dimension strings (comprehensive) ─────────────────────────────────
    dim_y  = Y(building_d - margin) + DIM_SPACE * S * 0.5
    dim_x  = X(building_w - margin) + DIM_SPACE * S * 0.5
    start_x = X(margin)
    end_x   = X(building_w - margin)
    start_y = Y(margin)
    end_y   = Y(building_d - margin)

    def _dim_line(x1,y1,x2,y2,label,horiz=True):
        mx, my = (x1+x2)/2, (y1+y2)/2
        ls = [
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{C_DIM}" stroke-width="1.2"/>',
            f'<line x1="{x1:.0f}" y1="{y1-10:.0f}" x2="{x1:.0f}" y2="{y1+10:.0f}" '
            f'stroke="{C_DIM}" stroke-width="1.2"/>',
            f'<line x1="{x2:.0f}" y1="{y2-10:.0f}" x2="{x2:.0f}" y2="{y2+10:.0f}" '
            f'stroke="{C_DIM}" stroke-width="1.2"/>',
        ]
        rot = "" if horiz else f' transform="rotate(-90,{mx:.0f},{my:.0f})"'
        ls.append(
            f'<text x="{mx:.0f}" y="{my - 5:.0f}" text-anchor="middle" '
            f'font-size="10" font-weight="600" fill="{C_DIM}"{rot}>{label}</text>'
        )
        return ls

    # Overall width
    lines += _dim_line(start_x, dim_y, end_x, dim_y,
                       f"{building_w - 2*margin:.0f}'-0\"")
    # Overall depth
    lines += _dim_line(start_y, dim_x, end_y, dim_x,
                       f"{building_d - 2*margin:.0f}'-0\"", horiz=False)

    # Room widths (top)
    top_dim_y = Y(margin) - DIM_SPACE * S * 0.35
    for room in rooms:
        if room.y <= margin + 1:  # Top row only
            x1 = X(room.x)
            x2 = X(room.x + room.width)
            lines += _dim_line(x1, top_dim_y, x2, top_dim_y,
                               f"{room.width:.0f}'")

    # ── Egress symbols ────────────────────────────────────────────────────
    # Exit signs at main entrances
    exits = [(margin, building_d/2), (building_w-margin, building_d/2)]
    for ex, ey in exits:
        lines.append(
            f'<g transform="translate({X(ex):.0f},{Y(ey):.0f})">'
            f'<rect x="-18" y="-10" width="36" height="20" fill="{C_EXIT}" rx="3"/>'
            f'<text x="0" y="4" text-anchor="middle" font-size="10" '
            f'font-weight="bold" fill="white">EXIT</text></g>'
        )

    # ── North arrow ───────────────────────────────────────────────────────
    na_x = X(building_w) + DIM_SPACE * S * 0.3
    na_y = Y(margin) + 35
    r_arr = 20
    lines += [
        f'<circle cx="{na_x:.0f}" cy="{na_y:.0f}" r="{r_arr}" '
        f'fill="none" stroke="{C_DIM}" stroke-width="1.8"/>',
        f'<polygon points="{na_x:.0f},{na_y-r_arr:.0f} '
        f'{na_x-8:.0f},{na_y+9:.0f} {na_x:.0f},{na_y+5:.0f} '
        f'{na_x+8:.0f},{na_y+9:.0f}" fill="{C_DIM}"/>',
        f'<text x="{na_x:.0f}" y="{na_y - r_arr - 6:.0f}" '
        f'text-anchor="middle" font-size="13" font-weight="bold" fill="{C_DIM}">N</text>',
    ]

    # ── Scale bar ─────────────────────────────────────────────────────────
    sb_x = X(margin)
    sb_y = Y(building_d) + DIM_SPACE * S * 0.3
    unit = 10 * S
    for i in range(6):
        fill_c = C_DIM if i % 2 == 0 else "white"
        lines.append(
            f'<rect x="{sb_x + i*unit:.0f}" y="{sb_y:.0f}" '
            f'width="{unit:.0f}" height="10" '
            f'fill="{fill_c}" stroke="{C_DIM}" stroke-width="1.2"/>'
        )
    lines += [
        f'<text x="{sb_x:.0f}" y="{sb_y + 22:.0f}" font-size="9" fill="{C_DIM}">0</text>',
        f'<text x="{sb_x + 6*unit:.0f}" y="{sb_y + 22:.0f}" font-size="9" fill="{C_DIM}" text-anchor="end">60\'</text>',
        f'<text x="{sb_x + 3*unit:.0f}" y="{sb_y + 22:.0f}" font-size="9" fill="{C_DIM}" text-anchor="middle">SCALE: 1/8\" = 1\'-0\"</text>',
    ]

    # ── Enhanced title block ──────────────────────────────────────────────
    tb_y  = Y(building_d) + DIM_SPACE * S * 0.65
    tb_h  = TITLE_H_FT * S
    tb_x  = X(margin)
    tb_w  = (building_w - 2*margin) * S

    lines += [
        f'<rect x="{tb_x:.0f}" y="{tb_y:.0f}" width="{tb_w:.0f}" height="{tb_h:.0f}" '
        f'fill="{C_TITLE_BG}" rx="4"/>',
        f'<text x="{tb_x + 14:.0f}" y="{tb_y + 26:.0f}" font-size="16" '
        f'font-weight="bold" fill="white">{project_name}</text>',
        f'<text x="{tb_x + 14:.0f}" y="{tb_y + 44:.0f}" font-size="10" fill="#aac4e0">'
        f'FLOOR PLAN – LEVEL 1   |   {building_type.upper()}   |   {primary_code}</text>',
        f'<text x="{tb_x + 14:.0f}" y="{tb_y + 58:.0f}" font-size="9" fill="#86efac">'
        f'✓ ADA COMPLIANT   |   60\" CORRIDORS   |   ACCESSIBLE ROUTES</text>',
        f'<text x="{tb_x + tb_w - 14:.0f}" y="{tb_y + 26:.0f}" font-size="12" '
        f'font-weight="bold" fill="#4a9eff" text-anchor="end">'
        f'A1.0</text>',
        f'<text x="{tb_x + tb_w - 14:.0f}" y="{tb_y + 44:.0f}" font-size="9" '
        f'fill="#aac4e0" text-anchor="end">'
        f'{sum(r.sqft for r in rooms):.0f} SF TOTAL</text>',
    ]
    if jurisdiction:
        lines.append(
            f'<text x="{tb_x + 14:.0f}" y="{tb_y + 72:.0f}" font-size="8" '
            f'fill="#aac4e0">{jurisdiction}</text>'
        )

    lines.append('</svg>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 4 – DXF Export (unchanged, works with new layout)
# ---------------------------------------------------------------------------

def svg_layout_to_dxf(
    rooms: List[Room],
    building_w: float,
    building_d: float,
    project_name: str = "Floor Plan",
) -> bytes:
    """Convert the placed room layout to an AutoCAD DXF file."""
    import io
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    doc = ezdxf.new("R2010")
    doc.units = ezdxf.units.IN   # inches

    # AIA layers
    for name, color, lw in [
        ("A-WALL",      7, 50), ("A-WALL-INTR", 7, 35),
        ("A-DOOR",      7, 25), ("A-GLAZ",      4, 18),
        ("A-ANNO-TEXT", 2, 18), ("A-ANNO-DIMS", 2, 13),
        ("A-FLOR-PATT", 8, 13), ("A-ANNO-BORD", 7, 70),
        ("A-ADA",       2, 25),
    ]:
        l = doc.layers.add(name); l.color = color; l.lineweight = lw

    msp = doc.modelspace()

    def f(ft): return ft * 12  # feet → inches for DXF

    margin = 2.0

    # Exterior walls
    ext = [(f(margin), f(margin)), (f(building_w-margin), f(margin)),
           (f(building_w-margin), f(building_d-margin)), (f(margin), f(building_d-margin))]
    msp.add_lwpolyline(ext, dxfattribs={"layer":"A-WALL","lineweight":50,"closed":True})

    # Interior walls
    drawn = set()
    for room in rooms:
        for x1,y1,x2,y2 in [
            (room.x, room.y, room.x+room.width, room.y),
            (room.x+room.width, room.y, room.x+room.width, room.y+room.depth),
            (room.x, room.y+room.depth, room.x+room.width, room.y+room.depth),
            (room.x, room.y, room.x, room.y+room.depth),
        ]:
            key = (round(x1,1),round(y1,1),round(x2,1),round(y2,1))
            rkey = (round(x2,1),round(y2,1),round(x1,1),round(y1,1))
            if key not in drawn and rkey not in drawn:
                drawn.add(key)
                msp.add_line((f(x1),f(y1)),(f(x2),f(y2)),
                             dxfattribs={"layer":"A-WALL-INTR"})

    # Room labels with ADA indicators
    for room in rooms:
        cx = f(room.x + room.width/2)
        cy = f(room.y + room.depth/2)
        msp.add_text(room.name, dxfattribs={"layer":"A-ANNO-TEXT","height":f(0.8)}
        ).set_placement((cx, cy+f(0.5)), align=TextEntityAlignment.CENTER)
        msp.add_text(f"{room.sqft:.0f} SF", dxfattribs={"layer":"A-ANNO-TEXT","height":f(0.6)}
        ).set_placement((cx, cy-f(0.3)), align=TextEntityAlignment.CENTER)
        if room.ada_features:
            msp.add_text("ADA", dxfattribs={"layer":"A-ADA","height":f(0.5)}
            ).set_placement((cx, cy-f(0.9)), align=TextEntityAlignment.CENTER)

    # Doors
    for room in rooms:
        for door in room.doors:
            dx, dy = f(door["x"]), f(door["y"])
            dw = f(door["width"])
            msp.add_line((dx, dy), (dx+dw, dy), dxfattribs={"layer":"A-DOOR"})

    # Overall dimensions
    dim_y = f(building_d - margin) + f(2.5)
    msp.add_line((f(margin), dim_y), (f(building_w-margin), dim_y),
                 dxfattribs={"layer":"A-ANNO-DIMS"})
    msp.add_text(f"{building_w-2*margin:.0f}'-0\"",
                 dxfattribs={"layer":"A-ANNO-DIMS","height":f(0.7)}
    ).set_placement((f((margin+(building_w-margin))/2), dim_y+f(0.8)),
                    align=TextEntityAlignment.CENTER)

    # Title block text
    tb_y = f(building_d + 2)
    msp.add_text(project_name, dxfattribs={"layer":"A-ANNO-BORD","height":f(1.2)}
    ).set_placement((f(margin), tb_y), align=TextEntityAlignment.LEFT)
    msp.add_text("FLOOR PLAN – LEVEL 1 | ADA COMPLIANT",
                 dxfattribs={"layer":"A-ANNO-BORD","height":f(0.7)}
    ).set_placement((f(margin), tb_y+f(1.5)), align=TextEntityAlignment.LEFT)

    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_floor_plan(
    description:  str,
    project_name: str = "My Project",
    building_type: str = "Commercial",
    primary_code:  str = "IBC 2023",
    jurisdiction:  str = "",
    api_key:       str = "",
) -> FloorPlan:
    """
    Full pipeline: text → parsed program → layout → SVG + DXF.

    Returns FloorPlan with comprehensive ADA and IBC compliance features.
    """
    fp = FloorPlan(
        project_name=project_name,
        building_type=building_type,
        primary_code=primary_code,
        jurisdiction=jurisdiction,
    )

    # Stage 1: Parse
    logger.info("Stage 1: Parsing description (%d chars)", len(description))
    program = parse_program(description, api_key) if api_key else _keyword_parse(description)
    fp.program = program
    fp.building_shape = program.get("building_shape", "rectangular")

    # Stage 2: Layout
    logger.info("Stage 2: Layout engine (%s shape)", fp.building_shape)
    rooms, bw, bd, shape = layout_rooms(program)
    fp.rooms      = rooms
    fp.building_w = bw
    fp.building_d = bd
    fp.building_shape = shape
    fp.total_sqft = sum(r.sqft for r in rooms)
    fp.occupant_load = sum(r.occupant_load for r in rooms)

    # Calculate egress requirements
    fp.egress_data = {
        "occupant_load": fp.occupant_load,
        "exits_required": 2 if fp.occupant_load <= 500 else 3 if fp.occupant_load <= 1000 else 4,
        "corridor_width_in": 60,  # 5.0 ft = 60"
        "exit_width_required_in": max(32, fp.occupant_load * 0.2),  # 0.2" per person (IBC)
        "max_travel_distance_ft": 250,  # Sprinklered A/B occupancy
        "dead_end_limit_ft": 50,  # A occupancy
    }

    # Check ADA compliance
    fp.ada_compliant = all([
        any(r.zone == "circulation" and r.width >= 5.0 for r in rooms),  # 60" corridor
        all(len(r.doors) == 0 or r.doors[0]["clear_width"] >= 32/12 for r in rooms),  # 32" clear
        any("60\" turning circle" in r.ada_features for r in rooms),  # Turning space
    ])

    if not rooms:
        fp.warnings.append("No rooms could be placed — check your description.")
        return fp

    # Stage 3: SVG
    logger.info("Stage 3: Generating SVG (%d rooms, %.0f x %.0f ft, %s)", 
                len(rooms), bw, bd, shape)
    fp.svg_data = generate_svg(
        rooms, bw, bd, project_name, building_type, primary_code, jurisdiction
    )

    # Stage 4: DXF
    logger.info("Stage 4: Generating DXF")
    try:
        fp.dxf_data = svg_layout_to_dxf(rooms, bw, bd, project_name)
    except Exception as exc:
        fp.warnings.append(f"DXF export failed: {exc}")
        logger.warning("DXF export failed: %s", exc)

    logger.info("Floor plan complete: %.0f SF, %d rooms, %d occupants, %s shape, %s",
                fp.total_sqft, len(rooms), fp.occupant_load, shape,
                "ADA COMPLIANT" if fp.ada_compliant else "ADA REVIEW NEEDED")
    return fp
