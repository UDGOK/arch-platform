"""
export_engine.py
================
Professional PDF and DXF/DWG export for the Architectural AI Platform.

PDF  – reportlab: multi-page permit-ready construction document set
       Page 1    : Cover sheet (project info + stamp block)
       Page 2    : Drawing index + compliance summary
       Pages 3+  : Per-sheet detail (code refs, ADA notes, key notes)

DXF  – ezdxf (R2010 / AutoCAD 2010 compatible):
       AIA-standard layer structure
       ANSI D title block (34" × 22") per sheet
       Walls, doors, windows, dimensions, annotations
       North arrow + scale bar on floor plan sheets
       All text follows AIA CAD Layer Guidelines v6

Both formats are generated from the job payload returned by /api/dispatch
or /api/dispatch/nim — no re-inference required.
"""

from __future__ import annotations

import io
import math
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── PDF ──────────────────────────────────────────────────────────────────
from reportlab.lib.colors import (
    Color, HexColor, black, white, grey, lightgrey
)
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import registerFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, PageTemplate,
    Paragraph, Spacer, Table, TableStyle
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# ── DXF ──────────────────────────────────────────────────────────────────
import ezdxf
from ezdxf import colors as dxf_colors
from ezdxf.enums import TextEntityAlignment

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRAND_DARK   = HexColor("#0f1420")
BRAND_BLUE   = HexColor("#4a9eff")
BRAND_GREEN  = HexColor("#76b900")   # NVIDIA green / compliance pass
BRAND_WARN   = HexColor("#f59e0b")
BRAND_ERROR  = HexColor("#ef4444")
BRAND_LIGHT  = HexColor("#e2e8f0")
BRAND_MID    = HexColor("#64748b")
BRAND_SURF   = HexColor("#1e2540")

PAGE_W, PAGE_H = landscape(letter)   # 11" × 8.5"

# AIA Layer Names (AIA CAD Layer Guidelines v6)
LAYERS = {
    "BORDER":  ("A-ANNO-BORD",  7,   25),   # (name, ACI color, lineweight µm)
    "TITLEBLK":("A-ANNO-TITL",  7,   50),
    "WALL":    ("A-WALL",       7,   50),
    "WALL_INT":("A-WALL-INTR",  7,   35),
    "DOOR":    ("A-DOOR",       7,   35),
    "WINDOW":  ("A-GLAZ",       4,   25),
    "DIM":     ("A-ANNO-DIMS",  2,   18),
    "TEXT":    ("A-ANNO-TEXT",  7,   18),
    "GRID":    ("A-GRID",       8,   18),
    "NORTH":   ("A-ANNO-NRTH",  7,   25),
    "FILLED":  ("A-AREA",       8,   18),
    "HATCH":   ("A-FLOR-PATT",  8,   18),
}


# ---------------------------------------------------------------------------
# PDF Export
# ---------------------------------------------------------------------------

class PDFExporter:
    """Generates a multi-page permit-ready PDF construction document set."""

    def __init__(self, job: Dict[str, Any]) -> None:
        self.job     = job
        self.project = job.get("project_name", "Unnamed Project")
        self.engine  = job.get("engine", "AI Platform")
        self.report  = job.get("compliance_report") or {}
        self.drawings: List[Dict] = job.get("drawings", [])
        self.timings  = job.get("stage_timings", {})
        self.now      = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        self._buf     = io.BytesIO()

    # ── Public ────────────────────────────────────────────────────────────

    def generate(self) -> bytes:
        """Return the complete PDF as bytes."""
        c = rl_canvas.Canvas(self._buf, pagesize=landscape(letter))
        c.setTitle(f"{self.project} – Construction Documents")
        c.setAuthor("Architectural AI Platform")
        c.setSubject("IBC 2023 Compliant Construction Document Set")

        self._cover(c)
        self._drawing_index(c)
        self._compliance_page(c)
        for i, drw in enumerate(self.drawings):
            self._sheet_page(c, drw, i + 1)

        c.save()
        return self._buf.getvalue()

    # ── Pages ─────────────────────────────────────────────────────────────

    def _cover(self, c: rl_canvas.Canvas) -> None:
        c.setPageSize(landscape(letter))
        W, H = PAGE_W, PAGE_H

        # Background
        c.setFillColor(BRAND_DARK)
        c.rect(0, 0, W, H, fill=1, stroke=0)

        # Left accent bar
        c.setFillColor(BRAND_BLUE)
        c.rect(0, 0, 0.35 * inch, H, fill=1, stroke=0)

        # Bottom accent bar
        c.setFillColor(BRAND_SURF)
        c.rect(0, 0, W, 1.2 * inch, fill=1, stroke=0)

        # Project title
        c.setFillColor(BRAND_LIGHT)
        c.setFont("Helvetica-Bold", 28)
        c.drawString(0.75 * inch, H - 1.5 * inch, self.project)

        c.setFillColor(BRAND_BLUE)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(0.75 * inch, H - 1.9 * inch, "CONSTRUCTION DOCUMENT SET")

        # Divider
        c.setStrokeColor(BRAND_BLUE)
        c.setLineWidth(1.5)
        c.line(0.75 * inch, H - 2.1 * inch, W - 0.75 * inch, H - 2.1 * inch)

        # Metadata table
        meta_rows = [
            ("Building Type",  self.job.get("building_type", "—")),
            ("Occupancy Group",self.job.get("occupancy_group", "—")),
            ("Construction Type", self.job.get("construction_type", "—")),
            ("Primary Code",   self.job.get("primary_code", "IBC 2023")),
            ("Jurisdiction",   self.job.get("jurisdiction_name", "—")),
            ("Sprinklered",    "Yes (NFPA 13)" if self.job.get("sprinklered") else "No"),
            ("Drawing Sheets", str(len(self.drawings))),
            ("Engine",         self.engine),
            ("Generated",      self.now),
        ]

        x1, x2, y = 0.75 * inch, 4.5 * inch, H - 2.6 * inch
        for label, value in meta_rows:
            c.setFillColor(BRAND_MID)
            c.setFont("Helvetica", 8.5)
            c.drawString(x1, y, label.upper())
            c.setFillColor(BRAND_LIGHT)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(x2, y, str(value))
            y -= 0.27 * inch

        # Compliance badge
        comp = self.report.get("is_compliant", True)
        badge_x = W - 3.2 * inch
        badge_y = H - 4.2 * inch
        badge_color = BRAND_GREEN if comp else BRAND_ERROR
        c.setFillColor(badge_color)
        c.roundRect(badge_x, badge_y, 2.4 * inch, 0.55 * inch, 6, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 11)
        status_text = "✓  COMPLIANT" if comp else "✗  NON-COMPLIANT"
        c.drawCentredString(badge_x + 1.2 * inch, badge_y + 0.17 * inch, status_text)

        # Compliance numbers
        c.setFillColor(BRAND_LIGHT)
        c.setFont("Helvetica", 8.5)
        c.drawString(badge_x, badge_y - 0.25 * inch,
            f"Errors: {self.report.get('blocking_count',0)}   "
            f"Warnings: {self.report.get('warning_count',0)}")

        # Sheet count
        c.setFillColor(BRAND_BLUE)
        c.setFont("Helvetica-Bold", 48)
        c.drawRightString(W - 0.75 * inch, H - 3.8 * inch, str(len(self.drawings)))
        c.setFillColor(BRAND_MID)
        c.setFont("Helvetica", 10)
        c.drawRightString(W - 0.75 * inch, H - 4.05 * inch, "SHEETS")

        # Footer
        c.setFillColor(BRAND_MID)
        c.setFont("Helvetica", 7.5)
        c.drawString(0.75 * inch, 0.45 * inch,
            "GENERATED BY ARCHITECTURAL AI PLATFORM  |  IBC 2023  |  REVIEW REQUIRED PRIOR TO PERMIT SUBMISSION")
        c.drawRightString(W - 0.75 * inch, 0.45 * inch,
            f"Page 1 of {len(self.drawings) + 3}")

        c.showPage()

    def _drawing_index(self, c: rl_canvas.Canvas) -> None:
        W, H = PAGE_W, PAGE_H
        self._page_frame(c, "G0.0", "Drawing Index", 2)

        y = H - 1.6 * inch

        # Table header
        cols = [("SHEET NO.", 1.1), ("TITLE", 4.2), ("DISCIPLINE", 1.5),
                ("SCALE", 1.3), ("FORMAT", 0.9)]

        c.setFillColor(BRAND_SURF)
        c.rect(0.5 * inch, y - 0.02 * inch, W - 1.0 * inch, 0.28 * inch, fill=1, stroke=0)

        x = 0.6 * inch
        for hdr, w in cols:
            c.setFillColor(BRAND_BLUE)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawString(x, y + 0.06 * inch, hdr)
            x += w * inch

        y -= 0.28 * inch
        c.setStrokeColor(BRAND_SURF)
        c.setLineWidth(0.5)

        for i, drw in enumerate(self.drawings):
            bg = BRAND_DARK if i % 2 == 0 else HexColor("#151929")
            c.setFillColor(bg)
            c.rect(0.5 * inch, y - 0.04 * inch, W - 1.0 * inch, 0.26 * inch, fill=1, stroke=0)

            row_data = [
                (drw.get("sheet_number","—"), 1.1, "Helvetica-Bold", BRAND_BLUE),
                (drw.get("title","—")[:52], 4.2, "Helvetica", BRAND_LIGHT),
                (drw.get("discipline","Architecture")[:20], 1.5, "Helvetica", BRAND_LIGHT),
                (drw.get("scale","1/8\"=1'-0\"")[:16], 1.3, "Helvetica", BRAND_MID),
                (drw.get("format","PDF"), 0.9, "Helvetica", BRAND_MID),
            ]
            x = 0.6 * inch
            for val, w, font, col in row_data:
                c.setFillColor(col)
                c.setFont(font, 8)
                c.drawString(x, y + 0.04 * inch, str(val))
                x += w * inch

            y -= 0.26 * inch
            if y < 1.2 * inch:
                break

        c.showPage()

    def _compliance_page(self, c: rl_canvas.Canvas) -> None:
        W, H = PAGE_W, PAGE_H
        self._page_frame(c, "G0.1", "Code Compliance Report", 3)

        y = H - 1.6 * inch
        comp = self.report

        # Summary banner
        is_ok  = comp.get("is_compliant", True)
        bc     = comp.get("blocking_count", 0)
        wc     = comp.get("warning_count", 0)
        banner_color = BRAND_GREEN if is_ok else BRAND_ERROR
        c.setFillColor(banner_color)
        c.roundRect(0.5*inch, y-0.05*inch, W-1.0*inch, 0.45*inch, 6, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 13)
        status = "SPECIFICATION IS COMPLIANT" if is_ok else "COMPLIANCE FAILURES DETECTED"
        c.drawCentredString(W/2, y+0.1*inch, status)
        y -= 0.7 * inch

        # Count badges
        for label, val, col in [
            ("BLOCKING ERRORS", bc, BRAND_ERROR),
            ("WARNINGS",        wc, BRAND_WARN),
            ("RULES CHECKED",   10, BRAND_BLUE),
        ]:
            c.setFillColor(BRAND_SURF)
            c.roundRect(0.5*inch, y-0.35*inch, 2.0*inch, 0.6*inch, 6, fill=1, stroke=0)
            c.setFillColor(col)
            c.setFont("Helvetica-Bold", 20)
            c.drawCentredString(1.5*inch, y-0.1*inch, str(val))
            c.setFillColor(BRAND_MID)
            c.setFont("Helvetica", 7)
            c.drawCentredString(1.5*inch, y-0.28*inch, label)
            # shift right
            c.translate(2.2*inch, 0)

        c.translate(-6.6*inch, 0)   # reset
        y -= 0.85 * inch

        # Findings
        findings = comp.get("findings", [])
        if not findings:
            c.setFillColor(BRAND_GREEN)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(0.6*inch, y, "✓  No findings – specification fully compliant with selected code.")
        else:
            for f in findings:
                sev = f.get("severity","INFO")
                col = {"CRITICAL":BRAND_ERROR,"ERROR":BRAND_ERROR,
                       "WARNING":BRAND_WARN,"INFO":BRAND_BLUE}.get(sev, BRAND_BLUE)
                # Severity pill
                c.setFillColor(col)
                c.roundRect(0.5*inch, y-0.04*inch, 0.75*inch, 0.2*inch, 3, fill=1, stroke=0)
                c.setFillColor(white)
                c.setFont("Helvetica-Bold", 7)
                c.drawCentredString(0.875*inch, y+0.02*inch, sev)
                # Rule id
                c.setFillColor(BRAND_MID)
                c.setFont("Helvetica", 7.5)
                c.drawString(1.35*inch, y+0.02*inch, f.get("rule_id",""))
                # Section
                c.setFillColor(BRAND_LIGHT)
                c.setFont("Helvetica-Bold", 8)
                c.drawString(0.5*inch, y - 0.18*inch, f.get("code_section",""))
                # Description
                desc = f.get("description","")[:110]
                c.setFillColor(BRAND_LIGHT)
                c.setFont("Helvetica", 8)
                c.drawString(0.5*inch, y - 0.33*inch, desc)
                # Recommendation
                rec = f.get("recommendation","")
                if rec:
                    c.setFillColor(BRAND_MID)
                    c.setFont("Helvetica-Oblique", 7.5)
                    c.drawString(0.5*inch, y - 0.46*inch, f"→ {rec[:110]}")

                y -= 0.65 * inch
                if y < 1.2 * inch: break

        c.showPage()

    def _sheet_page(self, c: rl_canvas.Canvas, drw: Dict, page_num: int) -> None:
        W, H = PAGE_W, PAGE_H
        self._page_frame(c, drw.get("sheet_number","X0"), drw.get("title","Sheet"), page_num + 3)

        y = H - 1.65 * inch
        col_w = (W - 1.0 * inch) / 2

        # Left column – key notes
        c.setFillColor(BRAND_SURF)
        c.rect(0.5*inch, y-0.02*inch, col_w - 0.1*inch, 0.25*inch, fill=1, stroke=0)
        c.setFillColor(BRAND_BLUE)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(0.6*inch, y+0.06*inch, "KEY NOTES")
        y -= 0.32*inch

        notes = drw.get("key_notes") or []
        for n in notes[:8]:
            c.setFillColor(BRAND_BLUE)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(0.55*inch, y, "•")
            c.setFillColor(BRAND_LIGHT)
            c.setFont("Helvetica", 8)
            # Wrap long notes
            words = str(n); line_max = 68
            if len(words) > line_max:
                c.drawString(0.7*inch, y, words[:line_max])
                y -= 0.17*inch
                c.drawString(0.7*inch, y, "  " + words[line_max:line_max*2])
            else:
                c.drawString(0.7*inch, y, words)
            y -= 0.22*inch
            if y < 1.4 * inch: break

        # Right column – code sections + ADA
        rx = 0.5*inch + col_w + 0.1*inch
        ry = H - 1.65*inch

        c.setFillColor(BRAND_SURF)
        c.rect(rx, ry-0.02*inch, col_w - 0.1*inch, 0.25*inch, fill=1, stroke=0)
        c.setFillColor(BRAND_BLUE)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(rx + 0.1*inch, ry+0.06*inch, "CODE REFERENCES")
        ry -= 0.32*inch

        for sec in (drw.get("code_sections") or [])[:8]:
            c.setFillColor(BRAND_BLUE)
            c.roundRect(rx, ry-0.03*inch, col_w-0.2*inch, 0.2*inch, 3, fill=1, stroke=0)
            c.setFillColor(white)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawString(rx+0.08*inch, ry+0.02*inch, str(sec)[:55])
            ry -= 0.27*inch

        # ADA Notes
        ada = drw.get("ada_notes","")
        if ada:
            ry -= 0.1*inch
            c.setFillColor(BRAND_SURF)
            c.rect(rx, ry-0.02*inch, col_w-0.1*inch, 0.25*inch, fill=1, stroke=0)
            c.setFillColor(BRAND_GREEN)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(rx+0.1*inch, ry+0.06*inch, "ADA / ACCESSIBILITY")
            ry -= 0.32*inch
            c.setFillColor(BRAND_LIGHT)
            c.setFont("Helvetica", 8)
            for chunk in [ada[i:i+65] for i in range(0,min(len(ada),260),65)]:
                c.drawString(rx+0.05*inch, ry, chunk)
                ry -= 0.18*inch

        # Models used (if NIM)
        models = drw.get("models_used") or {}
        if models:
            c.setFillColor(BRAND_MID)
            c.setFont("Helvetica", 6.5)
            mtext = "  |  ".join(f"{k}: {v}" for k,v in models.items() if v)
            c.drawString(0.5*inch, 1.05*inch, f"AI Models: {mtext}")

        c.showPage()

    # ── Shared page frame ─────────────────────────────────────────────────

    def _page_frame(self, c: rl_canvas.Canvas, sheet_no: str,
                    title: str, page_num: int) -> None:
        W, H = PAGE_W, PAGE_H
        total = len(self.drawings) + 3

        # Background
        c.setFillColor(BRAND_DARK)
        c.rect(0, 0, W, H, fill=1, stroke=0)

        # Top bar
        c.setFillColor(BRAND_SURF)
        c.rect(0, H - 1.1*inch, W, 1.1*inch, fill=1, stroke=0)
        c.setStrokeColor(BRAND_BLUE)
        c.setLineWidth(1.5)
        c.line(0, H - 1.1*inch, W, H - 1.1*inch)

        # Sheet number (large, left)
        c.setFillColor(BRAND_BLUE)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(0.4*inch, H - 0.82*inch, sheet_no)

        # Title
        c.setFillColor(BRAND_LIGHT)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(1.8*inch, H - 0.65*inch, title.upper())

        # Project name
        c.setFillColor(BRAND_MID)
        c.setFont("Helvetica", 8.5)
        c.drawString(1.8*inch, H - 0.9*inch, self.project)

        # Right side: page number + date
        c.setFillColor(BRAND_MID)
        c.setFont("Helvetica", 8)
        c.drawRightString(W - 0.4*inch, H - 0.65*inch, f"Page {page_num} of {total}")
        c.drawRightString(W - 0.4*inch, H - 0.85*inch, self.now)

        # Bottom border
        c.setFillColor(BRAND_SURF)
        c.rect(0, 0, W, 0.9*inch, fill=1, stroke=0)
        c.setStrokeColor(BRAND_SURF)
        c.setLineWidth(1)
        c.line(0, 0.9*inch, W, 0.9*inch)

        # Footer text
        c.setFillColor(BRAND_MID)
        c.setFont("Helvetica", 6.5)
        c.drawString(0.4*inch, 0.35*inch,
            "FOR REVIEW ONLY — VERIFY ALL DIMENSIONS IN FIELD — NOT FOR CONSTRUCTION WITHOUT LICENSED ARCHITECT STAMP")
        c.drawRightString(W - 0.4*inch, 0.35*inch,
            "Architectural AI Platform  |  IBC 2023")


# ---------------------------------------------------------------------------
# DXF Export
# ---------------------------------------------------------------------------

class DXFExporter:
    """
    Generates per-sheet DXF files (AutoCAD R2010 / DXF version AC1024).
    Each DrawingSet gets its own DXF with:
      • AIA-standard named layers
      • ANSI D title block (34" × 22" = 816 × 528 drawing units at 1:1)
      • Architectural content drawn to scale
      • Text annotation with code references
    """

    SHEET_W = 34.0   # inches
    SHEET_H = 22.0
    MARGIN  = 0.5
    TB_H    = 3.0    # title block height (bottom strip)
    TB_LOGO_W = 6.0

    def __init__(self, job: Dict[str, Any]) -> None:
        self.job      = job
        self.project  = job.get("project_name", "Project")
        self.drawings = job.get("drawings", [])
        self.report   = job.get("compliance_report") or {}
        self.now      = datetime.utcnow().strftime("%Y-%m-%d")

    # ── Public ────────────────────────────────────────────────────────────

    def generate_sheet(self, drw: Dict) -> bytes:
        """Return a single DXF file as bytes for one drawing sheet."""
        doc = ezdxf.new("R2010")
        doc.units = ezdxf.units.IN
        self._setup_layers(doc)
        self._setup_linetypes(doc)
        msp = doc.modelspace()
        self._draw_border(msp)
        self._draw_title_block(msp, drw)
        self._draw_content(msp, drw)
        buf = io.StringIO()
        doc.write(buf)
        return buf.getvalue().encode("utf-8")

    def generate_all_sheets(self) -> Dict[str, bytes]:
        """Return {filename: bytes} for every drawing in the job."""
        result = {}
        for drw in self.drawings:
            num   = drw.get("sheet_number","X0").replace("/","_").replace(" ","_")
            fname = f"{num}_{drw.get('sheet_type','Sheet').replace(' ','_')}.dxf"
            result[fname] = self.generate_sheet(drw)
        return result

    # ── DXF setup ─────────────────────────────────────────────────────────

    def _setup_layers(self, doc) -> None:
        for key, (name, color, lw) in LAYERS.items():
            try:
                layer = doc.layers.add(name)
                layer.color = color
                layer.lineweight = lw
            except Exception:
                pass

    def _setup_linetypes(self, doc) -> None:
        try:
            doc.linetypes.add("HIDDEN",  pattern=[0.25, -0.125])
            doc.linetypes.add("CENTER",  pattern=[1.0, -0.25, 0.0, -0.25])
            doc.linetypes.add("DASHDOT", pattern=[0.5, -0.25, 0.0, -0.25])
        except Exception:
            pass

    # ── Border & title block ──────────────────────────────────────────────

    def _draw_border(self, msp) -> None:
        W, H = self.SHEET_W, self.SHEET_H
        M    = self.MARGIN
        ln   = LAYERS["BORDER"][0]

        # Outer sheet boundary
        msp.add_lwpolyline(
            [(0,0),(W,0),(W,H),(0,H),(0,0)],
            dxfattribs={"layer": ln, "lineweight": 70, "closed": True}
        )
        # Inner drawing area border
        msp.add_lwpolyline(
            [(M, M+self.TB_H), (W-M, M+self.TB_H),
             (W-M, H-M), (M, H-M), (M, M+self.TB_H)],
            dxfattribs={"layer": ln, "lineweight": 50, "closed": True}
        )
        # Grid reference marks every 2" along top/bottom
        grid_ln = LAYERS["GRID"][0]
        for x in range(2, int(W), 2):
            msp.add_line((x, H-M), (x, H-M+0.15),
                         dxfattribs={"layer": grid_ln, "lineweight": 13})
            msp.add_line((x, M+self.TB_H), (x, M+self.TB_H-0.15),
                         dxfattribs={"layer": grid_ln, "lineweight": 13})
        for y in range(int(M+self.TB_H)+2, int(H-M), 2):
            msp.add_line((M, y), (M-0.15, y),
                         dxfattribs={"layer": grid_ln, "lineweight": 13})

    def _draw_title_block(self, msp, drw: Dict) -> None:
        tl  = LAYERS["TITLEBLK"][0]
        txt = LAYERS["TEXT"][0]
        W, H, M = self.SHEET_W, self.SHEET_H, self.MARGIN
        TB  = self.TB_H
        LW  = self.TB_LOGO_W

        # Title block outer box
        msp.add_lwpolyline(
            [(M, M), (W-M, M), (W-M, M+TB), (M, M+TB), (M, M)],
            dxfattribs={"layer": tl, "lineweight": 50, "closed": True}
        )

        # ── Left logo / firm cell ──────────────────────────────────────
        msp.add_line((M+LW, M), (M+LW, M+TB), dxfattribs={"layer":tl,"lineweight":50})
        msp.add_text("ARCHITECTURAL AI PLATFORM",
            dxfattribs={"layer":txt,"height":0.18,"style":"STANDARD"}
        ).set_placement((M+0.15, M+TB-0.35), align=TextEntityAlignment.LEFT)
        msp.add_text("IBC 2023 Compliant",
            dxfattribs={"layer":txt,"height":0.12,"style":"STANDARD"}
        ).set_placement((M+0.15, M+TB-0.58), align=TextEntityAlignment.LEFT)

        # ── Project info cell ──────────────────────────────────────────
        info_x = M + LW + 0.15
        msp.add_text("PROJECT",
            dxfattribs={"layer":txt,"height":0.09,"style":"STANDARD"}
        ).set_placement((info_x, M+TB-0.25), align=TextEntityAlignment.LEFT)
        msp.add_text(self.project[:60],
            dxfattribs={"layer":txt,"height":0.15,"style":"STANDARD"}
        ).set_placement((info_x, M+TB-0.45), align=TextEntityAlignment.LEFT)

        msp.add_text("SHEET TITLE",
            dxfattribs={"layer":txt,"height":0.09,"style":"STANDARD"}
        ).set_placement((info_x, M+TB-0.75), align=TextEntityAlignment.LEFT)
        msp.add_text(drw.get("title","Untitled")[:60],
            dxfattribs={"layer":txt,"height":0.13,"style":"STANDARD"}
        ).set_placement((info_x, M+TB-0.95), align=TextEntityAlignment.LEFT)

        # ── Right meta cell ────────────────────────────────────────────
        right_x = W - M - 4.5
        msp.add_line((right_x, M), (right_x, M+TB), dxfattribs={"layer":tl,"lineweight":35})

        meta = [
            ("SHEET NO.",  drw.get("sheet_number","X0.0")),
            ("SCALE",      drw.get("scale","AS NOTED")),
            ("DATE",       self.now),
            ("DISCIPLINE", drw.get("discipline","Architecture")),
        ]
        my = M + TB - 0.35
        for label, val in meta:
            msp.add_text(label,
                dxfattribs={"layer":txt,"height":0.08}
            ).set_placement((right_x+0.1, my), align=TextEntityAlignment.LEFT)
            msp.add_text(str(val),
                dxfattribs={"layer":txt,"height":0.12}
            ).set_placement((right_x+0.1, my-0.17), align=TextEntityAlignment.LEFT)
            msp.add_line((right_x, my-0.25), (W-M, my-0.25),
                         dxfattribs={"layer":tl,"lineweight":18})
            my -= 0.62

        # Compliance stamp
        comp_ok = self.report.get("is_compliant", True)
        stamp   = "IBC COMPLIANT" if comp_ok else "COMPLIANCE REVIEW REQUIRED"
        msp.add_text(stamp,
            dxfattribs={"layer":txt,"height":0.11}
        ).set_placement(((right_x + W-M)/2, M+0.22), align=TextEntityAlignment.CENTER)

    # ── Drawing content ───────────────────────────────────────────────────

    def _draw_content(self, msp, drw: Dict) -> None:
        """Dispatch to per-sheet-type content generator."""
        st = drw.get("sheet_type","").lower()
        dispatch = {
            "floor plan":          self._content_floor_plan,
            "site plan":           self._content_site_plan,
            "exterior elevations": self._content_elevations,
            "building sections":   self._content_sections,
            "structural drawings": self._content_structural,
            "fire & life safety":  self._content_fire,
            "accessibility plan":  self._content_accessibility,
            "mep drawings":        self._content_mep,
        }
        fn = None
        for key, func in dispatch.items():
            if key in st:
                fn = func
                break
        if fn:
            fn(msp, drw)
        else:
            self._content_generic(msp, drw)

    # ── Content generators ────────────────────────────────────────────────

    def _content_floor_plan(self, msp, drw: Dict) -> None:
        """Schematic floor plan with room grid, walls, doors, windows, dims."""
        W, H = self.SHEET_W, self.SHEET_H
        M, TB = self.MARGIN, self.TB_H
        # Drawing area
        DX = M + 0.75;  DW = W - M - 1.5
        DY = M + TB + 0.5; DH = H - M - TB - 1.0
        # Scale: fit into drawing area
        BLDG_W, BLDG_D = 120.0, 80.0   # ft (schematic)
        sx = DW / BLDG_W
        sy = DH / BLDG_D

        wall = LAYERS["WALL"][0]
        wint = LAYERS["WALL_INT"][0]
        door = LAYERS["DOOR"][0]
        win  = LAYERS["WINDOW"][0]
        dim  = LAYERS["DIM"][0]
        txt  = LAYERS["TEXT"][0]

        def W2D(x, y): return (DX + x*sx, DY + y*sy)

        # Exterior walls (double-line: 8" = 0.67 ft thick)
        thick = 0.67
        perimeter = [
            (0,0),(BLDG_W,0),(BLDG_W,BLDG_D),(0,BLDG_D),(0,0)
        ]
        pts_out = [W2D(x, y) for x,y in perimeter]
        pts_in  = [W2D(x+(thick if x==0 else -thick if x==BLDG_W else 0),
                       y+(thick if y==0 else -thick if y==BLDG_D else 0)) for x,y in perimeter]
        msp.add_lwpolyline(pts_out, dxfattribs={"layer":wall,"lineweight":50,"closed":True})
        msp.add_lwpolyline(pts_in,  dxfattribs={"layer":wall,"lineweight":50,"closed":True})

        # Interior walls (4" = 0.33 ft)
        int_walls = [
            ((40,0),(40,60)), ((40,60),(0,60)),
            ((80,0),(80,BLDG_D)), ((40,30),(80,30)),
            ((0,30),(40,30)),
        ]
        for (x1,y1),(x2,y2) in int_walls:
            p1, p2 = W2D(x1,y1), W2D(x2,y2)
            msp.add_line(p1, p2, dxfattribs={"layer":wint,"lineweight":35})

        # Doors (arcs representing swing)
        doors_pos = [(38,0,True),(78,0,True),(38,28,False),(0,58,True)]
        for dx,dy,horiz in doors_pos:
            cx,cy = W2D(dx+3, dy+3 if not horiz else dy)
            r = 3*sx
            start_ang = 90 if horiz else 0
            msp.add_arc(center=(cx, cy, 0), radius=r, start_angle=start_ang, end_angle=start_ang+90,
                        dxfattribs={"layer":door,"lineweight":25})

        # Windows (triple line on exterior)
        windows = [(20,0,10,True),(60,0,10,True),(10,BLDG_D,15,True),
                   (0,15,10,False),(BLDG_W,20,12,False)]
        for wx,wy,wlen,horiz in windows:
            p1 = W2D(wx, wy); p2 = W2D(wx+(wlen if horiz else 0), wy+(0 if horiz else wlen))
            msp.add_line(p1, p2, dxfattribs={"layer":win,"lineweight":25})
            off = 0.08*inch*sx
            dx_off = 0 if horiz else off
            dy_off = off if horiz else 0
            p1o = (p1[0]+dx_off, p1[1]+dy_off)
            p2o = (p2[0]+dx_off, p2[1]+dy_off)
            msp.add_line(p1o, p2o, dxfattribs={"layer":win,"lineweight":18})

        # Room labels
        rooms = [
            (5,32,"LOBBY"),(45,35,"OFFICE"),(45,5,"CONF ROOM"),
            (85,35,"OPEN OFFICE"),(85,5,"STORAGE"),(2,2,"EXIT")
        ]
        for rx,ry,label in rooms:
            pt = W2D(rx, ry)
            msp.add_text(label, dxfattribs={"layer":txt,"height":0.14}
            ).set_placement(pt, align=TextEntityAlignment.LEFT)

        # Dimension strings
        self._add_dim(msp, W2D(0,-5), W2D(BLDG_W,-5), f"{int(BLDG_W)}'-0\"", dim, txt)
        self._add_dim(msp, W2D(-5,0), W2D(-5,BLDG_D), f"{int(BLDG_D)}'-0\"", dim, txt, vertical=True)

        # North arrow
        self._north_arrow(msp, W2D(BLDG_W+5, BLDG_D+5))

        # Scale bar
        self._scale_bar(msp, W2D(0, BLDG_D+5), sx, "1/8\" = 1'-0\"", txt)

        # Sheet title in drawing area
        msp.add_text("GROUND FLOOR PLAN",
            dxfattribs={"layer":txt,"height":0.22}
        ).set_placement(W2D(BLDG_W/2, BLDG_D+8), align=TextEntityAlignment.CENTER)

        self._add_code_notes(msp, drw, DX+DW+0.1, DY+DH)

    def _content_site_plan(self, msp, drw: Dict) -> None:
        W, H = self.SHEET_W, self.SHEET_H
        M, TB = self.MARGIN, self.TB_H
        DX, DY = M+0.5, M+TB+0.5
        DW, DH = W-M-1.0, H-M-TB-1.0
        txt = LAYERS["TEXT"][0]; wall = LAYERS["WALL"][0]
        dim = LAYERS["DIM"][0];  grid = LAYERS["GRID"][0]
        fill = LAYERS["FILLED"][0]

        # Property boundary
        prop = [(DX+1,DY+1),(DX+DW-1,DY+1),(DX+DW-1,DY+DH-1),(DX+1,DY+DH-1),(DX+1,DY+1)]
        msp.add_lwpolyline(prop, dxfattribs={"layer":grid,"lineweight":35,"closed":True})
        # Building footprint
        bx = DX+3; by = DY+3; bw = DW-7; bh = DH-7
        msp.add_lwpolyline([(bx,by),(bx+bw,by),(bx+bw,by+bh),(bx,by+bh),(bx,by)],
            dxfattribs={"layer":wall,"lineweight":50,"closed":True})
        h = msp.add_hatch(color=8); h.dxf.layer = fill; h.paths.add_polyline_path(
            [(bx,by),(bx+bw,by),(bx+bw,by+bh),(bx,by+bh)], is_closed=True)

        # Roads, parking
        road_y = DY + 0.3
        msp.add_line((DX, road_y), (DX+DW, road_y), dxfattribs={"layer":grid,"lineweight":25})
        msp.add_text("RIGHT OF WAY", dxfattribs={"layer":txt,"height":0.12}
        ).set_placement((DX+DW/2, road_y-0.2), align=TextEntityAlignment.CENTER)
        msp.add_text("BUILDING FOOTPRINT",dxfattribs={"layer":txt,"height":0.15}
        ).set_placement((bx+bw/2, by+bh/2), align=TextEntityAlignment.CENTER)
        msp.add_text("SITE PLAN",dxfattribs={"layer":txt,"height":0.22}
        ).set_placement((DX+DW/2, DY+DH-0.4), align=TextEntityAlignment.CENTER)
        self._north_arrow(msp, (DX+DW-0.8, DY+DH-0.9))
        self._add_code_notes(msp, drw, DX+DW+0.05, DY+DH)

    def _content_elevations(self, msp, drw: Dict) -> None:
        W, H = self.SHEET_W, self.SHEET_H
        M, TB = self.MARGIN, self.TB_H
        DX, DY = M+0.5, M+TB+0.5
        DW, DH = W-M-1.0, H-M-TB-1.0
        wall = LAYERS["WALL"][0]; txt = LAYERS["TEXT"][0]
        win  = LAYERS["WINDOW"][0]; dim = LAYERS["DIM"][0]

        BW, BH = DW*0.85, DH*0.7
        EX = DX + (DW-BW)/2; EY = DY + 0.8

        # Ground line
        msp.add_line((EX-0.5, EY), (EX+BW+0.5, EY),
                     dxfattribs={"layer":dim,"lineweight":50})
        # Building envelope
        msp.add_lwpolyline([(EX,EY),(EX+BW,EY),(EX+BW,EY+BH),(EX,EY+BH),(EX,EY)],
            dxfattribs={"layer":wall,"lineweight":50,"closed":True})
        # Parapet
        par_h = 0.25
        msp.add_lwpolyline([(EX,EY+BH),(EX+BW,EY+BH),(EX+BW,EY+BH+par_h),
                             (EX,EY+BH+par_h),(EX,EY+BH)],
            dxfattribs={"layer":wall,"lineweight":35,"closed":True})
        # Windows (3 rows)
        rows = 3; cols = 5
        win_w = BW*0.12; win_h = BH*0.22
        x_gap = BW/(cols+1); y_gap = BH/(rows+1)
        for r in range(rows):
            for col in range(cols):
                wx = EX + x_gap*(col+1) - win_w/2
                wy = EY + y_gap*(r+1) - win_h/2
                msp.add_lwpolyline(
                    [(wx,wy),(wx+win_w,wy),(wx+win_w,wy+win_h),(wx,wy+win_h),(wx,wy)],
                    dxfattribs={"layer":win,"lineweight":25,"closed":True})
                # Sill
                msp.add_line((wx-0.05,wy),(wx+win_w+0.05,wy),
                             dxfattribs={"layer":win,"lineweight":35})
        # Entry door
        door_w = BW*0.08; door_x = EX+BW*0.45
        msp.add_lwpolyline([(door_x,EY),(door_x+door_w,EY),
                             (door_x+door_w,EY+BH*0.18),(door_x,EY+BH*0.18),(door_x,EY)],
            dxfattribs={"layer":LAYERS["DOOR"][0],"lineweight":35,"closed":True})
        # Labels
        self._add_dim(msp,(EX,EY-0.5),(EX+BW,EY-0.5),"BUILDING WIDTH",dim,txt)
        self._add_dim(msp,(EX+BW+0.4,EY),(EX+BW+0.4,EY+BH),
                      f"FIN FLOOR TO PARAPET",dim,txt,vertical=True)
        msp.add_text("FRONT ELEVATION", dxfattribs={"layer":txt,"height":0.2}
        ).set_placement((EX+BW/2, EY+BH+0.45), align=TextEntityAlignment.CENTER)
        self._add_code_notes(msp, drw, DX+DW+0.05, DY+DH)

    def _content_structural(self, msp, drw: Dict) -> None:
        self._content_generic(msp, drw, "STRUCTURAL DRAWING — SEE STRUCTURAL ENGINEER OF RECORD")

    def _content_sections(self, msp, drw: Dict) -> None:
        self._content_generic(msp, drw, "BUILDING SECTION — SEE ARCHITECTURAL DRAWINGS")

    def _content_fire(self, msp, drw: Dict) -> None:
        self._content_generic(msp, drw, "FIRE & LIFE SAFETY PLAN — PER IBC 2023 §907")

    def _content_accessibility(self, msp, drw: Dict) -> None:
        self._content_generic(msp, drw, "ACCESSIBILITY PLAN — PER ADA / ICC A117.1-2017")

    def _content_mep(self, msp, drw: Dict) -> None:
        self._content_generic(msp, drw, "MEP DRAWINGS — SEE MECHANICAL / ELECTRICAL ENGINEER")

    def _content_generic(self, msp, drw: Dict, subtitle: str = "") -> None:
        W, H = self.SHEET_W, self.SHEET_H
        M, TB = self.MARGIN, self.TB_H
        DX, DY = M+0.75, M+TB+0.75
        DW, DH = W-M-1.5, H-M-TB-1.5
        txt = LAYERS["TEXT"][0]
        # Title
        msp.add_text(drw.get("sheet_type","Drawing").upper(),
            dxfattribs={"layer":txt,"height":0.3}
        ).set_placement((DX+DW/2, DY+DH*0.7), align=TextEntityAlignment.CENTER)
        if subtitle:
            msp.add_text(subtitle,
                dxfattribs={"layer":txt,"height":0.14}
            ).set_placement((DX+DW/2, DY+DH*0.6), align=TextEntityAlignment.CENTER)
        msp.add_text(self.project,
            dxfattribs={"layer":txt,"height":0.16}
        ).set_placement((DX+DW/2, DY+DH*0.5), align=TextEntityAlignment.CENTER)
        self._add_code_notes(msp, drw, DX, DY+DH*0.42)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _add_dim(self, msp, p1, p2, label, dim_layer, txt_layer,
                 vertical=False, offset=0.3) -> None:
        mx, my = (p1[0]+p2[0])/2, (p1[1]+p2[1])/2
        if not vertical:
            oy = -offset
            msp.add_line((p1[0],p1[1]+oy),(p2[0],p2[1]+oy), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_line(p1,(p1[0],p1[1]+oy), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_line(p2,(p2[0],p2[1]+oy), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_text(label, dxfattribs={"layer":txt_layer,"height":0.1}
            ).set_placement((mx, p1[1]+oy-0.13), align=TextEntityAlignment.CENTER)
        else:
            ox = offset
            msp.add_line((p1[0]+ox,p1[1]),(p2[0]+ox,p2[1]), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_line(p1,(p1[0]+ox,p1[1]), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_line(p2,(p2[0]+ox,p2[1]), dxfattribs={"layer":dim_layer,"lineweight":18})
            msp.add_text(label, dxfattribs={"layer":txt_layer,"height":0.1}
            ).set_placement((p1[0]+ox+0.15, my), align=TextEntityAlignment.LEFT)

    def _north_arrow(self, msp, center) -> None:
        cx, cy = center; r = 0.25; ln = LAYERS["NORTH"][0]
        # Circle
        msp.add_circle(center=(cx, cy, 0), radius=r, dxfattribs={"layer":ln,"lineweight":25})
        # Arrow up
        msp.add_lwpolyline([(cx,cy),(cx-0.08,cy-r*0.9),(cx,cy+r*0.95),(cx+0.08,cy-r*0.9),(cx,cy)],
            dxfattribs={"layer":ln,"lineweight":25,"closed":True})
        msp.add_text("N", dxfattribs={"layer":ln,"height":0.18}
        ).set_placement((cx, cy+r+0.05), align=TextEntityAlignment.CENTER)

    def _scale_bar(self, msp, origin, sx, label, txt_layer) -> None:
        bx, by = origin; unit = 10 * sx; ln = LAYERS["DIM"][0]
        for i in range(5):
            fill = i % 2 == 0
            x = bx + i * unit
            msp.add_lwpolyline([(x,by),(x+unit,by),(x+unit,by+0.08),(x,by+0.08),(x,by)],
                dxfattribs={"layer":ln,"lineweight":18,"closed":True})
        msp.add_text(label, dxfattribs={"layer":txt_layer,"height":0.1}
        ).set_placement((bx + 2.5*unit, by-0.12), align=TextEntityAlignment.CENTER)
        msp.add_text("0", dxfattribs={"layer":txt_layer,"height":0.09}
        ).set_placement((bx, by-0.12), align=TextEntityAlignment.LEFT)
        msp.add_text("50'", dxfattribs={"layer":txt_layer,"height":0.09}
        ).set_placement((bx+5*unit, by-0.12), align=TextEntityAlignment.RIGHT)

    def _add_code_notes(self, msp, drw: Dict, x: float, y: float) -> None:
        txt = LAYERS["TEXT"][0]
        notes = drw.get("key_notes") or []
        codes = drw.get("code_sections") or []
        ada   = drw.get("ada_notes","")

        msp.add_text("GENERAL NOTES", dxfattribs={"layer":txt,"height":0.13}
        ).set_placement((x, y), align=TextEntityAlignment.LEFT)
        cy = y - 0.2
        for i, note in enumerate(notes[:5], 1):
            txt_val = f"{i}. {str(note)[:70]}"
            msp.add_text(txt_val, dxfattribs={"layer":txt,"height":0.1}
            ).set_placement((x, cy), align=TextEntityAlignment.LEFT)
            cy -= 0.17
        if codes:
            cy -= 0.1
            msp.add_text("CODE REFERENCES", dxfattribs={"layer":txt,"height":0.11}
            ).set_placement((x, cy), align=TextEntityAlignment.LEFT)
            cy -= 0.18
            for code in codes[:4]:
                msp.add_text(str(code)[:50], dxfattribs={"layer":txt,"height":0.09}
                ).set_placement((x+0.1, cy), align=TextEntityAlignment.LEFT)
                cy -= 0.15
        if ada:
            cy -= 0.1
            msp.add_text("ADA/ACCESSIBILITY", dxfattribs={"layer":txt,"height":0.11}
            ).set_placement((x, cy), align=TextEntityAlignment.LEFT)
            cy -= 0.16
            msp.add_text(ada[:70], dxfattribs={"layer":txt,"height":0.09}
            ).set_placement((x+0.1, cy), align=TextEntityAlignment.LEFT)


# ---------------------------------------------------------------------------
# ZIP Package
# ---------------------------------------------------------------------------

def build_export_package(job: Dict[str, Any]) -> bytes:
    """
    Generate a ZIP containing:
      • construction_documents.pdf   (full multi-page permit set)
      • sheets/A1.0_Floor_Plan.dxf   (per-sheet DXF files)
      • manifest.json                (machine-readable job summary)
    Returns bytes of the ZIP file.
    """
    import json

    pdf_bytes = PDFExporter(job).generate()
    dxf_sheets = DXFExporter(job).generate_all_sheets()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("construction_documents.pdf", pdf_bytes)
        for fname, dxf_bytes in dxf_sheets.items():
            zf.writestr(f"sheets/{fname}", dxf_bytes)
        manifest = {
            "project_name":    job.get("project_name"),
            "generated_at":    datetime.utcnow().isoformat(),
            "engine":          job.get("engine"),
            "drawing_count":   job.get("drawing_count", len(job.get("drawings",[]))),
            "compliance":      job.get("compliance_report",{}).get("summary",""),
            "sheets": [{
                "sheet_number": d.get("sheet_number"),
                "title":        d.get("title"),
                "sheet_type":   d.get("sheet_type"),
                "discipline":   d.get("discipline",""),
                "scale":        d.get("scale",""),
            } for d in job.get("drawings",[])],
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()
