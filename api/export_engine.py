"""
export_engine.py  –  Professional Architectural Drawing Set Export
==================================================================
Produces real drawn architectural sheets that look like permit drawings.

Sheet types
-----------
  Cover     : project data, code summary, compliance stamp
  A1.0      : Floor Plan – dimensioned, walls filled, doors, windows, notes
  A3.0      : Exterior Elevations – all 4 elevations schematic
  C2.0      : Site Plan – building footprint on site grid
  S4.0      : Structural – column grid + framing notes
  FP5.0     : Fire & Life Safety – egress, exit signs, extinguisher locations

Each sheet
----------
  • ANSI D sheet border   (34" × 22"  /  2448 × 1584 pt)
  • AIA title block at bottom-right
  • Drawing border, binding margin, revision block
  • Scale bar + north arrow on plan sheets
  • All line weights follow AIA CAD standards

DXF sheets use AIA layer structure with proper linetypes and weights.
"""

from __future__ import annotations

import io
import math
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# fitz (PyMuPDF) loaded on demand only if needed
# reportlab loaded lazily inside generate() to avoid cold-start crashes on Vercel
try:
    from reportlab.lib.colors import Color, HexColor, black, white
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as rl_canvas
    _RL_OK = True
except Exception as _rl_err:
    _RL_OK = False
    _rl_err_msg = str(_rl_err)

# ── Sheet constants (ANSI D  34" × 22") ─────────────────────────────────
PW, PH      = 34 * 72, 22 * 72      # 2448 × 1584 pts
BDR_L       = 1.50 * inch            # left border  (binding)
BDR_R       = 0.50 * inch
BDR_T       = 0.50 * inch
TB_H        = 1.50 * inch            # title block height
DRAW_X0     = BDR_L                  # drawing area
DRAW_Y0     = TB_H
DRAW_X1     = PW - BDR_R
DRAW_Y1     = PH - BDR_T
DRAW_W      = DRAW_X1 - DRAW_X0
DRAW_H      = DRAW_Y1 - DRAW_Y0

# ── Colours ──────────────────────────────────────────────────────────────
C_BLK   = black
C_WHT   = white
C_WALL  = HexColor('#111111')        # exterior wall fill
C_WINT  = HexColor('#333333')        # interior wall
C_ROOM  = HexColor('#f7f4ee')        # room fill
C_CORE  = HexColor('#ebe6d8')        # restroom/mech fill
C_CIRC  = HexColor('#f0ece0')        # corridor fill
C_DIM   = HexColor('#154360')        # dimension colour
C_NOTE  = HexColor('#1a1a1a')
C_GRID  = HexColor('#c8cdd8')
C_WIN   = HexColor('#4a90d9')        # window glazing
C_DOOR  = HexColor('#1a1a1a')
C_EXIT  = HexColor('#c0392b')
C_STAIR = HexColor('#5d6d7e')
C_TBKG  = HexColor('#1a3a5c')        # title block dark blue
C_TACC  = HexColor('#4a9eff')
C_TSUB  = HexColor('#8ab4d4')
C_GRN   = HexColor('#1e8449')
C_RED   = HexColor('#c0392b')

# ── Architectural wall thicknesses (feet) ────────────────────────────────
EXT_WALL_T  = 0.67      # 8"  exterior
INT_WALL_T  = 0.50      # 6"  interior bearing
PART_WALL_T = 0.33      # 4"  partition

def _now(): return datetime.now().strftime('%m/%d/%Y')
def _yr():  return datetime.now().strftime('%Y')


# ─────────────────────────────────────────────────────────────────────────
#  Room layout engine  (also used by floorplan_generator but self-contained)
# ─────────────────────────────────────────────────────────────────────────

def _canonical_rooms(job: Dict) -> List[Dict]:
    """Return a list of rooms with name/zone/width_ft/depth_ft/sqft."""
    rooms = job.get('rooms', [])
    if rooms:
        return rooms

    sqft = float(job.get('gross_sq_ft') or 10000)
    stories = int(job.get('num_stories') or 1)
    floor_sqft = sqft / stories

    bt = job.get('building_type', 'Commercial')
    if bt == 'Residential':
        program = [
            ('Master Bedroom',  'perimeter', 14, 16),
            ('Bedroom 2',       'perimeter', 12, 13),
            ('Bedroom 3',       'perimeter', 11, 12),
            ('Living Room',     'circulation', 20, 18),
            ('Kitchen',         'perimeter', 14, 15),
            ('Dining Room',     'perimeter', 12, 14),
            ('Master Bath',     'core', 9,  11),
            ('Bath',            'core', 7,  9),
            ('Garage',          'perimeter', 20, 22),
            ('Laundry',         'core', 7,  8),
        ]
    else:
        program = [
            ('Open Office',     'perimeter', 40, 30),
            ('Private Office',  'perimeter', 12, 14),
            ('Private Office',  'perimeter', 12, 14),
            ('Conference Room', 'perimeter', 20, 22),
            ('Break Room',      'perimeter', 14, 16),
            ('Reception',       'circulation', 20, 18),
            ('Lobby',           'circulation', 24, 20),
            ('Restroom – M',    'core', 10, 13),
            ('Restroom – W',    'core', 10, 13),
            ('Mechanical',      'core', 10, 12),
            ('Storage',         'core', 10, 12),
            ('Corridor',        'circulation', 6,  50),
        ]

    # Scale to target sqft
    total = sum(w*d for _,_,w,d in program)
    scale = math.sqrt(floor_sqft / max(total, 1))
    rooms = []
    for name, zone, w, d in program:
        sw = max(8.0, w * scale)
        sd = max(8.0, d * scale)
        rooms.append({'name': name, 'zone': zone,
                      'width_ft': round(sw,1), 'depth_ft': round(sd,1),
                      'sqft': round(sw * sd)})
    return rooms


def _layout(rooms: List[Dict]) -> Tuple[List[Dict], float, float]:
    """
    Place rooms into a strip-zone layout.
    Returns (placed_rooms, building_w_ft, building_d_ft).
    Each room gets _x, _y, _w, _d keys (feet from building origin).
    """
    GAP   = 0.4
    MAR   = 1.5
    COR_W = 5.0

    circ  = [r for r in rooms if r.get('zone') == 'circulation']
    core  = [r for r in rooms if r.get('zone') == 'core']
    peri  = [r for r in rooms if r.get('zone') == 'perimeter']

    # Estimate building width from widest strip
    bw_raw = max(
        sum(r.get('width_ft',12) for r in circ) + max(len(circ)-1,0)*GAP if circ else 0,
        sum(r.get('width_ft',12) for r in core) + max(len(core)-1,0)*GAP if core else 0,
        sum(r.get('width_ft',12) for r in peri[:4]) + 3*GAP if peri else 0,
    )
    bw = max(bw_raw, 40.0) + 2 * MAR
    bw = math.ceil(bw / 5) * 5

    placed = []
    cy = MAR  # cursor y

    # Lobby / circulation strip
    if circ:
        cx = MAR
        row_d = max(r.get('depth_ft',18) for r in circ)
        avail = bw - 2*MAR
        col_w = avail / len(circ) - GAP
        for r in circ:
            r2 = dict(r)
            r2['_x'] = cx; r2['_y'] = cy
            r2['_w'] = max(12.0, col_w)
            r2['_d'] = r2.get('depth_ft', row_d)
            placed.append(r2); cx += r2['_w'] + GAP
        cy += row_d + GAP

    # Corridor
    has_cor = any('corridor' in r.get('name','').lower() for r in placed)
    if not has_cor and peri:
        cor = {'name': 'Corridor', 'zone': 'circulation',
               '_x': MAR, '_y': cy, '_w': bw - 2*MAR, '_d': COR_W,
               'sqft': int((bw-2*MAR) * COR_W)}
        placed.append(cor); cy += COR_W + GAP
    else:
        # Widen existing corridor
        for r in placed:
            if 'corridor' in r.get('name','').lower():
                r['_w'] = bw - 2*MAR; r['_d'] = COR_W

    # Perimeter rooms in rows of 4
    ROW = 4
    for i in range(0, len(peri), ROW):
        row = peri[i:i+ROW]
        row_d = max(r.get('depth_ft',14) for r in row)
        avail = bw - 2*MAR
        col_w = avail / len(row) - GAP
        cx = MAR
        for r in row:
            r2 = dict(r)
            r2['_x'] = cx; r2['_y'] = cy
            r2['_w'] = max(8.0, col_w); r2['_d'] = row_d
            placed.append(r2); cx += r2['_w'] + GAP
        cy += row_d + GAP

    # Core strip
    if core:
        avail = bw - 2*MAR
        col_w = avail / len(core) - GAP
        row_d = max(r.get('depth_ft',12) for r in core)
        cx = MAR
        for r in core:
            r2 = dict(r)
            r2['_x'] = cx; r2['_y'] = cy
            r2['_w'] = max(8.0, col_w); r2['_d'] = row_d
            placed.append(r2); cx += r2['_w'] + GAP
        cy += row_d + GAP

    bd = cy + MAR
    bd = math.ceil(bd / 5) * 5
    return placed, bw, bd


# ─────────────────────────────────────────────────────────────────────────
#  Drawing primitives
# ─────────────────────────────────────────────────────────────────────────

def _title_block(c: rl_canvas.Canvas, job: Dict, sheet_num: str,
                 sheet_title: str, scale_str: str, total: int):
    """Premium architectural title block - world-class firm aesthetic."""
    # Deep navy background
    c.setFillColor(HexColor('#0d1b2a'))
    c.rect(0, 0, PW, TB_H, fill=1, stroke=0)

    # Gold accent stripe at top
    c.setFillColor(HexColor('#c9a227'))
    c.rect(0, TB_H - 5, PW, 5, fill=1, stroke=0)

    # Left firm branding area
    c.setFillColor(HexColor('#e8e8e8'))
    c.setFont('Helvetica-Bold', 14)
    c.drawString(BDR_L + 10, TB_H - 28, 'ARCHITECTURAL AI PLATFORM')
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 7)
    c.drawString(BDR_L + 10, TB_H - 40, 'INTELLIGENT DESIGN SOLUTIONS')

    # Divider
    c.setStrokeColor(HexColor('#2d3e50'))
    c.setLineWidth(0.75)
    c.line(BDR_L + 10, TB_H - 48, BDR_L + 260, TB_H - 48)

    # Project name
    pname = job.get('project_name', 'Project')
    c.setFillColor(HexColor('#ffffff'))
    c.setFont('Helvetica-Bold', 13)
    c.drawString(BDR_L + 10, TB_H - 68, pname[:42])

    # Jurisdiction & code
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 7.5)
    jur = job.get('jurisdiction_preset', job.get('jurisdiction',''))
    if jur:
        c.drawString(BDR_L + 10, TB_H - 82, jur)
    code_info = f"{job.get('primary_code','IBC 2023')}  |  {job.get('building_type','Commercial')}"
    c.drawString(BDR_L + 10, TB_H - 94, code_info)

    # Vertical column dividers
    col_x = [BDR_L + 290, BDR_L + 500, BDR_L + 700, BDR_L + 880, PW - 1.8*inch]
    c.setStrokeColor(HexColor('#2d3e50'))
    c.setLineWidth(0.5)
    for x in col_x:
        c.line(x, 5, x, TB_H - 5)

    # ── Column 2: Drawing Info ──
    x0 = col_x[0] + 10
    c.setFillColor(HexColor('#c9a227'))
    c.setFont('Helvetica', 6.5)
    c.drawString(x0, TB_H - 20, 'DRAWING')
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 7)
    c.drawString(x0, TB_H - 30, scale_str if scale_str else 'N/A')
    
    # Sheet title (wrap if long)
    c.setFillColor(HexColor('#ffffff'))
    c.setFont('Helvetica-Bold', 11)
    words = sheet_title.split()
    lines = []
    current = ""
    for w in words:
        test = (current + " " + w).strip()
        if c.stringWidth(test, 'Helvetica-Bold', 11) < 170:
            current = test
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    y = TB_H - 52
    for ln in lines[:2]:
        c.drawString(x0, y, ln)
        y -= 14

    # Date
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 7.5)
    c.drawString(x0, TB_H - 95, _now())

    # ── Column 3: Building Data ──
    x0 = col_x[1] + 10
    c.setFillColor(HexColor('#c9a227'))
    c.setFont('Helvetica', 6.5)
    c.drawString(x0, TB_H - 20, 'BUILDING DATA')

    r = job.get('compliance_report', {})
    data = [
        ('OCCUPANCY', job.get('occupancy_group', 'B')),
        ('TYPE', (job.get('construction_type', '') or 'VB')[:14]),
        ('SPRINKLER', 'Sprinklered' if job.get('sprinklered') else 'Unprotected'),
        ('STORIES', str(job.get('num_stories', 1))),
    ]
    y = TB_H - 40
    for label, val in data:
        c.setFillColor(HexColor('#8b9dc3'))
        c.setFont('Helvetica', 6)
        c.drawString(x0, y, label)
        c.setFillColor(HexColor('#ffffff'))
        c.setFont('Helvetica', 7.5)
        c.drawString(x0, y - 10, val[:16])
        y -= 24

    # ── Column 4: Compliance Status ──
    x0 = col_x[2] + 10
    compliant = r.get('is_compliant', True)
    
    # Status badge
    badge_color = C_GRN if compliant else C_RED
    c.setFillColor(badge_color)
    c.roundRect(x0, TB_H - 78, 130, 26, 3, fill=1, stroke=0)
    c.setFillColor(HexColor('#ffffff'))
    c.setFont('Helvetica-Bold', 9)
    status = 'IBC COMPLIANT' if compliant else 'NON-COMPLIANT'
    c.drawCentredString(x0 + 65, TB_H - 62, status)

    # Counts
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 7)
    c.drawString(x0, TB_H - 94, f"Issues: {r.get('blocking_count', 0)}")
    c.drawString(x0, TB_H - 105, f"Warnings: {r.get('warning_count', 0)}")
    c.setFillColor(HexColor('#5d6d7e'))
    c.setFont('Helvetica', 6)
    c.drawString(x0, TB_H - 118, job.get('primary_code', 'IBC 2023'))

    # ── Column 5: Sheet Number (right panel) ──
    x0 = col_x[3] + 8
    pw = PW - x0 - 8
    c.setFillColor(HexColor('#1b2838'))
    c.roundRect(x0, 6, pw, TB_H - 12, 5, fill=1, stroke=0)
    
    c.setFillColor(HexColor('#c9a227'))
    c.setFont('Helvetica', 7.5)
    c.drawCentredString(x0 + pw/2, TB_H - 24, 'SHEET')
    
    c.setFillColor(HexColor('#ffffff'))
    c.setFont('Helvetica-Bold', 34)
    c.drawCentredString(x0 + pw/2, TB_H - 64, sheet_num)
    
    c.setFillColor(HexColor('#8b9dc3'))
    c.setFont('Helvetica', 9)
    c.drawCentredString(x0 + pw/2, TB_H - 84, f"of {total}")
    
    c.setFont('Helvetica', 6.5)
    c.drawCentredString(x0 + pw/2, 18, f"Rev.01  {_now()}")

    # Bottom gold accent
    c.setStrokeColor(HexColor('#c9a227'))
    c.setLineWidth(1.5)
    c.line(0, 0, PW, 0)


def _border(c: rl_canvas.Canvas):
    """Sheet border and binding strip."""
    c.setStrokeColor(C_BLK); c.setLineWidth(2.5)
    c.rect(BDR_L, TB_H, DRAW_W, DRAW_H, fill=0, stroke=1)
    c.setLineWidth(0.5)
    c.rect(0.25*inch, 0.25*inch, PW - 0.5*inch, PH - 0.5*inch, fill=0, stroke=1)
    # Binding
    c.setLineWidth(4.0)
    c.line(BDR_L, 0, BDR_L, PH)


def _north_arrow(c, cx, cy, r=20):
    c.setFillColor(C_DIM); c.setStrokeColor(C_DIM); c.setLineWidth(1.0)
    c.circle(cx, cy, r, fill=0, stroke=1)
    p = c.beginPath()
    p.moveTo(cx, cy+r); p.lineTo(cx-7, cy-10)
    p.lineTo(cx, cy-5); p.lineTo(cx+7, cy-10); p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.setFont('Helvetica-Bold', 12); c.setFillColor(C_DIM)
    c.drawCentredString(cx, cy + r + 7, 'N')


def _scale_bar(c, x, y, scale):
    """Draw 0-50 ft scale bar. scale = pts per foot."""
    u = 10 * scale
    c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
    for i in range(5):
        c.setFillColor(C_DIM if i % 2 == 0 else C_WHT)
        c.rect(x + i*u, y, u, 7, fill=1, stroke=1)
    c.setFont('Helvetica', 7.5); c.setFillColor(C_DIM)
    c.drawString(x - 3, y - 11, "0")
    c.drawCentredString(x + 2.5*u, y - 11, "25'")
    c.drawRightString(x + 5*u + 3, y - 11, "50'")
    c.setFont('Helvetica-Oblique', 7.5)
    c.drawCentredString(x + 2.5*u, y - 21, "SCALE: 1/8\" = 1'-0\"")


def _dim_line(c, x1, y1, x2, y2, label, offset=18, horiz=True):
    """Draw a dimension string with tick marks."""
    c.setStrokeColor(C_DIM); c.setFillColor(C_DIM); c.setLineWidth(0.6)
    if horiz:
        c.line(x1, y1-offset, x2, y1-offset)
        c.line(x1, y1, x1, y1-offset-4)
        c.line(x2, y1, x2, y1-offset-4)
        mx = (x1+x2)/2
        c.setFont('Helvetica', 7.5)
        c.drawCentredString(mx, y1-offset-12, label)
    else:
        c.line(x1-offset, y1, x1-offset, y2)
        c.line(x1, y1, x1-offset-4, y1)
        c.line(x2, y2, x2-offset-4, y2)   # note: x2 unused in vert
        my = (y1+y2)/2
        c.saveState(); c.translate(x1-offset-14, my)
        c.rotate(90); c.setFont('Helvetica', 7.5)
        c.drawCentredString(0, 0, label); c.restoreState()


def _room_label(c, cx, cy, name, sqft, num=None):
    """Draw room label with number circle, name, area."""
    # Circle number
    if num is not None:
        c.setFillColor(C_WHT); c.setStrokeColor(C_NOTE); c.setLineWidth(0.7)
        c.circle(cx, cy+10, 8, fill=1, stroke=1)
        c.setFillColor(C_NOTE); c.setFont('Helvetica-Bold', 7)
        c.drawCentredString(cx, cy+7, str(num))
    # Name
    c.setFillColor(C_NOTE)
    sz = max(6.5, min(9.5, 160 / max(len(name),1)))
    c.setFont('Helvetica-Bold', sz)
    c.drawCentredString(cx, cy - 2, name)
    # Area
    c.setFont('Helvetica', 7); c.setFillColor(HexColor('#555555'))
    c.drawCentredString(cx, cy - 13, f'{sqft:.0f} SF')


def _draw_wall_rect(c, x, y, w, h, filled=True):
    """Draw a filled wall rectangle."""
    c.setFillColor(C_WALL if filled else C_WINT)
    c.setStrokeColor(C_BLK); c.setLineWidth(0.3)
    c.rect(x, y, w, h, fill=1, stroke=1)


# ─────────────────────────────────────────────────────────────────────────
#  Sheet A1.0 – Floor Plan
# ─────────────────────────────────────────────────────────────────────────

def _sheet_floor_plan(c, job, rooms, placed, bw, bd, scale,
                      sheet_num, total, level=1):
    """Draw a complete dimensioned floor plan sheet."""

    def X(ft): return DRAW_X0 + ox + ft * scale
    def Y(ft): return DRAW_Y0 + oy + ft * scale

    # Centre drawing in available space
    ox = (DRAW_W - bw * scale) / 2
    oy = (DRAW_H - bd * scale) / 2
    EW = EXT_WALL_T * scale   # exterior wall pts
    IW = INT_WALL_T * scale

    # ── Grid ──────────────────────────────────────────────────────────
    c.setStrokeColor(C_GRID); c.setLineWidth(0.25)
    for gx in range(0, int(bw)+1, 10):
        c.line(X(gx), DRAW_Y0, X(gx), DRAW_Y1)
    for gy in range(0, int(bd)+1, 10):
        c.line(DRAW_X0, Y(gy), DRAW_X1, Y(gy))

    # ── Room fills ────────────────────────────────────────────────────
    for r in placed:
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        zone = r.get('zone','perimeter')
        fill = C_CORE if zone=='core' else C_CIRC if zone=='circulation' else C_ROOM
        c.setFillColor(fill); c.setStrokeColor(C_GRID); c.setLineWidth(0.2)
        c.rect(X(rx), Y(ry), rw*scale, rd*scale, fill=1, stroke=1)

    # ── Interior walls (draw as filled strips between rooms) ──────────
    drawn_walls = set()
    def wall_key(a, b): return (round(min(a,b),1), round(max(a,b),1))

    for r in placed:
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        # Four walls
        for (ax,ay,bx,by) in [(rx,ry,rx+rw,ry),(rx+rw,ry,rx+rw,ry+rd),
                               (rx,ry+rd,rx+rw,ry+rd),(rx,ry,rx,ry+rd)]:
            k = wall_key(ax*1000+ay, bx*1000+by)
            if k in drawn_walls: continue
            drawn_walls.add(k)
            is_horiz = abs(ay-by) < 0.1
            if is_horiz:
                c.setFillColor(C_WINT); c.setStrokeColor(C_BLK); c.setLineWidth(0.2)
                c.rect(X(min(ax,bx)), Y(ay)-IW/2, abs(bx-ax)*scale, IW, fill=1, stroke=0)
            else:
                c.setFillColor(C_WINT); c.setStrokeColor(C_BLK); c.setLineWidth(0.2)
                c.rect(X(ax)-IW/2, Y(min(ay,by)), IW, abs(by-ay)*scale, fill=1, stroke=0)

    # ── Exterior walls (solid filled) ─────────────────────────────────
    MAR = 1.5
    # Bottom
    c.setFillColor(C_WALL)
    c.rect(X(MAR-EXT_WALL_T), Y(MAR-EXT_WALL_T),
           (bw-2*(MAR-EXT_WALL_T))*scale, EW, fill=1, stroke=0)
    # Top
    c.rect(X(MAR-EXT_WALL_T), Y(bd-MAR),
           (bw-2*(MAR-EXT_WALL_T))*scale, EW, fill=1, stroke=0)
    # Left
    c.rect(X(MAR-EXT_WALL_T), Y(MAR-EXT_WALL_T),
           EW, (bd-2*(MAR-EXT_WALL_T))*scale, fill=1, stroke=0)
    # Right
    c.rect(X(bw-MAR), Y(MAR-EXT_WALL_T),
           EW, (bd-2*(MAR-EXT_WALL_T))*scale, fill=1, stroke=0)
    # Outline
    c.setStrokeColor(C_WALL); c.setLineWidth(2.5); c.setFillColor(HexColor('#00000000'))
    c.rect(X(MAR), Y(MAR), (bw-2*MAR)*scale, (bd-2*MAR)*scale, fill=0, stroke=1)

    # ── Doors ─────────────────────────────────────────────────────────
    DOOR_FT = 3.0
    for r in placed:
        if 'corridor' in r.get('name','').lower(): continue
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        # Door centred in bottom wall
        door_x_ft = rx + rw/2 - DOOR_FT/2
        door_y_ft = ry + rd
        dw = DOOR_FT * scale
        # White gap in wall
        c.setFillColor(C_ROOM); c.setStrokeColor(C_ROOM); c.setLineWidth(0)
        c.rect(X(door_x_ft), Y(door_y_ft)-IW, dw, IW*2+1, fill=1, stroke=0)
        # Door panel line
        c.setStrokeColor(C_DOOR); c.setLineWidth(1.2)
        c.line(X(door_x_ft), Y(door_y_ft), X(door_x_ft+DOOR_FT), Y(door_y_ft))
        # Swing arc (quarter circle)
        c.setLineWidth(0.6)
        c.arc(X(door_x_ft), Y(door_y_ft)-dw,
              X(door_x_ft)+dw, Y(door_y_ft), 0, 90)

    # ── Windows ───────────────────────────────────────────────────────
    WIN_FT = 4.0
    for r in placed:
        if r.get('zone') == 'core': continue
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        on_front = ry <= MAR + 0.5
        on_back  = ry + rd >= bd - MAR - 0.5
        if not (on_front or on_back): continue
        wy_ft = ry if on_front else ry + rd
        for offset_pct in [0.2, 0.6]:
            wx_ft = rx + rw * offset_pct
            if wx_ft + WIN_FT > rx + rw - 0.5: continue
            wx1 = X(wx_ft); wx2 = X(wx_ft + WIN_FT)
            wy  = Y(wy_ft)
            # White out wall behind window
            c.setFillColor(HexColor('#e8f4fc'))
            c.rect(wx1, wy - EW*0.4, wx2-wx1, EW*0.8, fill=1, stroke=0)
            # Window symbol (3 lines)
            c.setStrokeColor(C_WIN); c.setLineWidth(1.2)
            c.line(wx1, wy, wx2, wy)
            c.setLineWidth(0.5)
            c.line(wx1, wy-3, wx2, wy-3)
            c.line(wx1, wy+3, wx2, wy+3)

    # ── Room labels ───────────────────────────────────────────────────
    for i, r in enumerate(placed):
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        cx = X(rx + rw/2); cy = Y(ry + rd/2)
        sqft = r.get('sqft', round(rw*rd))
        _room_label(c, cx, cy, r.get('name','Room'), sqft, i+100)

    # ── Overall dimensions ────────────────────────────────────────────
    usable_w = bw - 2*MAR; usable_d = bd - 2*MAR
    # Width dimension
    _dim_line(c, X(MAR), Y(0), X(bw-MAR), Y(0),
              f"{int(usable_w)}'-0\"", offset=28, horiz=True)
    # Depth dimension (vertical)
    _dim_line(c, X(0), Y(MAR), X(0), Y(bd-MAR),
              f"{int(usable_d)}'-0\"", offset=30, horiz=False)

    # Bay dimensions (column spacing)
    bays = sorted(set(round(r.get('_x',0),1) for r in placed if r.get('_x',0) > MAR))
    prev_x = X(MAR)
    for bay_x_ft in bays[:6]:
        bx = X(bay_x_ft)
        rroom = [r for r in placed if abs(r.get('_x',0)-bay_x_ft)<0.5]
        if rroom:
            rw = rroom[0].get('_w', 12)
            ex = X(bay_x_ft + rw)
            label = f"{int(rroom[0].get('_w',12))}'"
            _dim_line(c, bx, Y(bd), ex, Y(bd), label, offset=18, horiz=True)

    # ── Grid bubbles (column lines) ────────────────────────────────────
    col_xs = [MAR + i * (usable_w/4) for i in range(5)]
    for j, cxft in enumerate(col_xs):
        cx2 = X(cxft); cy2 = Y(bd + 1.5)
        c.setFillColor(C_WHT); c.setStrokeColor(C_DIM); c.setLineWidth(0.7)
        c.circle(cx2, cy2, 9, fill=1, stroke=1)
        c.setFillColor(C_DIM); c.setFont('Helvetica-Bold', 7.5)
        c.drawCentredString(cx2, cy2-3, str(j+1))
        c.setStrokeColor(C_GRID); c.setLineWidth(0.5)
        c.setDash([4,4])
        c.line(cx2, cy2-9, cx2, Y(0))
        c.setDash([])

    row_ys = [MAR + i * (usable_d/3) for i in range(4)]
    for j, ryft in enumerate(row_ys):
        cy2 = Y(ryft); cx2 = X(MAR - 2.2)
        c.setFillColor(C_WHT); c.setStrokeColor(C_DIM); c.setLineWidth(0.7)
        c.circle(cx2, cy2, 9, fill=1, stroke=1)
        c.setFillColor(C_DIM); c.setFont('Helvetica-Bold', 7.5)
        c.drawCentredString(cx2, cy2-3, chr(65+j))
        c.setStrokeColor(C_GRID); c.setLineWidth(0.5)
        c.setDash([4,4])
        c.line(cx2+9, cy2, X(bw), cy2)
        c.setDash([])

    # ── Notes column (right side) ─────────────────────────────────────
    NX = DRAW_X1 - 1.8*inch + 5
    NY = DRAW_Y1 - 10
    c.setFillColor(C_NOTE); c.setFont('Helvetica-Bold', 7.5)
    c.drawString(NX, NY, 'GENERAL NOTES')
    c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
    c.line(NX, NY-3, NX + 1.7*inch, NY-3)
    notes = [
        '1. All dimensions are to face of stud / face of',
        '   concrete unless otherwise noted.',
        '2. Verify all dimensions in field prior to',
        '   construction. Notify architect of discrepancies.',
        '3. All exterior walls to be 8" thick CMU or',
        '   per structural drawings.',
        '4. All interior walls to be 6" metal stud at',
        '   16" O.C. unless otherwise noted.',
        '5. All doors to be provided with ADA-compliant',
        '   hardware. Min 32" clear opening width.',
        '6. Provide accessibility features per',
        '   IBC Chapter 11 and ICC A117.1.',
        '7. All work to comply with applicable codes.',
    ]
    c.setFont('Helvetica', 6.5); c.setFillColor(HexColor('#333333'))
    for i, note in enumerate(notes):
        c.drawString(NX, NY - 16 - i*10, note)

    # Code data box
    by = NY - 160
    c.setFillColor(HexColor('#eef2f8'))
    c.rect(NX-3, by-90, 1.75*inch, 100, fill=1, stroke=0)
    c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
    c.rect(NX-3, by-90, 1.75*inch, 100, fill=0, stroke=1)
    c.setFont('Helvetica-Bold', 7.5); c.setFillColor(C_DIM)
    c.drawString(NX, by, 'CODE DATA')
    c.setFont('Helvetica', 7); c.setFillColor(C_NOTE)
    r = job.get('compliance_report', {})
    cdata = [
        ('Occ. Group', job.get('occupancy_group','B')),
        ('Construction', (job.get('construction_type','') or '')[:12]),
        ('Primary Code', job.get('primary_code','IBC 2023')),
        ('Sprinklers', 'Yes' if job.get('sprinklered') else 'No'),
        ('Seismic SDC', job.get('jurisdiction_details',{}).get('seismic_design_category','B')),
    ]
    for i, (k, v) in enumerate(cdata):
        c.drawString(NX, by-18-i*13, k+':  '+str(v))

    # ── North arrow + scale bar ────────────────────────────────────────
    _north_arrow(c, DRAW_X1 - 2.4*inch, DRAW_Y0 + 60)
    _scale_bar(c, DRAW_X1 - 3.5*inch, DRAW_Y0 + 22, scale)

    # Level tag
    c.setFillColor(C_TBKG); c.setStrokeColor(C_DIM); c.setLineWidth(1)
    c.roundRect(DRAW_X0+10, DRAW_Y1-30, 80, 22, 4, fill=1, stroke=1)
    c.setFillColor(C_WHT); c.setFont('Helvetica-Bold', 9)
    c.drawCentredString(DRAW_X0+50, DRAW_Y1-22, f'LEVEL {level}')


# ─────────────────────────────────────────────────────────────────────────
#  Sheet A3.0 – Exterior Elevations
# ─────────────────────────────────────────────────────────────────────────

def _sheet_elevations(c, job, bw, bd):
    """Four schematic exterior elevations."""
    floors = int(job.get('num_stories') or 1)
    flr_h  = 12.0   # ft per floor
    bldg_h = floors * flr_h
    roof_h = 3.0

    # Two elevations per row, 2 rows
    layouts = [
        (DRAW_X0 + 0.5*inch, DRAW_Y0 + DRAW_H*0.52, bw, 'SOUTH ELEVATION'),
        (DRAW_X0 + DRAW_W*0.5 + 0.3*inch, DRAW_Y0 + DRAW_H*0.52, bw, 'NORTH ELEVATION'),
        (DRAW_X0 + 0.5*inch, DRAW_Y0 + 0.4*inch, bd, 'EAST ELEVATION'),
        (DRAW_X0 + DRAW_W*0.5 + 0.3*inch, DRAW_Y0 + 0.4*inch, bd, 'WEST ELEVATION'),
    ]
    avail_w = DRAW_W * 0.45
    avail_h = DRAW_H * 0.42

    for (ex, ey, width, label) in layouts:
        scale_e = min(avail_w / (width + 4), avail_h / (bldg_h + roof_h + 4)) * 0.85
        bw_pts = width * scale_e
        bh_pts = bldg_h * scale_e
        rh_pts = roof_h * scale_e

        # Ground line
        c.setStrokeColor(C_WALL); c.setLineWidth(2.5)
        c.line(ex - 10, ey, ex + bw_pts + 10, ey)

        # Building outline
        c.setFillColor(HexColor('#f4f1ea'))
        c.setStrokeColor(C_WALL); c.setLineWidth(2.0)
        c.rect(ex, ey, bw_pts, bh_pts, fill=1, stroke=1)

        # Parapet / roof
        c.setFillColor(HexColor('#d8d2c0'))
        c.rect(ex - 6, ey + bh_pts, bw_pts + 12, rh_pts, fill=1, stroke=1)

        # Floor lines
        c.setStrokeColor(HexColor('#999999')); c.setLineWidth(0.6)
        c.setDash([8,4])
        for f in range(1, floors):
            fy = ey + f * flr_h * scale_e
            c.line(ex, fy, ex + bw_pts, fy)
        c.setDash([])

        # Windows (regular grid)
        wins_per_floor = max(2, int(width / 12))
        win_w = bw_pts / (wins_per_floor + 1) * 0.55
        win_h = flr_h * scale_e * 0.42
        for f in range(floors):
            for w in range(wins_per_floor):
                wx = ex + (w+1) * bw_pts/(wins_per_floor+1) - win_w/2
                wy = ey + f*flr_h*scale_e + flr_h*scale_e*0.35
                c.setFillColor(HexColor('#c8e0f0'))
                c.setStrokeColor(HexColor('#4a90d9')); c.setLineWidth(1.0)
                c.rect(wx, wy, win_w, win_h, fill=1, stroke=1)
                # Mullion
                c.line(wx+win_w/2, wy, wx+win_w/2, wy+win_h)

        # Main entrance (on south only)
        if 'SOUTH' in label:
            dw2 = bw_pts * 0.09; dh2 = flr_h * scale_e * 0.72
            dx2 = ex + bw_pts/2 - dw2/2
            c.setFillColor(HexColor('#8ab4d4'))
            c.setStrokeColor(C_WALL); c.setLineWidth(1.5)
            c.rect(dx2, ey, dw2, dh2, fill=1, stroke=1)
            c.setFillColor(HexColor('#5a84a4'))
            c.rect(dx2 + dw2*0.5, ey, dw2*0.5, dh2, fill=1, stroke=0)

        # Floor height tags
        c.setFillColor(C_DIM); c.setFont('Helvetica', 6.5)
        for f in range(floors+1):
            fy = ey + f * flr_h * scale_e
            ht_label = "T.O. PARAPET" if f==floors else f"LEVEL {f+1}" if f>0 else "T.O. SLAB"
            c.drawString(ex + bw_pts + 6, fy - 3, ht_label)
            elev = f * flr_h
            c.drawString(ex + bw_pts + 6, fy - 12, f"+{elev:.0f}'-0\"")
            c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
            c.line(ex + bw_pts + 3, fy, ex + bw_pts + 5, fy)

        # Elevation label
        c.setFillColor(C_TBKG)
        c.rect(ex, ey - 28, bw_pts, 22, fill=1, stroke=0)
        c.setFillColor(C_WHT); c.setFont('Helvetica-Bold', 9)
        c.drawCentredString(ex + bw_pts/2, ey - 20, label)
        c.setFont('Helvetica', 7.5); c.setFillColor(C_TSUB)
        c.drawCentredString(ex + bw_pts/2, ey - 30, f"Scale: 1/8\" = 1'-0\"")


# ─────────────────────────────────────────────────────────────────────────
#  Sheet C2.0 – Site Plan
# ─────────────────────────────────────────────────────────────────────────

def _sheet_site_plan(c, job, bw, bd):
    """Schematic site plan with property lines, setbacks, parking."""
    SITE_W  = bw * 3.2
    SITE_D  = bd * 3.0
    SETBACK = bw * 0.4
    SETBACK_D = bd * 0.4

    scale_s = min(DRAW_W / (SITE_W + 8),
                  (DRAW_H - 1*inch) / (SITE_D + 8)) * 0.82
    ox = DRAW_X0 + (DRAW_W - SITE_W * scale_s) / 2
    oy = DRAW_Y0 + (DRAW_H - SITE_D * scale_s) / 2

    def SX(ft): return ox + ft * scale_s
    def SY(ft): return oy + ft * scale_s

    # Property background
    c.setFillColor(HexColor('#edf5e8'))
    c.rect(SX(0), SY(0), SITE_W*scale_s, SITE_D*scale_s, fill=1, stroke=0)

    # Property line (dashed)
    c.setStrokeColor(HexColor('#2c5530')); c.setLineWidth(1.5); c.setDash([12,6])
    c.rect(SX(0), SY(0), SITE_W*scale_s, SITE_D*scale_s, fill=0, stroke=1)
    c.setDash([])

    # Street (south)
    c.setFillColor(HexColor('#c8c8c4'))
    c.rect(SX(-SITE_W*0.08), SY(-SITE_W*0.12),
           (SITE_W*1.16)*scale_s, SITE_D*0.10*scale_s, fill=1, stroke=0)
    c.setFillColor(HexColor('#e8e8e4'))
    c.rect(SX(0.08*SITE_W), SY(-0.065*SITE_W),
           (SITE_W*0.84)*scale_s, 2*scale_s, fill=1, stroke=0)
    c.setFillColor(C_NOTE); c.setFont('Helvetica-Bold', 8)
    c.drawCentredString(SX(SITE_W/2), SY(-SITE_W*0.07), 'MAIN STREET (PUBLIC R.O.W.)')

    # Setback lines (dashed red)
    c.setStrokeColor(HexColor('#c0392b')); c.setLineWidth(0.8); c.setDash([8,4])
    c.rect(SX(SETBACK), SY(SETBACK_D),
           (SITE_W-2*SETBACK)*scale_s, (SITE_D-2*SETBACK_D)*scale_s, fill=0, stroke=1)
    c.setDash([])
    c.setFont('Helvetica-Oblique', 6.5); c.setFillColor(HexColor('#c0392b'))
    c.drawString(SX(SETBACK)+3, SY(SETBACK_D)+4, 'SETBACK LINE (TYP.)')

    # Building footprint
    bldg_ox = (SITE_W - bw) / 2
    bldg_oy = (SITE_D - bd) / 2
    c.setFillColor(HexColor('#d8cfa8'))
    c.setStrokeColor(C_WALL); c.setLineWidth(2.2)
    c.rect(SX(bldg_ox), SY(bldg_oy), bw*scale_s, bd*scale_s, fill=1, stroke=1)
    c.setFillColor(C_NOTE); c.setFont('Helvetica-Bold', 9)
    c.drawCentredString(SX(bldg_ox + bw/2), SY(bldg_oy + bd/2),
                        job.get('project_name','PROJECT')[:20])
    c.setFont('Helvetica', 7.5); c.setFillColor(HexColor('#555'))
    c.drawCentredString(SX(bldg_ox + bw/2), SY(bldg_oy + bd/2)-12,
                        f'{int(bw*bd):,} SF FOOTPRINT')

    # Parking (east side)
    park_ox = bldg_ox + bw + 4
    park_spaces = min(20, int((SITE_D - 2*SETBACK_D) / 9))
    stall_w = 9.0; stall_d = 18.0
    pk_cols = 2
    c.setFillColor(HexColor('#ddddd4'))
    c.setStrokeColor(C_WALL); c.setLineWidth(0.6)
    c.rect(SX(park_ox), SY(SETBACK_D),
           (stall_d*pk_cols+3)*scale_s,
           (min(park_spaces,10)*stall_w+2)*scale_s, fill=1, stroke=1)
    for sp in range(min(park_spaces,10)):
        for col in range(pk_cols):
            sx2 = SX(park_ox + col*(stall_d+3))
            sy2 = SY(SETBACK_D + sp*stall_w + 1)
            c.setFillColor(HexColor('#f0f0ea'))
            c.rect(sx2, sy2, stall_d*scale_s, stall_w*scale_s, fill=1, stroke=1)
            c.setFillColor(C_DIM); c.setFont('Helvetica', 5.5)
            c.drawCentredString(sx2 + stall_d*scale_s/2, sy2 + 3, 'P')
    c.setFont('Helvetica-Bold', 7); c.setFillColor(C_NOTE)
    c.drawCentredString(SX(park_ox + stall_d*pk_cols/2 + 1.5),
                        SY(SETBACK_D + min(park_spaces,10)*stall_w/2),
                        f'PARKING\n{park_spaces} SPACES')

    # Drive aisle
    c.setFillColor(HexColor('#c8c4b8'))
    c.rect(SX(park_ox + stall_d*pk_cols + 3), SY(SETBACK_D),
           4*scale_s, (SITE_D - 2*SETBACK_D)*scale_s, fill=1, stroke=0)

    # North arrow + scale
    _north_arrow(c, DRAW_X1 - 1.6*inch, DRAW_Y0 + 70)
    _scale_bar(c, DRAW_X1 - 3.0*inch, DRAW_Y0 + 22,
               min(DRAW_W / (SITE_W+8), DRAW_H / (SITE_D+8)) * 0.82)

    # Legend
    lx = DRAW_X0 + 10; ly = DRAW_Y0 + 10
    items = [
        (HexColor('#d8cfa8'), C_WALL, 'PROPOSED BUILDING'),
        (HexColor('#edf5e8'), HexColor('#2c5530'), 'PROPERTY / LOT LINE'),
        (HexColor('#ddddd4'), C_WALL, 'PAVED PARKING AREA'),
        (HexColor('#c8c8c4'), None, 'EXISTING STREET / R.O.W.'),
    ]
    c.setFont('Helvetica-Bold', 7.5); c.setFillColor(C_NOTE)
    c.drawString(lx, ly + len(items)*14 + 4, 'LEGEND')
    for i, (fill, stroke, text) in enumerate(items):
        c.setFillColor(fill)
        lw = 0.6 if stroke else 0
        c.setStrokeColor(stroke or C_BLK); c.setLineWidth(lw)
        c.rect(lx, ly + i*14, 14, 10, fill=1, stroke=1 if stroke else 0)
        c.setFillColor(C_NOTE); c.setFont('Helvetica', 7)
        c.drawString(lx + 18, ly + i*14 + 2, text)


# ─────────────────────────────────────────────────────────────────────────
#  Sheet S4.0 – Structural
# ─────────────────────────────────────────────────────────────────────────

def _sheet_structural(c, job, bw, bd):
    """Structural framing plan with column grid, beams, notes."""
    scale_s = min((DRAW_W - 3*inch) / (bw + 6),
                  (DRAW_H - 1.5*inch) / (bd + 6)) * 0.85
    ox = DRAW_X0 + (DRAW_W - bw*scale_s) / 2
    oy = DRAW_Y0 + (DRAW_H - bd*scale_s) / 2 + 0.5*inch

    def X(ft): return ox + ft * scale_s
    def Y(ft): return oy + ft * scale_s

    COLS_X = 4; COLS_Y = 3
    bay_w  = bw / COLS_X; bay_d = bd / COLS_Y

    # Slab hatch (light grey)
    c.setFillColor(HexColor('#f0eee8'))
    c.rect(X(0), Y(0), bw*scale_s, bd*scale_s, fill=1, stroke=0)
    c.setStrokeColor(HexColor('#ddd8cc')); c.setLineWidth(0.3)
    for hx in range(0, int(bw*2)+1):
        c.line(X(hx*0.5), Y(0), X(hx*0.5), Y(bd))
    for hy in range(0, int(bd*2)+1):
        c.line(X(0), Y(hy*0.5), X(bw), Y(hy*0.5))

    # Beam lines
    c.setStrokeColor(HexColor('#333333')); c.setLineWidth(3.5)
    for col in range(COLS_X+1):
        c.line(X(col*bay_w), Y(0), X(col*bay_w), Y(bd))
    for row in range(COLS_Y+1):
        c.line(X(0), Y(row*bay_d), X(bw), Y(row*bay_d))

    # Secondary framing
    c.setStrokeColor(HexColor('#777777')); c.setLineWidth(1.2)
    c.setDash([])
    for col in range(COLS_X):
        c.line(X((col+0.5)*bay_w), Y(0), X((col+0.5)*bay_w), Y(bd))
    for row in range(COLS_Y):
        c.line(X(0), Y((row+0.5)*bay_d), X(bw), Y((row+0.5)*bay_d))

    # Columns (filled squares)
    COL_SZ = 0.67 * scale_s
    for cx_i in range(COLS_X+1):
        for cy_i in range(COLS_Y+1):
            px = X(cx_i*bay_w) - COL_SZ/2
            py = Y(cy_i*bay_d) - COL_SZ/2
            c.setFillColor(C_WALL); c.setStrokeColor(C_BLK); c.setLineWidth(0.4)
            c.rect(px, py, COL_SZ, COL_SZ, fill=1, stroke=1)
            # Column mark
            mark = f"{chr(65+cy_i)}{cx_i+1}"
            c.setFillColor(C_DIM); c.setFont('Helvetica-Bold', 6)
            c.drawCentredString(px + COL_SZ/2, py + COL_SZ + 2, mark)

    # Grid bubbles
    for i in range(COLS_X+1):
        cx2 = X(i*bay_w); cy2 = Y(bd) + 22
        c.setFillColor(C_WHT); c.setStrokeColor(C_DIM); c.setLineWidth(0.8)
        c.circle(cx2, cy2, 10, fill=1, stroke=1)
        c.setFillColor(C_DIM); c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(cx2, cy2-3, str(i+1))
    for i in range(COLS_Y+1):
        cy2 = Y(i*bay_d); cx2 = X(0) - 22
        c.setFillColor(C_WHT); c.setStrokeColor(C_DIM); c.setLineWidth(0.8)
        c.circle(cx2, cy2, 10, fill=1, stroke=1)
        c.setFillColor(C_DIM); c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(cx2, cy2-3, chr(65+i))

    # Bay dimensions
    for i in range(COLS_X):
        x1 = X(i*bay_w); x2 = X((i+1)*bay_w)
        _dim_line(c, x1, Y(0), x2, Y(0), f"{bay_w:.0f}'-0\"", offset=22)
    for i in range(COLS_Y):
        y1 = Y(i*bay_d); y2 = Y((i+1)*bay_d)
        _dim_line(c, X(0), y1, X(0), y2, f"{bay_d:.0f}'-0\"", offset=26, horiz=False)

    # Structural notes
    sdc = (job.get('jurisdiction_details') or {}).get('seismic_design_category','B')
    wind = (job.get('jurisdiction_details') or {}).get('wind_speed_mph', 90)
    ct = job.get('construction_type','Type II-A')
    NX = DRAW_X1 - 2.0*inch; NY = DRAW_Y1 - 15
    c.setFont('Helvetica-Bold', 8); c.setFillColor(C_NOTE)
    c.drawString(NX, NY, 'STRUCTURAL NOTES')
    c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
    c.line(NX, NY-3, NX+1.9*inch, NY-3)
    struct_notes = [
        f'1. Construction Type: {ct}',
        f'2. Seismic SDC: {sdc}  /  ASCE 7',
        f'3. Wind Speed: {wind or "N/A"} mph (Exp. C)',
        '4. Live Load: 50 PSF (office), 100 PSF (corridor)',
        '5. Dead Load: 20 PSF superimposed',
        '6. Columns: HSS 8×8×½ steel tube (typical)',
        '7. Primary beams: W14×48 (typical)',
        '8. Secondary: W10×30 at mid-bay (typical)',
        '9. Slab: 6" normal-weight concrete on deck',
        '10. Connections per AISC per structural EOR.',
        '11. Geotechnical report governs all footing design.',
    ]
    c.setFont('Helvetica', 7); c.setFillColor(HexColor('#333'))
    for i, note in enumerate(struct_notes):
        c.drawString(NX, NY - 16 - i*11, note)


# ─────────────────────────────────────────────────────────────────────────
#  Sheet FP5.0 – Fire & Life Safety
# ─────────────────────────────────────────────────────────────────────────

def _sheet_fire_life_safety(c, job, placed, bw, bd):
    """Fire & life safety plan: egress, exits, extinguishers."""
    scale_f = min((DRAW_W - 2.5*inch) / (bw + 6),
                  (DRAW_H - 1.5*inch) / (bd + 6)) * 0.85
    ox = DRAW_X0 + (DRAW_W - bw*scale_f) / 2
    oy = DRAW_Y0 + (DRAW_H - bd*scale_f) / 2 + 0.3*inch

    def X(ft): return ox + ft * scale_f
    def Y(ft): return oy + ft * scale_f

    MAR = 1.5

    # Building outline
    c.setFillColor(HexColor('#f9f6ee'))
    c.rect(X(MAR), Y(MAR), (bw-2*MAR)*scale_f, (bd-2*MAR)*scale_f, fill=1, stroke=0)
    c.setFillColor(C_WALL); c.setStrokeColor(C_BLK); c.setLineWidth(2.5)
    c.rect(X(MAR), Y(MAR), (bw-2*MAR)*scale_f, (bd-2*MAR)*scale_f, fill=0, stroke=1)

    # Room outlines (light)
    for r in placed:
        rx, ry = r.get('_x',0), r.get('_y',0)
        rw = r.get('_w', r.get('width_ft',12))
        rd = r.get('_d', r.get('depth_ft',14))
        c.setFillColor(HexColor('#f0ece0'))
        c.setStrokeColor(HexColor('#aaaaaa')); c.setLineWidth(0.5)
        c.rect(X(rx), Y(ry), rw*scale_f, rd*scale_f, fill=1, stroke=1)
        c.setFillColor(HexColor('#888')); c.setFont('Helvetica', 6)
        c.drawCentredString(X(rx+rw/2), Y(ry+rd/2)-3, r.get('name','')[:15])

    # Egress paths (green arrows)
    exits = [
        (MAR, bd/2, -1, 0, 'EXIT 1'),
        (bw-MAR, bd/2,  1, 0, 'EXIT 2'),
        (bw/2,   MAR, 0, -1, 'EXIT 3'),
    ]
    for (ex_ft, ey_ft, dx, dy, lbl) in exits:
        ex2 = X(ex_ft); ey2 = Y(ey_ft)
        alen = 36
        c.setStrokeColor(C_EXIT); c.setFillColor(C_EXIT); c.setLineWidth(2.5)
        c.line(ex2, ey2, ex2 + dx*alen, ey2 + dy*alen)
        # Arrowhead
        p = c.beginPath()
        p.moveTo(ex2+dx*alen, ey2+dy*alen)
        perp_x = -dy; perp_y = dx
        p.lineTo(ex2+dx*alen - dx*8 + perp_x*6, ey2+dy*alen - dy*8 + perp_y*6)
        p.lineTo(ex2+dx*alen - dx*8 - perp_x*6, ey2+dy*alen - dy*8 - perp_y*6)
        p.close(); c.drawPath(p, fill=1, stroke=0)
        # Label
        c.setFont('Helvetica-Bold', 8); c.setFillColor(C_EXIT)
        lx2 = ex2 + dx*alen + dx*8; ly2 = ey2 + dy*alen + dy*8
        c.drawCentredString(lx2, ly2-3, lbl)

    # Egress travel path
    c.setStrokeColor(HexColor('#e67e22')); c.setLineWidth(1.2); c.setDash([6,4])
    c.line(X(bw/2), Y(bd/2), X(MAR*0.3), Y(bd/2))
    c.line(X(bw/2), Y(bd/2), X(bw-MAR*0.3), Y(bd/2))
    c.setDash([])

    # Fire extinguisher symbols
    FE_POS = [(bw*0.3, bd*0.5), (bw*0.7, bd*0.5), (bw/2, bd*0.8)]
    for (fex, fey) in FE_POS:
        px = X(fex); py = Y(fey)
        c.setFillColor(C_EXIT); c.setLineWidth(0)
        c.circle(px, py, 7, fill=1, stroke=0)
        c.setFillColor(C_WHT); c.setFont('Helvetica-Bold', 7)
        c.drawCentredString(px, py-2.5, 'FE')

    # Sprinkler heads
    if job.get('sprinklered'):
        for r in placed:
            rx, ry = r.get('_x',0), r.get('_y',0)
            rw = r.get('_w',12); rd = r.get('_d',14)
            px = X(rx+rw/2); py = Y(ry+rd/2)+15
            c.setStrokeColor(C_WIN); c.setLineWidth(0.8)
            c.circle(px, py, 4, fill=0, stroke=1)
            c.line(px-5, py, px+5, py)
            c.line(px, py-5, px, py+5)

    # Legend
    lx = DRAW_X1 - 2.1*inch; ly = DRAW_Y0 + 10
    legends = [
        (C_EXIT, '→', 'EXIT / EGRESS DIRECTION'),
        (HexColor('#e67e22'), '---', 'EGRESS TRAVEL PATH'),
        (C_EXIT, '●', 'FIRE EXTINGUISHER'),
        (C_WIN, '⊕', 'SPRINKLER HEAD'),
    ]
    c.setFont('Helvetica-Bold', 7.5); c.setFillColor(C_NOTE)
    c.drawString(lx, ly + len(legends)*14 + 6, 'LIFE SAFETY LEGEND')
    for i, (col, sym, text) in enumerate(legends):
        c.setFillColor(col); c.setFont('Helvetica-Bold', 9)
        c.drawString(lx, ly + i*14 + 2, sym)
        c.setFillColor(C_NOTE); c.setFont('Helvetica', 7.5)
        c.drawString(lx + 18, ly + i*14 + 2, text)

    # IBC egress notes
    r_data = job.get('compliance_report', {})
    NX = DRAW_X0 + 10; NY = DRAW_Y1 - 15
    c.setFont('Helvetica-Bold', 7.5); c.setFillColor(C_NOTE)
    c.drawString(NX, NY, 'EGRESS / CODE NOTES')
    c.setStrokeColor(C_DIM); c.setLineWidth(0.4)
    c.line(NX, NY-3, NX + 2.2*inch, NY-3)
    occ_load = int((job.get('gross_sq_ft') or 10000) / 150)
    exits_req = 2 if occ_load <= 500 else 3 if occ_load <= 1000 else 4
    enotes = [
        f'1. Occupant Load: {occ_load} persons (IBC Table 1004.1)',
        f'2. Number of Exits Required: {exits_req} (IBC §1006.3)',
        '3. Min. Exit Access Door Width: 32" clear (IBC §1010.1)',
        '4. Max Travel Distance: 250 ft sprinklered (IBC §1017)',
        '5. Corridor Width: 44" minimum (IBC §1020.2)',
        '6. Exit Signage: per IBC §1013 at all exits',
        '7. Emergency Lighting: per IBC §1008 throughout',
        f'8. Fire Extinguishers: Class ABC, 75 ft max travel',
        '9. All doors to swing in direction of travel when',
        '   serving 50+ occupants (IBC §1010.1.2)',
    ]
    c.setFont('Helvetica', 6.5); c.setFillColor(HexColor('#333'))
    for i, note in enumerate(enotes):
        c.drawString(NX, NY - 16 - i*11, note)

    _north_arrow(c, DRAW_X1 - 1.6*inch, DRAW_Y0 + 65)


# ─────────────────────────────────────────────────────────────────────────
#  Cover Sheet
# ─────────────────────────────────────────────────────────────────────────

def _sheet_cover(c, job, total_sheets):
    """Professional cover sheet with project summary and compliance."""
    CX = PW / 2

    # Dark header band
    c.setFillColor(C_TBKG)
    c.rect(0, PH - 5*inch, PW, 5*inch, fill=1, stroke=0)

    # Project name
    c.setFillColor(C_TACC); c.setFont('Helvetica-Bold', 36)
    name = job.get('project_name','Project')
    c.drawCentredString(CX, PH - 1.4*inch, name[:40])

    # Sub-line
    c.setFillColor(C_TSUB); c.setFont('Helvetica', 16)
    c.drawCentredString(CX, PH - 2.1*inch,
        job.get('building_type','Commercial') + '  ·  ' + job.get('primary_code','IBC 2023'))
    c.setFont('Helvetica', 13)
    c.drawCentredString(CX, PH - 2.65*inch,
        job.get('jurisdiction_preset','') + '  ·  Issued: ' + _now())

    # Decorative line
    c.setStrokeColor(C_TACC); c.setLineWidth(2.5)
    c.line(BDR_L + inch, PH - 3.1*inch, PW - inch, PH - 3.1*inch)

    # Compliance badge
    rpt = job.get('compliance_report', {})
    compliant = rpt.get('is_compliant', True)
    c.setFillColor(C_GRN if compliant else C_RED)
    c.roundRect(CX - 0.8*inch, PH - 4.5*inch, 1.6*inch, 0.7*inch, 6, fill=1, stroke=0)
    c.setFillColor(C_WHT); c.setFont('Helvetica-Bold', 13)
    c.drawCentredString(CX, PH - 4.08*inch,
                        '✓  COMPLIANT' if compliant else '✗  NON-COMPLIANT')

    # Project data boxes
    box_data = [
        ('OCCUPANCY GROUP', job.get('occupancy_group','B')),
        ('CONSTRUCTION TYPE', (job.get('construction_type','') or '')[:16]),
        ('GROSS FLOOR AREA', f"{int(job.get('gross_sq_ft') or 10000):,} SF"),
        ('STORIES', str(job.get('num_stories') or 1)),
        ('SPRINKLER SYSTEM', 'Required' if job.get('sprinklered') else 'Not Required'),
        ('SEISMIC SDC', (job.get('jurisdiction_details') or {}).get('seismic_design_category','B')),
    ]
    n = len(box_data); cols = 3
    box_w = (DRAW_W - inch) / cols; box_h = 0.9*inch
    base_y = PH - 5*inch - box_h - 0.4*inch
    for i, (lbl, val) in enumerate(box_data):
        col = i % cols; row = i // cols
        bx = DRAW_X0 + col * box_w
        by = base_y - row * (box_h + 8)
        c.setFillColor(HexColor('#eef2f8'))
        c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
        c.roundRect(bx+3, by, box_w - 8, box_h, 4, fill=1, stroke=1)
        c.setFillColor(C_DIM); c.setFont('Helvetica', 7.5)
        c.drawCentredString(bx + box_w/2, by + box_h - 16, lbl)
        c.setFillColor(C_NOTE); c.setFont('Helvetica-Bold', 13)
        c.drawCentredString(bx + box_w/2, by + box_h*0.25, val)

    # Sheet index table
    ix = DRAW_X0 + 0.5*inch
    iy = base_y - 2*(box_h+8) - 0.5*inch
    c.setFillColor(C_TBKG); c.setStrokeColor(C_DIM); c.setLineWidth(0.5)
    c.roundRect(ix, iy - total_sheets*18 - 10, DRAW_W - inch, total_sheets*18+30, 6, fill=1, stroke=1)
    c.setFillColor(C_WHT); c.setFont('Helvetica-Bold', 9)
    c.drawString(ix+10, iy+5, 'DRAWING INDEX')
    c.setStrokeColor(HexColor('#2a4a6a')); c.setLineWidth(0.4)
    c.line(ix+10, iy+2, ix + DRAW_W - inch - 10, iy+2)
    sheet_list = [
        ('G0.0', 'Cover Sheet & Project Information'),
        ('A1.0', 'Architectural Floor Plan – Level 1'),
        ('A3.0', 'Exterior Elevations – All Sides'),
        ('C2.0', 'Site Plan'),
        ('S4.0', 'Structural Framing Plan'),
        ('FP5.0','Fire & Life Safety Plan'),
    ][:total_sheets]
    for i, (num, desc) in enumerate(sheet_list):
        sy = iy - 14 - i*18
        c.setFillColor(HexColor('#eef2f8') if i%2==0 else C_TBKG)
        c.setFillColor(C_TACC); c.setFont('Helvetica-Bold', 8.5)
        c.drawString(ix+12, sy, num)
        c.setFillColor(C_TSUB); c.setFont('Helvetica', 8)
        c.drawString(ix+80, sy, desc)

    # Stamp box
    sx = DRAW_X1 - 1.8*inch; sy = DRAW_Y0 + 1.2*inch
    c.setFillColor(C_WHT); c.setStrokeColor(C_DIM); c.setLineWidth(1.0)
    c.roundRect(sx, sy, 1.6*inch, 1.5*inch, 6, fill=1, stroke=1)
    c.circle(sx + 0.8*inch, sy + 0.9*inch, 0.5*inch, fill=0, stroke=1)
    c.setFont('Helvetica', 5.5); c.setFillColor(C_DIM)
    c.drawCentredString(sx + 0.8*inch, sy + 0.88*inch, 'ARCHITECT SEAL')
    c.drawCentredString(sx + 0.8*inch, sy + 0.72*inch, 'PLACE HERE')
    c.setFont('Helvetica', 6.5)
    c.drawCentredString(sx + 0.8*inch, sy + 0.2*inch, 'Architectural AI Platform')
    c.drawCentredString(sx + 0.8*inch, sy + 0.06*inch, _yr())


# ─────────────────────────────────────────────────────────────────────────
#  Master PDF Exporter
# ─────────────────────────────────────────────────────────────────────────

class PDFExporter:
    def __init__(self, job: Dict):
        self.job = job

    def generate(self) -> bytes:
        if not _RL_OK:
            raise RuntimeError(f"reportlab not available on this runtime: {_rl_err_msg}")
        buf = io.BytesIO()
        c   = rl_canvas.Canvas(buf, pagesize=(PW, PH))
        c.setTitle(self.job.get('project_name','Project') + ' – Construction Documents')

        rooms  = _canonical_rooms(self.job)
        placed, bw, bd = _layout(rooms)

        # Calculate drawing scale
        scale  = min((DRAW_W - 2.5*inch) / (bw + 6),
                     (DRAW_H - 1.5*inch) / (bd + 6)) * 0.82

        SHEETS = [
            ('G0.0',  'Cover Sheet'),
            ('A1.0',  'Floor Plan – Level 1'),
            ('A3.0',  'Exterior Elevations'),
            ('C2.0',  'Site Plan'),
            ('S4.0',  'Structural Framing Plan'),
            ('FP5.0', 'Fire & Life Safety Plan'),
        ]

        # Add sheets only for requested drawing sets
        draw_sets = set(self.job.get('drawing_sets', SHEETS))
        def want(name):
            keywords = {'Floor': 'Floor Plan', 'Elev': 'Exterior Elevations',
                        'Site': 'Site Plan', 'Struct': 'Structural',
                        'Fire': 'Fire', 'Cover': 'Cover'}
            return True  # Always generate all 6 for now

        total = len(SHEETS)
        sheet_funcs = {
            'G0.0':  lambda c: _sheet_cover(c, self.job, total),
            'A1.0':  lambda c: _sheet_floor_plan(c, self.job, rooms, placed,
                                                  bw, bd, scale, 'A1.0', total),
            'A3.0':  lambda c: _sheet_elevations(c, self.job, bw, bd),
            'C2.0':  lambda c: _sheet_site_plan(c, self.job, bw, bd),
            'S4.0':  lambda c: _sheet_structural(c, self.job, bw, bd),
            'FP5.0': lambda c: _sheet_fire_life_safety(c, self.job, placed, bw, bd),
        }

        for i, (num, title) in enumerate(SHEETS):
            c.saveState()
            sheet_funcs[num](c)
            _border(c)
            sc = "1/8\"=1'-0\"" if num != 'G0.0' else 'N/A'
            _title_block(c, self.job, num, title, sc, total)
            c.restoreState()
            c.showPage()

        c.save()
        return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────
#  DXF Exporter  (AIA layers, real geometry)
# ─────────────────────────────────────────────────────────────────────────

class DXFExporter:
    def __init__(self, job: Dict):
        self.job = job

    def generate_all_sheets(self) -> Dict[str, bytes]:
        rooms  = _canonical_rooms(self.job)
        placed, bw, bd = _layout(rooms)
        sheets = {}
        for fname, title, fn in [
            ('A1.0_Floor_Plan.dxf',          'Floor Plan',          lambda: self._floor_plan_dxf(placed, bw, bd)),
            ('A3.0_Exterior_Elevations.dxf',  'Exterior Elevations', lambda: self._elevation_dxf(bw, bd)),
            ('C2.0_Site_Plan.dxf',            'Site Plan',           lambda: self._site_dxf(bw, bd)),
            ('S4.0_Structural.dxf',           'Structural',          lambda: self._structural_dxf(bw, bd)),
            ('FP5.0_Fire_Safety.dxf',         'Fire & Life Safety',  lambda: self._fire_dxf(placed, bw, bd)),
        ]:
            try:
                sheets[fname] = fn()
            except Exception as e:
                sheets[fname] = self._error_dxf(title, str(e))
        return sheets

    def _new_doc(self, title):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc = ezdxf.new('R2010')
        doc.units = ezdxf.units.IN
        layers = [
            ('A-WALL',      7,  50),  ('A-WALL-INTR',  7,  35),
            ('A-GLAZ',      4,  18),  ('A-DOOR',       7,  25),
            ('A-ANNO-TEXT', 2,  18),  ('A-ANNO-DIMS',  2,  13),
            ('A-FLOR-PATT', 8,  13),  ('A-ANNO-BORD',  7,  70),
            ('A-GRID',      3,   9),  ('S-COLS',       7,  50),
            ('C-PROP',      2,  25),  ('C-PARK',       4,  13),
            ('FP-EXIT',     1,  25),  ('FP-EQUIP',     1,  18),
        ]
        for name, colour, lw in layers:
            try:
                l = doc.layers.add(name)
                l.color = colour; l.lineweight = lw
            except Exception:
                pass
        return doc

    def _write(self, doc) -> bytes:
        import io as _io
        buf = _io.StringIO()
        doc.write(buf)
        return buf.getvalue().encode('utf-8')

    def _f(self, ft): return ft * 12   # feet → inches

    def _floor_plan_dxf(self, placed, bw, bd):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc = self._new_doc('Floor Plan')
        msp = doc.modelspace()
        f = self._f
        MAR = 1.5

        # Exterior wall
        pts = [(f(MAR),f(MAR)),(f(bw-MAR),f(MAR)),(f(bw-MAR),f(bd-MAR)),(f(MAR),f(bd-MAR))]
        msp.add_lwpolyline(pts, dxfattribs={'layer':'A-WALL','closed':True})

        # Interior walls + rooms
        drawn = set()
        for r in placed:
            rx,ry = r.get('_x',0), r.get('_y',0)
            rw = r.get('_w', r.get('width_ft',12))
            rd = r.get('_d', r.get('depth_ft',14))
            for (ax,ay,bx2,by2) in [(rx,ry,rx+rw,ry),(rx+rw,ry,rx+rw,ry+rd),
                                      (rx,ry+rd,rx+rw,ry+rd),(rx,ry,rx,ry+rd)]:
                k = (round(min(ax,bx2),1),round(min(ay,by2),1),round(max(ax,bx2),1),round(max(ay,by2),1))
                if k in drawn: continue
                drawn.add(k)
                msp.add_line((f(ax),f(ay)),(f(bx2),f(by2)), dxfattribs={'layer':'A-WALL-INTR'})
            # Label
            cx = f(rx+rw/2); cy = f(ry+rd/2)
            msp.add_text(r.get('name',''), dxfattribs={'layer':'A-ANNO-TEXT','height':f(0.7)}
            ).set_placement((cx,cy+f(0.4)), align=TextEntityAlignment.CENTER)
            msp.add_text(f"{r.get('sqft',int(rw*rd)):.0f} SF",
                         dxfattribs={'layer':'A-ANNO-TEXT','height':f(0.5)}
            ).set_placement((cx,cy-f(0.4)), align=TextEntityAlignment.CENTER)

        # Overall dimensions
        dim_y = f(MAR) - 18
        msp.add_line((f(MAR),dim_y),(f(bw-MAR),dim_y), dxfattribs={'layer':'A-ANNO-DIMS'})
        msp.add_text(f"{int(bw-2*MAR)}'-0\"",
                     dxfattribs={'layer':'A-ANNO-DIMS','height':f(0.7)}
        ).set_placement((f(bw/2),dim_y-f(1)), align=TextEntityAlignment.CENTER)

        # Title block
        msp.add_text(job_val(self.job,'project_name','Project'),
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(1.2)}
        ).set_placement((f(MAR),f(bd)+f(2)), align=TextEntityAlignment.LEFT)
        msp.add_text('FLOOR PLAN – LEVEL 1  |  Scale: 1/8"=1\'-0"  |  ' + _now(),
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(0.65)}
        ).set_placement((f(MAR),f(bd)+f(3.2)), align=TextEntityAlignment.LEFT)
        msp.add_text('A1.0', dxfattribs={'layer':'A-ANNO-BORD','height':f(1.8)}
        ).set_placement((f(bw-MAR),f(bd)+f(2)), align=TextEntityAlignment.RIGHT)
        return self._write(doc)

    def _elevation_dxf(self, bw, bd):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc = self._new_doc('Elevations'); msp = doc.modelspace(); f=self._f
        floors = int(self.job.get('num_stories') or 1)
        flr_h  = 12.0; bh = floors*flr_h; rh=3.0
        for ei, (wx, label, width) in enumerate([
            (0, 'SOUTH ELEVATION', bw),
            (bw+10, 'NORTH ELEVATION', bw),
            (0, 'EAST ELEVATION', bd),
            (bd+10, 'EAST ELEVATION', bd),
        ]):
            ox = f(ei*(bw+10) if ei<2 else 0)
            oy = f(0 if ei<2 else (bh+rh+8))
            W  = f(width); H=f(bh); RH=f(rh)
            msp.add_lwpolyline([(ox,oy),(ox+W,oy),(ox+W,oy+H),(ox,oy+H)],
                               dxfattribs={'layer':'A-WALL','closed':True})
            msp.add_lwpolyline([(ox-f(0.5),oy+H),(ox+W+f(0.5),oy+H),(ox+W+f(0.5),oy+H+RH),(ox-f(0.5),oy+H+RH)],
                               dxfattribs={'layer':'A-WALL','closed':True})
            for fl in range(1,floors):
                fy=oy+f(fl*flr_h)
                msp.add_line((ox,fy),(ox+W,fy),dxfattribs={'layer':'A-WALL-INTR'})
            msp.add_text(label,dxfattribs={'layer':'A-ANNO-TEXT','height':f(0.9)}
            ).set_placement((ox+W/2,oy-f(2.5)),align=TextEntityAlignment.CENTER)
        msp.add_text(job_val(self.job,'project_name','Project') + ' – EXTERIOR ELEVATIONS',
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(1.2)}
        ).set_placement((0,f(bh+rh+18)),align=TextEntityAlignment.LEFT)
        return self._write(doc)

    def _site_dxf(self, bw, bd):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc=self._new_doc('Site Plan'); msp=doc.modelspace(); f=self._f
        SW=bw*3.2; SD=bd*3.0; SET=bw*0.4; SETD=bd*0.4
        ox=(SW-bw)/2; oy=(SD-bd)/2
        msp.add_lwpolyline([(0,0),(f(SW),0),(f(SW),f(SD)),(0,f(SD))],
                           dxfattribs={'layer':'C-PROP','closed':True,'linetype':'DASHED'})
        msp.add_lwpolyline([(f(SET),f(SETD)),(f(SW-SET),f(SETD)),
                            (f(SW-SET),f(SD-SETD)),(f(SET),f(SD-SETD))],
                           dxfattribs={'layer':'C-PROP','closed':True,'linetype':'DASHED'})
        msp.add_lwpolyline([(f(ox),f(oy)),(f(ox+bw),f(oy)),
                            (f(ox+bw),f(oy+bd)),(f(ox),f(oy+bd))],
                           dxfattribs={'layer':'A-WALL','closed':True})
        msp.add_text('BUILDING',dxfattribs={'layer':'A-ANNO-TEXT','height':f(1)}
        ).set_placement((f(ox+bw/2),f(oy+bd/2)),align=TextEntityAlignment.CENTER)
        msp.add_text(job_val(self.job,'project_name','Project') + ' – SITE PLAN',
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(1.2)}
        ).set_placement((0,f(SD)+f(3)),align=TextEntityAlignment.LEFT)
        return self._write(doc)

    def _structural_dxf(self, bw, bd):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc=self._new_doc('Structural'); msp=doc.modelspace(); f=self._f
        COLS_X=4; COLS_Y=3; bxw=bw/COLS_X; byd=bd/COLS_Y
        for i in range(COLS_X+1):
            msp.add_line((f(i*bxw),0),(f(i*bxw),f(bd)),dxfattribs={'layer':'S-COLS'})
        for i in range(COLS_Y+1):
            msp.add_line((0,f(i*byd)),(f(bw),f(i*byd)),dxfattribs={'layer':'S-COLS'})
        CS=f(0.67)
        for cx2 in range(COLS_X+1):
            for cy2 in range(COLS_Y+1):
                px=f(cx2*bxw); py=f(cy2*byd)
                msp.add_lwpolyline([(px-CS/2,py-CS/2),(px+CS/2,py-CS/2),
                                    (px+CS/2,py+CS/2),(px-CS/2,py+CS/2)],
                                   dxfattribs={'layer':'S-COLS','closed':True})
                msp.add_text(f"{chr(65+cy2)}{cx2+1}",
                             dxfattribs={'layer':'A-ANNO-TEXT','height':f(0.5)}
                ).set_placement((px,py+CS/2+f(0.3)),align=TextEntityAlignment.CENTER)
        msp.add_text(job_val(self.job,'project_name','Project') + ' – STRUCTURAL FRAMING PLAN',
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(1.2)}
        ).set_placement((0,f(bd)+f(3)),align=TextEntityAlignment.LEFT)
        return self._write(doc)

    def _fire_dxf(self, placed, bw, bd):
        import ezdxf
        from ezdxf.enums import TextEntityAlignment
        doc=self._new_doc('Fire Safety'); msp=doc.modelspace(); f=self._f
        MAR=1.5
        msp.add_lwpolyline([(f(MAR),f(MAR)),(f(bw-MAR),f(MAR)),
                            (f(bw-MAR),f(bd-MAR)),(f(MAR),f(bd-MAR))],
                           dxfattribs={'layer':'A-WALL','closed':True})
        for r in placed:
            rx,ry=r.get('_x',0),r.get('_y',0)
            rw=r.get('_w',12); rd=r.get('_d',14)
            msp.add_lwpolyline([(f(rx),f(ry)),(f(rx+rw),f(ry)),
                                (f(rx+rw),f(ry+rd)),(f(rx),f(ry+rd))],
                               dxfattribs={'layer':'A-WALL-INTR','closed':True})
        exits=[(MAR,bd/2,'EXIT 1'),(bw-MAR,bd/2,'EXIT 2'),(bw/2,MAR,'EXIT 3')]
        for ex2,ey2,lbl in exits:
            msp.add_text(lbl,dxfattribs={'layer':'FP-EXIT','height':f(1.2)}
            ).set_placement((f(ex2),f(ey2)),align=TextEntityAlignment.CENTER)
        for i,(fex,fey) in enumerate([(bw*0.3,bd/2),(bw*0.7,bd/2),(bw/2,bd*0.8)]):
            msp.add_circle((f(fex),f(fey)),f(0.8),dxfattribs={'layer':'FP-EQUIP'})
            msp.add_text('FE',dxfattribs={'layer':'FP-EQUIP','height':f(0.6)}
            ).set_placement((f(fex),f(fey-0.3)),align=TextEntityAlignment.CENTER)
        msp.add_text(job_val(self.job,'project_name','Project') + ' – FIRE & LIFE SAFETY',
                     dxfattribs={'layer':'A-ANNO-BORD','height':f(1.2)}
        ).set_placement((0,f(bd)+f(3)),align=TextEntityAlignment.LEFT)
        return self._write(doc)

    def _error_dxf(self, title, err):
        import ezdxf; from ezdxf.enums import TextEntityAlignment
        doc=ezdxf.new('R2010'); msp=doc.modelspace()
        msp.add_text(f'{title}: {err[:80]}',dxfattribs={'height':6}
        ).set_placement((0,0),align=TextEntityAlignment.LEFT)
        buf=io.StringIO(); doc.write(buf); return buf.getvalue().encode()


def job_val(job,key,default=''):
    return job.get(key) or default


# ─────────────────────────────────────────────────────────────────────────
#  ZIP Package
# ─────────────────────────────────────────────────────────────────────────

def build_export_package(job: Dict) -> bytes:
    exporter = PDFExporter(job)
    pdf = exporter.generate()
    dxf_sheets = DXFExporter(job).generate_all_sheets()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('construction_documents.pdf', pdf)
        for fname, data in dxf_sheets.items():
            zf.writestr(f'cad/{fname}', data)
        manifest = {
            'project_name':    job.get('project_name',''),
            'generated':       _now(),
            'sheet_count':     6,
            'sheets': [
                {'number':'G0.0','title':'Cover Sheet'},
                {'number':'A1.0','title':'Floor Plan – Level 1'},
                {'number':'A3.0','title':'Exterior Elevations'},
                {'number':'C2.0','title':'Site Plan'},
                {'number':'S4.0','title':'Structural Framing Plan'},
                {'number':'FP5.0','title':'Fire & Life Safety Plan'},
            ],
            'compliance': job.get('compliance_report',{}),
        }
        import json
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))
    return buf.getvalue()
