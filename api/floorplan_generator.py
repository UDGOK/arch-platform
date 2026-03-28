"""
floorplan_generator.py
======================
Real 2D Floor Plan Generator

Pipeline
--------
Stage 1 – Program Parser (LLM)
    Text/sketch description → structured room program (JSON)
    Uses nvidia/llama-3.1-70b-instruct via NVIDIA NIM

Stage 2 – Layout Engine (Python algorithm)
    Room program → optimised floor plan layout
    Strip-zone placement, corridor routing, IBC egress compliance

Stage 3 – Drawing Generator (Python)
    Layout → dimensioned SVG floor plan
    Proper wall thickness, door swings, window breaks,
    dimension strings, room labels with SF, north arrow,
    scale bar, title block

Stage 4 – DXF Export (ezdxf)
    SVG layout data → AutoCAD-compatible DXF with AIA layers

Output
------
FloorPlan dataclass:
    svg_data        : str   (complete SVG string)
    dxf_data        : bytes (AutoCAD DXF)
    rooms           : list  (placed rooms with coordinates)
    total_sqft      : float
    warnings        : list
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
# Constants
# ---------------------------------------------------------------------------

SCALE          = 10.0        # 1 SVG unit = 1 foot
WALL_EXT       = 0.5         # exterior wall half-thickness (ft)
WALL_INT       = 0.33        # interior wall half-thickness (ft)
DOOR_W         = 3.0         # standard door width (ft)
WINDOW_W       = 4.0         # standard window width (ft)
CORRIDOR_W     = 5.0         # corridor width (ft, IBC min 44")
MIN_ROOM_W     = 8.0         # minimum room dimension (ft)

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


# ---------------------------------------------------------------------------
# Stage 1 – LLM Program Parser
# ---------------------------------------------------------------------------

PARSE_SYSTEM = """You are an architectural programming expert.
Convert the user's description into a structured JSON room program.
Respond ONLY with valid JSON, no markdown, no explanation.

Output format:
{
  "building_type": "Commercial" or "Residential",
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
  "special_requirements": []
}

Zone must be one of: perimeter, core, circulation.
Core = restrooms, stairs, elevator, mechanical, storage.
Circulation = corridors, lobbies, reception.
Perimeter = all other occupied spaces.

Room dimensions must be realistic:
- Minimum 8ft in any direction
- Office: 10-15ft wide typical
- Open office: 20-60ft wide
- Conference: 12-20ft wide, 15-25ft deep
- Restroom: 8-12ft wide, 10-15ft deep
- Lobby: 15-30ft wide, 15-25ft deep
- Corridor: 5-8ft wide, length varies
- Bedroom: 10-14ft wide, 11-14ft deep
- Kitchen: 10-15ft wide, 12-16ft deep"""


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

    return {
        "building_type": "Commercial" if is_commercial else "Residential",
        "total_sqft_target": target,
        "stories": 1,
        "rooms": rooms,
        "special_requirements": [],
    }


# ---------------------------------------------------------------------------
# Stage 2 – Layout Engine
# ---------------------------------------------------------------------------

def layout_rooms(program: Dict[str, Any]) -> Tuple[List[Room], float, float]:
    """
    Strip-zone layout algorithm.
    Returns (placed_rooms, building_width, building_depth).
    """
    raw_rooms = program.get("rooms", [])

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

    # ── Layout: front strip (circulation) + middle strips (perimeter) + back (core)
    # Building width = max strip width, depth = sum of strip depths

    margin = 2.0   # exterior clearance

    # Front strip: lobby/reception/circulation rooms side by side
    front_rooms = circulation if circulation else []
    front_w = sum(r.width for r in front_rooms) + (len(front_rooms)-1) * WALL_INT if front_rooms else 0
    front_d = max((r.depth for r in front_rooms), default=0)

    # Core strip: restrooms, stairs, storage across back
    back_rooms = core if core else []
    back_w = sum(r.width for r in back_rooms) + (len(back_rooms)-1) * WALL_INT if back_rooms else 0
    back_d = max((r.depth for r in back_rooms), default=0)

    # Middle: perimeter rooms
    building_w = max(front_w, back_w,
                     sum(r.width for r in perimeter[:5]) + (len(perimeter[:5])-1)*WALL_INT,
                     40.0)
    building_w = math.ceil(building_w / 5) * 5  # round to 5 ft

    # Corridor: always 5ft wide spanning building width
    corridor = next((r for r in circulation if "corridor" in r.name.lower()), None)
    has_corridor = corridor is not None or len(perimeter) > 1

    # Place rooms ─────────────────────────────────────────────────────────────
    cursor_y = margin + WALL_EXT

    # Front strip
    cursor_x = margin + WALL_EXT
    for r in front_rooms:
        r.x = cursor_x; r.y = cursor_y; r.placed = True
        cursor_x += r.width + WALL_INT

    if front_rooms:
        cursor_y += front_d + WALL_INT

    # Corridor if needed
    if has_corridor and corridor:
        corridor.x = margin + WALL_EXT
        corridor.y = cursor_y
        corridor.width = building_w - 2*(margin + WALL_EXT)
        corridor.placed = True
        cursor_y += CORRIDOR_W + WALL_INT

    # Perimeter rooms in rows of ~4
    ROW = 4
    for row_start in range(0, len(perimeter), ROW):
        row = perimeter[row_start:row_start+ROW]
        row_d = max(r.depth for r in row)
        # Distribute widths evenly across building width
        avail_w = building_w - 2*(margin + WALL_EXT)
        col_w = avail_w / len(row) - WALL_INT
        cursor_x = margin + WALL_EXT
        for r in row:
            r.x = cursor_x; r.y = cursor_y
            r.width = max(MIN_ROOM_W, col_w)
            r.depth = row_d
            r.placed = True
            cursor_x += r.width + WALL_INT
        cursor_y += row_d + WALL_INT

    # Core/back strip
    if back_rooms:
        # Fit evenly
        avail_w = building_w - 2*(margin + WALL_EXT)
        col_w = avail_w / len(back_rooms) - WALL_INT
        cursor_x = margin + WALL_EXT
        for r in back_rooms:
            r.x = cursor_x; r.y = cursor_y
            r.width = max(MIN_ROOM_W, col_w)
            r.placed = True
            cursor_x += r.width + WALL_INT
        cursor_y += max(r.depth for r in back_rooms) + WALL_INT

    building_d = cursor_y + margin + WALL_EXT
    building_d = math.ceil(building_d / 5) * 5

    # Add doors and windows to placed rooms
    placed = [r for r in rooms if r.placed]
    for r in placed:
        _add_openings(r, building_w, building_d, margin)

    return placed, building_w, building_d


def _add_openings(room: Room, bldg_w: float, bldg_d: float, margin: float) -> None:
    """Add door and window positions to a room."""
    # Door on bottom wall (facing corridor/lobby side)
    if room.room_type not in ("corridor", "hallway"):
        door_x = room.x + room.width / 2 - DOOR_W / 2
        room.doors.append({
            "wall": "bottom",
            "x": door_x, "y": room.y + room.depth,
            "width": DOOR_W, "swing": "in",
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


# ---------------------------------------------------------------------------
# Stage 3 – SVG Drawing Generator
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
    """Generate a complete, dimensioned SVG floor plan."""

    S      = SCALE
    margin = 2.0

    # SVG canvas size (ft → px at SCALE, plus space for dims and title)
    DIM_SPACE   = 4.0   # ft space for dimension lines
    TITLE_H_FT  = 5.0   # title block height in ft

    canvas_w = (building_w + 2 * DIM_SPACE) * S + 40
    canvas_h = (building_d + 2 * DIM_SPACE + TITLE_H_FT) * S + 40

    def X(ft): return (ft + DIM_SPACE) * S + 20
    def Y(ft): return (ft + DIM_SPACE) * S + 20

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

    # ── Room fills ────────────────────────────────────────────────────────
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

    # ── Exterior walls ────────────────────────────────────────────────────
    ew = WALL_EXT * S
    ext_pts = [
        (X(margin), Y(margin)),
        (X(building_w - margin), Y(margin)),
        (X(building_w - margin), Y(building_d - margin)),
        (X(margin), Y(building_d - margin)),
    ]
    pts_str = " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in ext_pts)
    lines.append(
        f'<polygon points="{pts_str}" '
        f'fill="none" stroke="{C_WALL}" stroke-width="{ew*2:.1f}"/>'
    )

    # ── Interior walls ────────────────────────────────────────────────────
    iw = max(1.5, WALL_INT * S)
    lines.append(f'<g stroke="{C_WALL_INT}" stroke-width="{iw:.1f}">')
    _drawn_walls = set()
    for room in rooms:
        rx, ry = room.x, room.y
        rw, rd = room.width, room.depth
        for x1, y1, x2, y2 in [
            (rx, ry, rx+rw, ry),
            (rx+rw, ry, rx+rw, ry+rd),
            (rx, ry+rd, rx+rw, ry+rd),
            (rx, ry, rx, ry+rd),
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

    # ── Doors ─────────────────────────────────────────────────────────────
    lines.append(f'<g stroke="{C_WALL}" stroke-width="1" fill="none">')
    for room in rooms:
        for door in room.doors:
            dx, dy = X(door["x"]), Y(door["y"])
            dw = door["width"] * S
            # Door panel line
            lines.append(
                f'<line x1="{dx:.1f}" y1="{dy:.1f}" '
                f'x2="{dx + dw:.1f}" y2="{dy:.1f}" stroke-width="2"/>'
            )
            # Door swing arc
            lines.append(
                f'<path d="M {dx:.1f} {dy:.1f} '
                f'A {dw:.1f} {dw:.1f} 0 0 1 {dx:.1f} {dy - dw:.1f}" '
                f'stroke-dasharray="3,2" stroke-width="0.8"/>'
            )
    lines.append('</g>')

    # ── Windows ──────────────────────────────────────────────────────────
    lines.append(f'<g stroke="#4a90d9" stroke-width="2" fill="none">')
    for room in rooms:
        for win in room.windows:
            wx, wy_ft = win["x"], win["y"]
            ww = win["width"]
            wall = win["wall"]
            if wall in ("top", "bottom"):
                x1, y1 = X(wx), Y(wy_ft)
                x2, y2 = X(wx + ww), Y(wy_ft)
                # Triple line window symbol
                lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
                lines.append(f'<line x1="{x1:.1f}" y1="{y1+3:.1f}" x2="{x2:.1f}" y2="{y2+3:.1f}" stroke-width="1"/>')
                lines.append(f'<line x1="{x1:.1f}" y1="{y1-3:.1f}" x2="{x2:.1f}" y2="{y2-3:.1f}" stroke-width="1"/>')
            else:
                x1, y1 = X(wx), Y(wy_ft)
                x2, y2 = X(wx), Y(wy_ft + ww)
                lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
                lines.append(f'<line x1="{x1+3:.1f}" y1="{y1:.1f}" x2="{x2+3:.1f}" y2="{y2:.1f}" stroke-width="1"/>')
                lines.append(f'<line x1="{x1-3:.1f}" y1="{y1:.1f}" x2="{x2-3:.1f}" y2="{y2:.1f}" stroke-width="1"/>')
    lines.append('</g>')

    # ── Room labels ───────────────────────────────────────────────────────
    for room in rooms:
        cx = X(room.x + room.width / 2)
        cy = Y(room.y + room.depth / 2)
        label_size = max(7, min(11, room.width * S / len(room.name) * 1.2))
        sf_size    = max(6, label_size - 2)
        occ        = room.occupant_load
        lines.append(
            f'<text x="{cx:.1f}" y="{cy - 6:.1f}" '
            f'text-anchor="middle" font-size="{label_size:.0f}" '
            f'font-weight="600" fill="{C_LABEL}">'
            f'{room.name}</text>'
        )
        lines.append(
            f'<text x="{cx:.1f}" y="{cy + 8:.1f}" '
            f'text-anchor="middle" font-size="{sf_size:.0f}" '
            f'fill="#555">{room.sqft:.0f} SF'
            f'{f" / {occ} OCC" if occ > 0 else ""}</text>'
        )

    # ── Dimension strings ─────────────────────────────────────────────────
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
            f'stroke="{C_DIM}" stroke-width="1"/>',
            f'<line x1="{x1:.0f}" y1="{y1-8:.0f}" x2="{x1:.0f}" y2="{y1+8:.0f}" '
            f'stroke="{C_DIM}" stroke-width="1"/>',
            f'<line x1="{x2:.0f}" y1="{y2-8:.0f}" x2="{x2:.0f}" y2="{y2+8:.0f}" '
            f'stroke="{C_DIM}" stroke-width="1"/>',
        ]
        rot = "" if horiz else f' transform="rotate(-90,{mx:.0f},{my:.0f})"'
        ls.append(
            f'<text x="{mx:.0f}" y="{my - 4:.0f}" text-anchor="middle" '
            f'font-size="10" fill="{C_DIM}"{rot}>{label}</text>'
        )
        return ls

    # Overall width
    lines += _dim_line(start_x, dim_y, end_x, dim_y,
                       f"{building_w - 2*margin:.0f}'-0\"")
    # Overall depth (vertical)
    lines += _dim_line(start_y, dim_x, end_y, dim_x,
                       f"{building_d - 2*margin:.0f}'-0\"", horiz=False)

    # Per-column widths (top of drawing)
    top_dim_y = Y(margin) - DIM_SPACE * S * 0.4
    rooms_in_row = [r for r in rooms if abs(r.y - rooms[0].y) < 2 and rooms]
    if len(rooms_in_row) > 1:
        for r in rooms_in_row:
            x1 = X(r.x); x2 = X(r.x + r.width)
            lines += _dim_line(x1, top_dim_y, x2, top_dim_y,
                               f"{r.width:.0f}'")

    # ── North arrow ───────────────────────────────────────────────────────
    na_x = X(building_w) + DIM_SPACE * S * 0.4
    na_y = Y(margin) + 30
    r_arr = 18
    lines += [
        f'<circle cx="{na_x:.0f}" cy="{na_y:.0f}" r="{r_arr}" '
        f'fill="none" stroke="{C_DIM}" stroke-width="1.5"/>',
        f'<polygon points="{na_x:.0f},{na_y-r_arr:.0f} '
        f'{na_x-7:.0f},{na_y+8:.0f} {na_x:.0f},{na_y+4:.0f} '
        f'{na_x+7:.0f},{na_y+8:.0f}" fill="{C_DIM}"/>',
        f'<text x="{na_x:.0f}" y="{na_y - r_arr - 4:.0f}" '
        f'text-anchor="middle" font-size="12" font-weight="bold" fill="{C_DIM}">N</text>',
    ]

    # ── Scale bar ─────────────────────────────────────────────────────────
    sb_x = X(margin)
    sb_y = Y(building_d) + DIM_SPACE * S * 0.35
    unit = 10 * S
    for i in range(5):
        fill_c = C_DIM if i % 2 == 0 else "white"
        lines.append(
            f'<rect x="{sb_x + i*unit:.0f}" y="{sb_y:.0f}" '
            f'width="{unit:.0f}" height="8" '
            f'fill="{fill_c}" stroke="{C_DIM}" stroke-width="1"/>'
        )
    lines += [
        f'<text x="{sb_x:.0f}" y="{sb_y + 18:.0f}" font-size="9" fill="{C_DIM}">0</text>',
        f'<text x="{sb_x + 5*unit:.0f}" y="{sb_y + 18:.0f}" font-size="9" fill="{C_DIM}" text-anchor="end">50\'</text>',
        f'<text x="{sb_x + 2.5*unit:.0f}" y="{sb_y + 18:.0f}" font-size="9" fill="{C_DIM}" text-anchor="middle">SCALE: 1/8" = 1\'-0"</text>',
    ]

    # ── Title block ───────────────────────────────────────────────────────
    tb_y  = Y(building_d) + DIM_SPACE * S * 0.7
    tb_h  = TITLE_H_FT * S
    tb_x  = X(margin)
    tb_w  = (building_w - 2*margin) * S

    lines += [
        f'<rect x="{tb_x:.0f}" y="{tb_y:.0f}" width="{tb_w:.0f}" height="{tb_h:.0f}" '
        f'fill="{C_TITLE_BG}" rx="4"/>',
        f'<text x="{tb_x + 12:.0f}" y="{tb_y + 22:.0f}" font-size="14" '
        f'font-weight="bold" fill="white">{project_name}</text>',
        f'<text x="{tb_x + 12:.0f}" y="{tb_y + 36:.0f}" font-size="9" fill="#aac4e0">'
        f'FLOOR PLAN – LEVEL 1   |   {building_type.upper()}   |   {primary_code}</text>',
        f'<text x="{tb_x + tb_w - 12:.0f}" y="{tb_y + 22:.0f}" font-size="11" '
        f'font-weight="bold" fill="#4a9eff" text-anchor="end">'
        f'A1.0</text>',
        f'<text x="{tb_x + tb_w - 12:.0f}" y="{tb_y + 36:.0f}" font-size="9" '
        f'fill="#aac4e0" text-anchor="end">'
        f'{sum(r.sqft for r in rooms):.0f} SF TOTAL</text>',
    ]
    if jurisdiction:
        lines.append(
            f'<text x="{tb_x + 12:.0f}" y="{tb_y + 48:.0f}" font-size="9" '
            f'fill="#aac4e0">{jurisdiction}</text>'
        )

    lines.append('</svg>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 4 – DXF Export
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

    # Room labels
    for room in rooms:
        cx = f(room.x + room.width/2)
        cy = f(room.y + room.depth/2)
        msp.add_text(room.name, dxfattribs={"layer":"A-ANNO-TEXT","height":f(0.8)}
        ).set_placement((cx, cy+f(0.5)), align=TextEntityAlignment.CENTER)
        msp.add_text(f"{room.sqft:.0f} SF", dxfattribs={"layer":"A-ANNO-TEXT","height":f(0.6)}
        ).set_placement((cx, cy-f(0.3)), align=TextEntityAlignment.CENTER)

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
    msp.add_text("FLOOR PLAN – LEVEL 1",
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

    Args:
        description:   Natural language description of the floor plan
        project_name:  Project name for title block
        building_type: Commercial or Residential
        primary_code:  IBC 2023, CBC 2022, etc.
        jurisdiction:  City, State for title block
        api_key:       NVIDIA NIM API key (uses LLM parser if provided)

    Returns:
        FloorPlan with svg_data, dxf_data, rooms, total_sqft
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

    # Stage 2: Layout
    logger.info("Stage 2: Layout engine")
    rooms, bw, bd = layout_rooms(program)
    fp.rooms      = rooms
    fp.building_w = bw
    fp.building_d = bd
    fp.total_sqft = sum(r.sqft for r in rooms)
    fp.occupant_load = sum(r.occupant_load for r in rooms)

    if not rooms:
        fp.warnings.append("No rooms could be placed — check your description.")
        return fp

    # Stage 3: SVG
    logger.info("Stage 3: Generating SVG (%d rooms, %.0f x %.0f ft)", len(rooms), bw, bd)
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

    logger.info("Floor plan complete: %.0f SF, %d rooms, %d occupants",
                fp.total_sqft, len(rooms), fp.occupant_load)
    return fp
