"""
main.py
=======
Entry Point – Architectural AI Platform (tkinter GUI)

Provides a structured text-based GUI using only Python's standard library
(tkinter). No external UI frameworks or styling libraries are required.

Layout:
  Tab 1 – Project Configuration  : project metadata, building type, jurisdiction
  Tab 2 – Compliance Settings    : code selection, drawing sets, site parameters
  Tab 3 – Engine & Dispatch      : engine selection, generation trigger
  Tab 4 – Results                : compliance report + drawing manifest output
"""

from __future__ import annotations

import logging
import sys
try:
    import tkinter as tk
    import tkinter.font as tkfont
    import tkinter.messagebox as messagebox
    import tkinter.scrolledtext as scrolledtext
    from tkinter import ttk
    _HAS_TK = True
except ModuleNotFoundError:
    _HAS_TK = False
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import Any, Dict, List, Optional

# ── Platform imports ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from compliance import ComplianceEngine, JurisdictionLoader
from models import (
    BuildingType,
    CodeVersion,
    ConstructionType,
    DrawingSet,
    EngineProvider,
    GenerationJob,
    JobStatus,
    OccupancyGroup,
    ProjectSpecification,
)
from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("arch_platform.main")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_TITLE  = "Architectural AI Platform – IBC 2023 Compliant Drawing Generator"
APP_WIDTH  = 960
APP_HEIGHT = 750
PAD        = 10
ENTRY_W    = 36

OCCUPANCY_MAP: Dict[BuildingType, List[OccupancyGroup]] = {
    BuildingType.COMMERCIAL: [
        OccupancyGroup.A1, OccupancyGroup.A2, OccupancyGroup.A3,
        OccupancyGroup.A4, OccupancyGroup.A5,
        OccupancyGroup.B, OccupancyGroup.E,
        OccupancyGroup.F1, OccupancyGroup.F2,
        OccupancyGroup.H1, OccupancyGroup.H2, OccupancyGroup.H3,
        OccupancyGroup.H4, OccupancyGroup.H5,
        OccupancyGroup.I1, OccupancyGroup.I2, OccupancyGroup.I3,
        OccupancyGroup.I4,
        OccupancyGroup.M,
        OccupancyGroup.S1, OccupancyGroup.S2, OccupancyGroup.U,
    ],
    BuildingType.RESIDENTIAL: [
        OccupancyGroup.R1, OccupancyGroup.R2,
        OccupancyGroup.R3, OccupancyGroup.R4,
    ],
}


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

def _labeled_entry(
    parent: tk.Widget, label_text: str, row: int, *, col: int = 0, width: int = ENTRY_W
) -> tk.StringVar:
    tk.Label(parent, text=label_text, anchor="w").grid(
        row=row, column=col, padx=PAD, pady=4, sticky="w"
    )
    var = tk.StringVar()
    tk.Entry(parent, textvariable=var, width=width).grid(
        row=row, column=col + 1, padx=PAD, pady=4, sticky="w"
    )
    return var


def _labeled_combo(
    parent: tk.Widget, label_text: str, row: int, values: List[str], *,
    col: int = 0, width: int = ENTRY_W, state: str = "readonly"
) -> ttk.Combobox:
    tk.Label(parent, text=label_text, anchor="w").grid(
        row=row, column=col, padx=PAD, pady=4, sticky="w"
    )
    combo = ttk.Combobox(parent, values=values, width=width - 3, state=state)
    combo.grid(row=row, column=col + 1, padx=PAD, pady=4, sticky="w")
    if values:
        combo.current(0)
    return combo


def _section_header(parent: tk.Widget, text: str, row: int, col: int = 0,
                    colspan: int = 4) -> None:
    lbl = tk.Label(
        parent, text=text, anchor="w",
        font=("TkDefaultFont", 9, "bold"),
        bg="#dce8f0", relief="flat", padx=6, pady=2,
    )
    lbl.grid(row=row, column=col, columnspan=colspan,
             sticky="ew", padx=PAD, pady=(10, 2))


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class ArchPlatformApp(tk.Tk):
    """Root window for the Architectural AI Platform."""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        self.resizable(True, True)
        self.minsize(820, 640)

        # Runtime state
        self._last_job: Optional[GenerationJob] = None
        self._dispatcher = EngineDispatcher()
        EngineRegistry.register(MockDrawingEngine())

        self._build_ui()
        logger.info("Architectural AI Platform initialised.")

    # ── UI Construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Title banner
        banner = tk.Frame(self, bg="#1a3a5c", height=44)
        banner.pack(fill="x", side="top")
        banner.pack_propagate(False)
        tk.Label(
            banner, text=APP_TITLE,
            bg="#1a3a5c", fg="white",
            font=("TkDefaultFont", 10, "bold"), anchor="w", padx=PAD,
        ).pack(side="left", fill="y")

        tk.Label(
            banner, text="IBC 2023 Compliant",
            bg="#1a3a5c", fg="#90c8e8",
            font=("TkDefaultFont", 9), padx=PAD,
        ).pack(side="right", fill="y")

        # Notebook
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._tab_project    = tk.Frame(nb)
        self._tab_compliance = tk.Frame(nb)
        self._tab_engine     = tk.Frame(nb)
        self._tab_results    = tk.Frame(nb)

        nb.add(self._tab_project,    text="  1. Project Configuration  ")
        nb.add(self._tab_compliance, text="  2. Code & Drawing Sets  ")
        nb.add(self._tab_engine,     text="  3. Engine & Dispatch  ")
        nb.add(self._tab_results,    text="  4. Results  ")

        self._build_tab_project()
        self._build_tab_compliance()
        self._build_tab_engine()
        self._build_tab_results()

        # Status bar
        self._status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(
            self, textvariable=self._status_var,
            bd=1, relief="sunken", anchor="w", padx=PAD,
            font=("TkFixedFont", 8),
        )
        status_bar.pack(side="bottom", fill="x")

    # ── Tab 1: Project Configuration ─────────────────────────────────────

    def _build_tab_project(self) -> None:
        f = self._tab_project
        f.columnconfigure(1, weight=1)

        _section_header(f, "Project Identification", row=0)

        self._v_project_name = _labeled_entry(f, "Project Name *", 1)
        self._v_project_name.set("My Architectural Project")

        _section_header(f, "Building Classification", row=2)

        # Building Type
        tk.Label(f, text="Building Type *", anchor="w").grid(
            row=3, column=0, padx=PAD, pady=4, sticky="w"
        )
        self._v_building_type = tk.StringVar(value=BuildingType.COMMERCIAL.value)
        type_frame = tk.Frame(f)
        type_frame.grid(row=3, column=1, padx=PAD, pady=4, sticky="w")
        for bt in BuildingType:
            tk.Radiobutton(
                type_frame, text=bt.value, variable=self._v_building_type,
                value=bt.value, command=self._on_building_type_change,
            ).pack(side="left", padx=8)

        # Occupancy Group
        self._cb_occupancy = _labeled_combo(
            f, "Occupancy Group *", 4,
            [og.value for og in OCCUPANCY_MAP[BuildingType.COMMERCIAL]],
        )

        # Construction Type
        self._cb_construction = _labeled_combo(
            f, "Construction Type *", 5,
            [ct.value for ct in ConstructionType],
        )
        self._cb_construction.set(ConstructionType.IIA.value)

        _section_header(f, "Jurisdiction", row=6)

        preset_names = JurisdictionLoader.available_jurisdictions()
        self._cb_jurisdiction = _labeled_combo(
            f, "Jurisdiction Preset *", 7, preset_names, width=44,
        )
        self._cb_jurisdiction.bind("<<ComboboxSelected>>", self._on_jurisdiction_change)

        # Jurisdiction details (read-only info panel)
        tk.Label(f, text="Jurisdiction Details", anchor="w").grid(
            row=8, column=0, padx=PAD, pady=4, sticky="nw"
        )
        self._txt_jur_info = scrolledtext.ScrolledText(
            f, height=5, width=60, state="disabled",
            font=("TkFixedFont", 8),
        )
        self._txt_jur_info.grid(row=8, column=1, padx=PAD, pady=4, sticky="w")

        _section_header(f, "Dimensional Parameters (Optional)", row=9)

        self._v_gross_sqft   = _labeled_entry(f, "Gross Area (sq ft)", 10)
        self._v_num_stories  = _labeled_entry(f, "Number of Stories", 11)
        self._v_height_ft    = _labeled_entry(f, "Building Height (ft)", 12)
        self._v_occ_load     = _labeled_entry(f, "Occupant Load", 13)

        tk.Label(f, text="Fire Suppression", anchor="w").grid(
            row=14, column=0, padx=PAD, pady=4, sticky="w"
        )
        self._v_sprinklered = tk.BooleanVar(value=False)
        tk.Checkbutton(
            f, text="Fully Sprinklered (NFPA 13)",
            variable=self._v_sprinklered,
        ).grid(row=14, column=1, padx=PAD, pady=4, sticky="w")

        self._on_jurisdiction_change()  # populate info panel on load

    # ── Tab 2: Code & Drawing Sets ────────────────────────────────────────

    def _build_tab_compliance(self) -> None:
        f = self._tab_compliance
        f.columnconfigure(1, weight=1)

        _section_header(f, "Primary Building Code", row=0)

        self._cb_primary_code = _labeled_combo(
            f, "Primary Code *", 1,
            [cv.value for cv in CodeVersion], width=36,
        )
        self._cb_primary_code.set(CodeVersion.IBC_2023.value)

        _section_header(f, "Accessibility Standard", row=2)

        self._v_access_std = _labeled_entry(
            f, "Accessibility Standard", 3, width=44,
        )
        self._v_access_std.set("ADA / ICC A117.1-2017")

        _section_header(f, "Requested Drawing Sets *", row=4)

        tk.Label(
            f, text="Select all drawing sheets required for this submission:",
            anchor="w", font=("TkDefaultFont", 8, "italic"),
        ).grid(row=5, column=0, columnspan=3, padx=PAD, pady=2, sticky="w")

        self._drawing_vars: Dict[DrawingSet, tk.BooleanVar] = {}
        drawing_frame = tk.Frame(f)
        drawing_frame.grid(row=6, column=0, columnspan=3, padx=PAD, pady=4, sticky="w")

        col = 0
        row = 0
        for i, ds in enumerate(DrawingSet):
            var = tk.BooleanVar(value=False)
            self._drawing_vars[ds] = var
            tk.Checkbutton(drawing_frame, text=ds.value, variable=var).grid(
                row=row, column=col, sticky="w", padx=6, pady=2
            )
            col += 1
            if col >= 3:
                col = 0
                row += 1

        # Pre-select common defaults
        self._drawing_vars[DrawingSet.FLOOR_PLAN].set(True)
        self._drawing_vars[DrawingSet.ELEVATIONS].set(True)
        self._drawing_vars[DrawingSet.SITE].set(True)

        # Quick selection buttons
        btn_frame = tk.Frame(f)
        btn_frame.grid(row=7, column=0, columnspan=3, padx=PAD, pady=8, sticky="w")
        tk.Button(
            btn_frame, text="Select All", width=14,
            command=lambda: self._select_all_drawings(True),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Clear All", width=14,
            command=lambda: self._select_all_drawings(False),
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Full Construction Set", width=20,
            command=self._select_full_set,
        ).pack(side="left", padx=4)

        _section_header(f, "Additional Notes", row=8)

        self._txt_notes = scrolledtext.ScrolledText(f, height=4, width=70)
        self._txt_notes.grid(row=9, column=0, columnspan=3, padx=PAD, pady=4, sticky="ew")
        f.rowconfigure(9, weight=1)

    # ── Tab 3: Engine & Dispatch ──────────────────────────────────────────

    def _build_tab_engine(self) -> None:
        f = self._tab_engine
        f.columnconfigure(1, weight=1)

        _section_header(f, "Generative AI Engine Selection", row=0)

        self._cb_engine = _labeled_combo(
            f, "Engine Provider *", 1,
            [ep.value for ep in EngineProvider], width=36,
        )
        self._cb_engine.set(EngineProvider.MOCK.value)
        self._cb_engine.bind("<<ComboboxSelected>>", self._on_engine_change)

        # API key
        tk.Label(f, text="API Key", anchor="w").grid(
            row=2, column=0, padx=PAD, pady=4, sticky="w"
        )
        self._v_api_key = tk.StringVar()
        api_entry = tk.Entry(f, textvariable=self._v_api_key, width=ENTRY_W, show="*")
        api_entry.grid(row=2, column=1, padx=PAD, pady=4, sticky="w")
        tk.Label(
            f, text="(Leave blank for Mock engine)",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=2, column=2, padx=4, sticky="w")

        self._lbl_engine_info = tk.Label(
            f, text="", anchor="w",
            font=("TkDefaultFont", 8, "italic"), fg="#555",
        )
        self._lbl_engine_info.grid(
            row=3, column=0, columnspan=3, padx=PAD, pady=2, sticky="w"
        )
        self._on_engine_change()

        _section_header(f, "Run Compliance Pre-Check", row=4)

        tk.Label(
            f,
            text=(
                "Validate the project specification against IBC 2023 rules\n"
                "before dispatching to the AI engine."
            ),
            anchor="w", justify="left",
        ).grid(row=5, column=0, columnspan=3, padx=PAD, pady=2, sticky="w")

        tk.Button(
            f, text="▶  Run Compliance Check Only",
            width=30, bg="#e8f4e8",
            command=self._run_compliance_only,
        ).grid(row=6, column=0, columnspan=2, padx=PAD, pady=6, sticky="w")

        _section_header(f, "Generate Construction Drawings", row=7)

        tk.Label(
            f,
            text=(
                "Validate spec, confirm compliance, then dispatch to the\n"
                "selected AI engine to generate the drawing manifest."
            ),
            anchor="w", justify="left",
        ).grid(row=8, column=0, columnspan=3, padx=PAD, pady=2, sticky="w")

        self._btn_generate = tk.Button(
            f, text="⚡  Generate Drawings",
            width=30, bg="#d0e8ff", font=("TkDefaultFont", 10, "bold"),
            command=self._run_dispatch,
        )
        self._btn_generate.grid(
            row=9, column=0, columnspan=2, padx=PAD, pady=8, sticky="w"
        )

        # Progress bar
        self._progress = ttk.Progressbar(f, mode="indeterminate", length=400)
        self._progress.grid(row=10, column=0, columnspan=3, padx=PAD, pady=4, sticky="w")

        _section_header(f, "Dispatch Log", row=11)

        self._txt_dispatch_log = scrolledtext.ScrolledText(
            f, height=10, font=("TkFixedFont", 8), state="disabled",
        )
        self._txt_dispatch_log.grid(
            row=12, column=0, columnspan=3, padx=PAD, pady=4, sticky="nsew"
        )
        f.rowconfigure(12, weight=1)

    # ── Tab 4: Results ────────────────────────────────────────────────────

    def _build_tab_results(self) -> None:
        f = self._tab_results
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        f.rowconfigure(3, weight=2)

        # Compliance report
        _section_header(f, "Compliance Report", row=0)

        self._txt_compliance = scrolledtext.ScrolledText(
            f, height=12, font=("TkFixedFont", 8), state="disabled",
        )
        self._txt_compliance.grid(
            row=1, column=0, padx=PAD, pady=4, sticky="nsew"
        )

        # Drawing manifest
        _section_header(f, "Generated Drawing Manifest", row=2)

        self._txt_drawings = scrolledtext.ScrolledText(
            f, height=16, font=("TkFixedFont", 8), state="disabled",
        )
        self._txt_drawings.grid(
            row=3, column=0, padx=PAD, pady=4, sticky="nsew"
        )

        # Export / reset
        btn_frame = tk.Frame(f)
        btn_frame.grid(row=4, column=0, padx=PAD, pady=6, sticky="ew")

        tk.Button(
            btn_frame, text="Export Manifest to Log",
            width=22, command=self._export_manifest,
        ).pack(side="left", padx=4)
        tk.Button(
            btn_frame, text="Clear Results",
            width=16, command=self._clear_results,
        ).pack(side="left", padx=4)

    # ── Event Handlers ────────────────────────────────────────────────────

    def _on_building_type_change(self) -> None:
        bt_str = self._v_building_type.get()
        bt = BuildingType(bt_str)
        groups = [og.value for og in OCCUPANCY_MAP[bt]]
        self._cb_occupancy["values"] = groups
        self._cb_occupancy.current(0)

    def _on_jurisdiction_change(self, _event: Any = None) -> None:
        name = self._cb_jurisdiction.get()
        try:
            j = JurisdictionLoader.load(name)
            info_lines = [
                f"State/City         : {j.display_name()}",
                f"Adopted Code       : {j.adopted_building_code.value}",
                f"Fire Code          : {j.adopted_fire_code}",
                f"Energy Code        : {j.adopted_energy_code}",
                f"Seismic SDC        : {j.seismic_design_category}",
                f"Wind Exposure      : {j.wind_exposure_category}",
                f"Wind Speed         : {j.wind_speed_mph or 'N/A'} mph",
                f"Snow Load          : {j.snow_load_psf or 'N/A'} psf",
                f"Frost Depth        : {j.frost_depth_in or 'N/A'} in",
                f"Flood Zone         : {j.flood_zone or 'None'}",
                f"Local Amendments   : {', '.join(j.local_amendments) or 'None'}",
            ]
            self._set_text(self._txt_jur_info, "\n".join(info_lines))
        except KeyError:
            self._set_text(self._txt_jur_info, "Unknown jurisdiction preset.")

    def _on_engine_change(self, _event: Any = None) -> None:
        provider_str = self._cb_engine.get() if hasattr(self, "_cb_engine") else EngineProvider.MOCK.value
        info_map = {
            EngineProvider.MOCK.value:   "Mock engine – no API key required. Generates placeholder SVG drawings instantly.",
            EngineProvider.CLAUDE.value: "Anthropic Claude – requires a valid API key. Generates JSON drawing manifests via claude-opus-4-5.",
            EngineProvider.NVIDIA.value: "NVIDIA Picasso – requires an NVIDIA API key. Generates raster drawing images.",
            EngineProvider.OPENAI.value: "OpenAI GPT-4o – requires an OpenAI API key. Generates structured JSON drawing manifests.",
        }
        if hasattr(self, "_lbl_engine_info"):
            self._lbl_engine_info.config(text=info_map.get(provider_str, ""))

    # ── Compliance-only run ───────────────────────────────────────────────

    def _run_compliance_only(self) -> None:
        try:
            spec = self._build_spec()
        except ValueError as exc:
            messagebox.showerror("Input Error", str(exc))
            return

        engine = ComplianceEngine()
        try:
            report = engine.validate(spec)
        except ValueError as exc:
            messagebox.showerror("Specification Error", str(exc))
            return

        self._display_compliance_report(report)
        self._set_status(
            f"Compliance check complete: {'PASS' if report.is_compliant else 'FAIL'} "
            f"({report.blocking_count} error(s), {report.warning_count} warning(s))"
        )

        # Switch to Results tab
        nb = self.winfo_children()[1]  # Notebook is the second child
        if isinstance(nb, ttk.Notebook):
            nb.select(3)

    # ── Full dispatch run ─────────────────────────────────────────────────

    def _run_dispatch(self) -> None:
        try:
            spec = self._build_spec()
        except ValueError as exc:
            messagebox.showerror("Input Error", str(exc))
            return

        # Register engine with API key
        self._register_engine(spec)

        self._btn_generate.config(state="disabled")
        self._progress.start(12)
        self._set_status("Dispatching to AI engine…")
        self._log_dispatch(f"[{datetime.utcnow().isoformat()}] Starting job for '{spec.project_name}'")

        def _worker() -> None:
            job = self._dispatcher.dispatch(spec)
            self.after(0, lambda: self._on_job_complete(job))

        Thread(target=_worker, daemon=True).start()

    def _on_job_complete(self, job: GenerationJob) -> None:
        self._progress.stop()
        self._btn_generate.config(state="normal")
        self._last_job = job

        if job.compliance_report:
            self._display_compliance_report(job.compliance_report)

        if job.status == JobStatus.COMPLETED:
            self._display_drawings(job)
            msg = (
                f"Job {job.job_id[:8]}… completed. "
                f"{len(job.drawings)} sheet(s) generated in "
                f"{job.duration_seconds():.2f}s."
            )
            self._log_dispatch(f"[SUCCESS] {msg}")
            self._set_status(msg)
            messagebox.showinfo("Generation Complete", msg)
        else:
            err = job.error_message or "Unknown failure"
            self._log_dispatch(f"[FAILED] {err}")
            self._set_status(f"Job failed: {err[:80]}")
            messagebox.showerror("Generation Failed", err)

        # Switch to results tab
        nb = self.winfo_children()[1]
        if isinstance(nb, ttk.Notebook):
            nb.select(3)

    # ── Display helpers ───────────────────────────────────────────────────

    def _display_compliance_report(self, report: Any) -> None:
        lines = [
            "=" * 70,
            f" COMPLIANCE REPORT",
            f" Status   : {'✔ PASS' if report.is_compliant else '✖ FAIL'}",
            f" Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f" Errors   : {report.blocking_count}",
            f" Warnings : {report.warning_count}",
            "=" * 70,
        ]
        if not report.findings:
            lines.append("  No findings – specification is fully compliant.")
        else:
            for finding in report.findings:
                icon = {"CRITICAL": "🔴", "ERROR": "🟠", "WARNING": "🟡", "INFO": "🔵"}.get(
                    finding.severity.value, " "
                )
                lines.append(f"\n{icon} [{finding.severity.value}] {finding.rule_id}")
                lines.append(f"   Code Section  : {finding.code_section}")
                lines.append(f"   Description   : {finding.description}")
                if finding.recommendation:
                    lines.append(f"   Recommendation: {finding.recommendation}")
        lines.append("=" * 70)
        self._set_text(self._txt_compliance, "\n".join(lines))

    def _display_drawings(self, job: GenerationJob) -> None:
        lines = [
            "=" * 70,
            f" DRAWING MANIFEST – {job.spec.project_name if job.spec else 'N/A'}",
            f" Engine  : {job.engine_provider.value}",
            f" Job ID  : {job.job_id}",
            f" Sheets  : {len(job.drawings)}",
            "=" * 70,
        ]
        for i, drawing in enumerate(job.drawings, 1):
            lines.append(f"\n  Sheet {i:02d}: {drawing.sheet_number}  –  {drawing.title}")
            lines.append(f"           Type  : {drawing.sheet_type.value}")
            lines.append(f"           Format: {drawing.format}")
            lines.append(f"           Available: {'Yes' if drawing.is_available() else 'No'}")
            if drawing.metadata:
                for k, v in drawing.metadata.items():
                    if k not in ("generated_at",) and v:
                        lines.append(f"           {k.capitalize()}: {str(v)[:80]}")
        lines.append("\n" + "=" * 70)
        self._set_text(self._txt_drawings, "\n".join(lines))

    # ── Spec builder ──────────────────────────────────────────────────────

    def _build_spec(self) -> ProjectSpecification:
        """Collect all GUI values and return a ProjectSpecification.  Raises ValueError on invalid input."""
        errors: List[str] = []

        project_name = self._v_project_name.get().strip()
        if not project_name:
            errors.append("Project Name is required.")

        try:
            building_type = BuildingType(self._v_building_type.get())
        except ValueError:
            errors.append("Invalid building type selected.")
            building_type = BuildingType.COMMERCIAL

        try:
            occupancy_group = OccupancyGroup(self._cb_occupancy.get())
        except ValueError:
            errors.append("Invalid occupancy group selected.")
            occupancy_group = OccupancyGroup.B

        try:
            construction_type = ConstructionType(self._cb_construction.get())
        except ValueError:
            errors.append("Invalid construction type selected.")
            construction_type = ConstructionType.IIA

        jur_name = self._cb_jurisdiction.get()
        try:
            jurisdiction = JurisdictionLoader.load(jur_name)
        except KeyError:
            errors.append(f"Unknown jurisdiction: '{jur_name}'.")
            jurisdiction = JurisdictionLoader.load("Custom / Manual Entry")

        try:
            primary_code = CodeVersion(self._cb_primary_code.get())
        except ValueError:
            errors.append("Invalid code version selected.")
            primary_code = CodeVersion.IBC_2023

        drawing_sets = [ds for ds, var in self._drawing_vars.items() if var.get()]
        if not drawing_sets:
            errors.append("Select at least one Drawing Set.")

        try:
            engine_provider = EngineProvider(self._cb_engine.get())
        except ValueError:
            errors.append("Invalid engine provider selected.")
            engine_provider = EngineProvider.MOCK

        # Optional numeric fields
        def _parse_float(val: str, label: str) -> Optional[float]:
            v = val.strip()
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                errors.append(f"'{label}' must be a number.")
                return None

        def _parse_int(val: str, label: str) -> Optional[int]:
            v = val.strip()
            if not v:
                return None
            try:
                return int(v)
            except ValueError:
                errors.append(f"'{label}' must be an integer.")
                return None

        gross_sqft    = _parse_float(self._v_gross_sqft.get(), "Gross Area")
        num_stories   = _parse_int(self._v_num_stories.get(), "Number of Stories")
        building_ht   = _parse_float(self._v_height_ft.get(), "Building Height")
        occupant_load = _parse_int(self._v_occ_load.get(), "Occupant Load")
        notes         = self._txt_notes.get("1.0", "end").strip()

        if errors:
            raise ValueError("Input validation failed:\n\n" + "\n".join(f"• {e}" for e in errors))

        return ProjectSpecification(
            project_name=project_name,
            building_type=building_type,
            occupancy_group=occupancy_group,
            construction_type=construction_type,
            jurisdiction=jurisdiction,
            primary_code=primary_code,
            drawing_sets=drawing_sets,
            engine_provider=engine_provider,
            gross_sq_ft=gross_sqft,
            num_stories=num_stories,
            building_height_ft=building_ht,
            occupant_load=occupant_load,
            sprinklered=self._v_sprinklered.get(),
            accessibility_standard=self._v_access_std.get().strip(),
            additional_notes=notes,
        )

    def _register_engine(self, spec: ProjectSpecification) -> None:
        """Instantiate and register the selected engine with the API key."""
        api_key = self._v_api_key.get().strip()
        from orchestrator import (
            ClaudeDrawingEngine, NvidiaDrawingEngine, OpenAIDrawingEngine,
        )
        engine_map = {
            EngineProvider.CLAUDE: lambda: ClaudeDrawingEngine(api_key=api_key),
            EngineProvider.NVIDIA: lambda: NvidiaDrawingEngine(api_key=api_key),
            EngineProvider.OPENAI: lambda: OpenAIDrawingEngine(api_key=api_key),
            EngineProvider.MOCK:   lambda: MockDrawingEngine(),
        }
        factory = engine_map.get(spec.engine_provider)
        if factory:
            EngineRegistry.register(factory())

    # ── Utility ───────────────────────────────────────────────────────────

    def _select_all_drawings(self, value: bool) -> None:
        for var in self._drawing_vars.values():
            var.set(value)

    def _select_full_set(self) -> None:
        self._select_all_drawings(False)
        self._drawing_vars[DrawingSet.FULL_SET].set(True)

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.config(state="disabled")

    def _log_dispatch(self, msg: str) -> None:
        self._txt_dispatch_log.config(state="normal")
        self._txt_dispatch_log.insert("end", msg + "\n")
        self._txt_dispatch_log.see("end")
        self._txt_dispatch_log.config(state="disabled")

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)

    def _export_manifest(self) -> None:
        if not self._last_job:
            messagebox.showinfo("No Data", "No completed job to export.")
            return
        log_path = Path("drawing_manifest.txt")
        content = self._txt_drawings.get("1.0", "end")
        compliance_content = self._txt_compliance.get("1.0", "end")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("COMPLIANCE REPORT\n")
            fh.write(compliance_content)
            fh.write("\n\nDRAWING MANIFEST\n")
            fh.write(content)
        messagebox.showinfo(
            "Exported",
            f"Manifest saved to:\n{log_path.resolve()}"
        )

    def _clear_results(self) -> None:
        self._set_text(self._txt_compliance, "")
        self._set_text(self._txt_drawings, "")
        self._last_job = None
        self._set_status("Results cleared.")


# ---------------------------------------------------------------------------
# CLI runner (headless mode for CI / server environments)
# ---------------------------------------------------------------------------

def _cli_demo() -> None:
    """
    Demonstrates the full pipeline without the GUI.
    Useful in headless CI environments where tkinter is unavailable.
    """
    from compliance import ComplianceEngine, JurisdictionLoader
    from models import (
        BuildingType, CodeVersion, ConstructionType,
        DrawingSet, EngineProvider, OccupancyGroup, ProjectSpecification,
    )
    from orchestrator import EngineDispatcher, EngineRegistry, MockDrawingEngine

    EngineRegistry.register(MockDrawingEngine())

    spec = ProjectSpecification(
        project_name="Demo Corporate HQ",
        building_type=BuildingType.COMMERCIAL,
        occupancy_group=OccupancyGroup.B,
        construction_type=ConstructionType.IIA,
        jurisdiction=JurisdictionLoader.load("Chicago, IL"),
        primary_code=CodeVersion.IBC_2021,
        drawing_sets=[
            DrawingSet.SITE, DrawingSet.FLOOR_PLAN,
            DrawingSet.ELEVATIONS, DrawingSet.STRUCTURAL,
        ],
        engine_provider=EngineProvider.MOCK,
        gross_sq_ft=22_000,
        num_stories=3,
        building_height_ft=42.0,
        sprinklered=True,
    )

    dispatcher = EngineDispatcher()
    job = dispatcher.dispatch(spec)

    print("\n" + "=" * 60)
    print(f"  JOB STATUS : {job.status.value.upper()}")
    if job.compliance_report:
        print(f"  COMPLIANCE : {job.compliance_report.summary()}")
        for f in job.compliance_report.findings:
            print(f"  {f}")
    if job.status == JobStatus.COMPLETED:
        print(f"\n  DRAWINGS GENERATED: {len(job.drawings)}")
        for d in job.drawings:
            print(f"  [{d.sheet_number}] {d.title}  ({d.format})")
    else:
        print(f"\n  ERROR: {job.error_message}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Check for --cli flag to run headless demo
    if "--cli" in sys.argv:
        _cli_demo()
        sys.exit(0)

    # Launch GUI
    try:
        app = ArchPlatformApp()
        app.mainloop()
    except tk.TclError as exc:
        # No display available – fall back to CLI demo
        logger.warning("tkinter display unavailable (%s). Running CLI demo.", exc)
        _cli_demo()
