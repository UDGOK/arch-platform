"""
models.py
=========
Data classes and type definitions for the Architectural AI Platform.
All domain objects used across the system are defined here to maintain
a single source of truth for data contracts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BuildingType(str, Enum):
    """Top-level occupancy classification per IBC 2023 Chapter 3."""
    COMMERCIAL = "Commercial"
    RESIDENTIAL = "Residential"


class OccupancyGroup(str, Enum):
    """IBC 2023 Table 302.1 – Use and Occupancy Classification."""
    # Commercial groups
    A1 = "A-1"   # Assembly – fixed seating (theaters)
    A2 = "A-2"   # Assembly – food/drink (restaurants)
    A3 = "A-3"   # Assembly – worship, recreation
    A4 = "A-4"   # Assembly – indoor sporting
    A5 = "A-5"   # Assembly – outdoor activities
    B  = "B"     # Business
    E  = "E"     # Educational
    F1 = "F-1"   # Factory – moderate hazard
    F2 = "F-2"   # Factory – low hazard
    H1 = "H-1"   # High Hazard – detonation
    H2 = "H-2"   # High Hazard – deflagration
    H3 = "H-3"   # High Hazard – combustible
    H4 = "H-4"   # High Hazard – health
    H5 = "H-5"   # HPM fabrication
    I1 = "I-1"   # Institutional – supervised
    I2 = "I-2"   # Institutional – incapacitated
    I3 = "I-3"   # Institutional – restrained
    I4 = "I-4"   # Institutional – day care
    M  = "M"     # Mercantile
    R1 = "R-1"   # Residential – transient
    R2 = "R-2"   # Residential – permanent multi
    R3 = "R-3"   # Residential – 1 & 2 family / townhouse
    R4 = "R-4"   # Residential – care / assisted living
    S1 = "S-1"   # Storage – moderate hazard
    S2 = "S-2"   # Storage – low hazard
    U  = "U"     # Utility / Miscellaneous


class ConstructionType(str, Enum):
    """IBC 2023 Table 601 – Construction Types."""
    IA  = "Type I-A"
    IB  = "Type I-B"
    IIA = "Type II-A"
    IIB = "Type II-B"
    IIIA = "Type III-A"
    IIIB = "Type III-B"
    IV_HT = "Type IV-HT"
    IV_A  = "Type IV-A"
    IV_B  = "Type IV-B"
    IV_C  = "Type IV-C"
    VA  = "Type V-A"
    VB  = "Type V-B"


class CodeVersion(str, Enum):
    """Supported model building codes."""
    IBC_2023 = "IBC 2023"
    IBC_2021 = "IBC 2021"
    IBC_2018 = "IBC 2018"
    CBC_2022 = "CBC 2022"   # California
    NFPA_5000 = "NFPA 5000"


class DrawingSet(str, Enum):
    """Standard construction drawing sheet types."""
    SITE         = "Site Plan"
    FLOOR_PLAN   = "Floor Plan"
    ELEVATIONS   = "Exterior Elevations"
    SECTIONS     = "Building Sections"
    DETAILS      = "Architectural Details"
    STRUCTURAL   = "Structural Drawings"
    MEP          = "MEP Drawings"
    FIRE_LIFE    = "Fire & Life Safety"
    ACCESSIBILITY = "Accessibility Plan"
    FULL_SET     = "Full Construction Set"


class EngineProvider(str, Enum):
    """Supported generative AI engine back-ends."""
    CLAUDE   = "Anthropic Claude"
    NVIDIA   = "NVIDIA Picasso"
    OPENAI   = "OpenAI GPT-4o"
    MOCK     = "Mock Engine (Testing)"


class JobStatus(str, Enum):
    """Lifecycle states for a drawing generation job."""
    PENDING    = "pending"
    VALIDATING = "validating"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class Severity(str, Enum):
    """Compliance finding severity levels."""
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Jurisdiction
# ---------------------------------------------------------------------------

@dataclass
class Jurisdiction:
    """Geographic and regulatory context for a project."""
    state: str
    county: str
    city: str
    zip_code: str
    # Code adoptions
    adopted_building_code: CodeVersion = CodeVersion.IBC_2023
    adopted_fire_code: str = "IFC 2023"
    adopted_energy_code: str = "IECC 2021"
    # Local amendments
    local_amendments: List[str] = field(default_factory=list)
    seismic_design_category: str = "B"     # IBC 2023 §1613
    wind_exposure_category: str = "B"      # ASCE 7-22
    flood_zone: Optional[str] = None       # FEMA designation
    snow_load_psf: Optional[float] = None  # Ground snow load (psf)
    wind_speed_mph: Optional[float] = None # Basic wind speed (mph)
    frost_depth_in: Optional[float] = None # Frost depth (inches)

    def display_name(self) -> str:
        return f"{self.city}, {self.state} {self.zip_code}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "county": self.county,
            "city": self.city,
            "zip_code": self.zip_code,
            "adopted_building_code": self.adopted_building_code.value,
            "adopted_fire_code": self.adopted_fire_code,
            "adopted_energy_code": self.adopted_energy_code,
            "local_amendments": self.local_amendments,
            "seismic_design_category": self.seismic_design_category,
            "wind_exposure_category": self.wind_exposure_category,
            "flood_zone": self.flood_zone,
            "snow_load_psf": self.snow_load_psf,
            "wind_speed_mph": self.wind_speed_mph,
            "frost_depth_in": self.frost_depth_in,
        }


# ---------------------------------------------------------------------------
# Project Specification
# ---------------------------------------------------------------------------

@dataclass
class ProjectSpecification:
    """
    Complete specification for an architectural project.
    This is the primary input contract passed between all system layers.
    """
    project_name: str
    building_type: BuildingType
    occupancy_group: OccupancyGroup
    construction_type: ConstructionType
    jurisdiction: Jurisdiction
    primary_code: CodeVersion
    drawing_sets: List[DrawingSet]
    engine_provider: EngineProvider

    # Optional dimensional parameters
    gross_sq_ft: Optional[float] = None
    num_stories: Optional[int] = None
    building_height_ft: Optional[float] = None
    occupant_load: Optional[int] = None
    sprinklered: bool = False
    accessibility_standard: str = "ADA / ICC A117.1-2017"

    # Metadata
    project_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)
    additional_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "created_at": self.created_at.isoformat(),
            "building_type": self.building_type.value,
            "occupancy_group": self.occupancy_group.value,
            "construction_type": self.construction_type.value,
            "jurisdiction": self.jurisdiction.to_dict(),
            "primary_code": self.primary_code.value,
            "drawing_sets": [d.value for d in self.drawing_sets],
            "engine_provider": self.engine_provider.value,
            "gross_sq_ft": self.gross_sq_ft,
            "num_stories": self.num_stories,
            "building_height_ft": self.building_height_ft,
            "occupant_load": self.occupant_load,
            "sprinklered": self.sprinklered,
            "accessibility_standard": self.accessibility_standard,
            "additional_notes": self.additional_notes,
        }


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

@dataclass
class ComplianceFinding:
    """A single compliance check result."""
    rule_id: str
    severity: Severity
    code_section: str
    description: str
    recommendation: Optional[str] = None

    def is_blocking(self) -> bool:
        return self.severity in (Severity.ERROR, Severity.CRITICAL)

    def __str__(self) -> str:
        rec = f" → {self.recommendation}" if self.recommendation else ""
        return f"[{self.severity.value}] {self.code_section}: {self.description}{rec}"


@dataclass
class ComplianceReport:
    """Aggregated result of all compliance checks for a specification."""
    spec_id: str
    generated_at: datetime = field(default_factory=datetime.utcnow)
    findings: List[ComplianceFinding] = field(default_factory=list)

    @property
    def is_compliant(self) -> bool:
        return not any(f.is_blocking() for f in self.findings)

    @property
    def blocking_count(self) -> int:
        return sum(1 for f in self.findings if f.is_blocking())

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings
                   if f.severity == Severity.WARNING)

    def summary(self) -> str:
        status = "PASS" if self.is_compliant else "FAIL"
        return (
            f"Compliance Report [{status}] – "
            f"{self.blocking_count} error(s), {self.warning_count} warning(s)"
        )


# ---------------------------------------------------------------------------
# Drawing Output
# ---------------------------------------------------------------------------

@dataclass
class DrawingOutput:
    """A single generated drawing returned by an AI engine."""
    sheet_type: DrawingSet
    sheet_number: str
    title: str
    format: str = "SVG"          # SVG | DXF | PDF
    data: Optional[bytes] = None
    url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def is_available(self) -> bool:
        return self.data is not None or self.url is not None


@dataclass
class GenerationJob:
    """
    Tracks the full lifecycle of a drawing generation request,
    from dispatch through completion or failure.
    """
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    spec: Optional[ProjectSpecification] = None
    compliance_report: Optional[ComplianceReport] = None
    status: JobStatus = JobStatus.PENDING
    engine_provider: EngineProvider = EngineProvider.MOCK
    drawings: List[DrawingOutput] = field(default_factory=list)

    created_at: datetime = field(default_factory=datetime.utcnow)
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    error_message: Optional[str] = None
    engine_request_id: Optional[str] = None

    def mark_dispatched(self) -> None:
        self.status = JobStatus.DISPATCHED
        self.dispatched_at = datetime.utcnow()

    def mark_completed(self, drawings: List[DrawingOutput]) -> None:
        self.status = JobStatus.COMPLETED
        self.drawings = drawings
        self.completed_at = datetime.utcnow()

    def mark_failed(self, reason: str) -> None:
        self.status = JobStatus.FAILED
        self.error_message = reason
        self.completed_at = datetime.utcnow()

    def duration_seconds(self) -> Optional[float]:
        if self.dispatched_at and self.completed_at:
            return (self.completed_at - self.dispatched_at).total_seconds()
        return None
