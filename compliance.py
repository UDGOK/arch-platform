"""
compliance.py
=============
Compliance Engine – IBC 2023 Validation and Jurisdiction-Specific Rule Loading.

This module provides:
  • A registry of IBC 2023 rules keyed by section number
  • Jurisdiction-specific override loading
  • A ComplianceEngine that validates ProjectSpecification objects
  • A JurisdictionLoader that returns pre-configured Jurisdiction objects
    for a curated set of U.S. metros

All findings are returned as ComplianceFinding objects (see models.py).
No external libraries are required; only the Python standard library is used.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

from models import (
    BuildingType,
    CodeVersion,
    ComplianceFinding,
    ComplianceReport,
    ConstructionType,
    DrawingSet,
    Jurisdiction,
    OccupancyGroup,
    ProjectSpecification,
    Severity,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

RuleFunc = Callable[[ProjectSpecification], Optional[ComplianceFinding]]


# ---------------------------------------------------------------------------
# IBC 2023 Rule Implementations
# ---------------------------------------------------------------------------

def _rule_occupancy_construction_compatibility(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 Table 504.3 / 504.4 – Certain occupancy + construction type
    combinations are not permitted.  This is a representative subset.
    """
    DISALLOWED: Dict[Tuple[OccupancyGroup, ConstructionType], str] = {
        (OccupancyGroup.H1, ConstructionType.VB):
            "H-1 occupancies are prohibited in Type V-B construction "
            "(IBC 2023 §415.9).",
        (OccupancyGroup.H2, ConstructionType.VB):
            "H-2 occupancies are prohibited in Type V-B construction.",
        (OccupancyGroup.I2, ConstructionType.VB):
            "I-2 occupancies are prohibited in Type V-B construction "
            "(IBC 2023 §407.2).",
    }
    key = (spec.occupancy_group, spec.construction_type)
    if key in DISALLOWED:
        return ComplianceFinding(
            rule_id="IBC-504-COMPAT",
            severity=Severity.CRITICAL,
            code_section="IBC 2023 §504 / Table 504.3",
            description=DISALLOWED[key],
            recommendation=(
                "Select a permitted construction type for the "
                f"'{spec.occupancy_group.value}' occupancy group."
            ),
        )
    return None


def _rule_sprinkler_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §903.2 – Automatic sprinkler systems are required for
    certain occupancy groups and sizes.
    """
    sprinkler_mandatory: List[OccupancyGroup] = [
        OccupancyGroup.H1, OccupancyGroup.H2, OccupancyGroup.H3,
        OccupancyGroup.I1, OccupancyGroup.I2, OccupancyGroup.I3, OccupancyGroup.I4,
    ]
    if spec.occupancy_group in sprinkler_mandatory and not spec.sprinklered:
        return ComplianceFinding(
            rule_id="IBC-903-SPRINKLER",
            severity=Severity.CRITICAL,
            code_section="IBC 2023 §903.2",
            description=(
                f"Occupancy group '{spec.occupancy_group.value}' requires an "
                "automatic fire sprinkler system per IBC 2023 §903.2."
            ),
            recommendation="Enable sprinkler system in project specification.",
        )
    # Area threshold check for B/M/R-1 occupancies
    AREA_THRESHOLD_SQFT = 12_000
    area_groups = [OccupancyGroup.B, OccupancyGroup.M, OccupancyGroup.R1]
    if (
        spec.occupancy_group in area_groups
        and spec.gross_sq_ft is not None
        and spec.gross_sq_ft > AREA_THRESHOLD_SQFT
        and not spec.sprinklered
    ):
        return ComplianceFinding(
            rule_id="IBC-903-AREA",
            severity=Severity.ERROR,
            code_section="IBC 2023 §903.2.3",
            description=(
                f"Building area {spec.gross_sq_ft:,.0f} sq ft exceeds the "
                f"{AREA_THRESHOLD_SQFT:,} sq ft threshold for "
                f"'{spec.occupancy_group.value}' without sprinklers."
            ),
            recommendation="Add automatic sprinkler system or reduce building area.",
        )
    return None


def _rule_height_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 Table 504.3 – Maximum building height by construction type
    (selected representative values).
    """
    # {ConstructionType: max_height_ft}  (None = unlimited)
    HEIGHT_LIMITS: Dict[ConstructionType, Optional[float]] = {
        ConstructionType.IA:   None,
        ConstructionType.IB:   None,
        ConstructionType.IIA:  65.0,
        ConstructionType.IIB:  55.0,
        ConstructionType.IIIA: 65.0,
        ConstructionType.IIIB: 55.0,
        ConstructionType.VA:   50.0,
        ConstructionType.VB:   40.0,
        ConstructionType.IV_HT: 85.0,
        ConstructionType.IV_A:  None,
        ConstructionType.IV_B:  None,
        ConstructionType.IV_C:  None,
    }
    limit = HEIGHT_LIMITS.get(spec.construction_type)
    if (
        limit is not None
        and spec.building_height_ft is not None
        and spec.building_height_ft > limit
    ):
        return ComplianceFinding(
            rule_id="IBC-504-HEIGHT",
            severity=Severity.ERROR,
            code_section="IBC 2023 Table 504.3",
            description=(
                f"Building height {spec.building_height_ft:.1f} ft exceeds the "
                f"{limit:.0f} ft maximum for {spec.construction_type.value}."
            ),
            recommendation=(
                "Upgrade construction type (e.g., Type I-A/I-B) or reduce "
                "building height."
            ),
        )
    return None


def _rule_story_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 Table 504.4 – Maximum number of stories above grade.
    Values shown are for representative non-sprinklered B occupancy.
    """
    UNLIMITED = 9999
    STORY_LIMITS: Dict[ConstructionType, int] = {
        ConstructionType.IA:   UNLIMITED,
        ConstructionType.IB:   UNLIMITED,
        ConstructionType.IIA:  4,
        ConstructionType.IIB:  4,
        ConstructionType.IIIA: 4,
        ConstructionType.IIIB: 3,
        ConstructionType.VA:   2,
        ConstructionType.VB:   1,
        ConstructionType.IV_HT: 5,
        ConstructionType.IV_A:  UNLIMITED,
        ConstructionType.IV_B:  UNLIMITED,
        ConstructionType.IV_C:  UNLIMITED,
    }
    limit = STORY_LIMITS.get(spec.construction_type, 1)
    if (
        limit < 9999
        and spec.num_stories is not None
        and spec.num_stories > limit
    ):
        return ComplianceFinding(
            rule_id="IBC-504-STORIES",
            severity=Severity.ERROR,
            code_section="IBC 2023 Table 504.4",
            description=(
                f"{spec.num_stories} stories exceeds the {limit}-story limit "
                f"for {spec.construction_type.value} construction (non-sprinklered)."
            ),
            recommendation=(
                "Add sprinkler system (may increase story limit), upgrade "
                "construction type, or reduce building height."
            ),
        )
    return None


def _rule_accessibility_drawings_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1101 – Commercial / public buildings must include
    accessibility plans in the drawing set.
    """
    if (
        spec.building_type == BuildingType.COMMERCIAL
        and DrawingSet.ACCESSIBILITY not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-1101-ACCESS",
            severity=Severity.WARNING,
            code_section="IBC 2023 §1101.2 / ADA §4.1",
            description=(
                "Commercial projects should include an Accessibility Plan "
                "demonstrating ADA / ICC A117.1 compliance."
            ),
            recommendation="Add 'Accessibility Plan' to the requested drawing set.",
        )
    return None


def _rule_fire_life_safety_drawings(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §907 – Fire & Life Safety drawings are required for
    I, H, and A occupancy groups.
    """
    mandatory_groups = [
        OccupancyGroup.I1, OccupancyGroup.I2, OccupancyGroup.I3,
        OccupancyGroup.H1, OccupancyGroup.H2, OccupancyGroup.H3,
        OccupancyGroup.A1, OccupancyGroup.A2, OccupancyGroup.A3,
    ]
    if (
        spec.occupancy_group in mandatory_groups
        and DrawingSet.FIRE_LIFE not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-907-FLS",
            severity=Severity.WARNING,
            code_section="IBC 2023 §907.2",
            description=(
                f"'{spec.occupancy_group.value}' occupancy typically requires "
                "Fire & Life Safety drawings for permitting."
            ),
            recommendation="Add 'Fire & Life Safety' to the drawing set.",
        )
    return None


def _rule_jurisdiction_code_alignment(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    Verifies that the selected primary code matches the code adopted
    by the project jurisdiction.
    """
    if spec.primary_code != spec.jurisdiction.adopted_building_code:
        return ComplianceFinding(
            rule_id="JUR-CODE-MISMATCH",
            severity=Severity.WARNING,
            code_section="Jurisdictional Adoption",
            description=(
                f"Selected code '{spec.primary_code.value}' does not match "
                f"the jurisdiction's adopted code "
                f"'{spec.jurisdiction.adopted_building_code.value}' for "
                f"{spec.jurisdiction.display_name()}."
            ),
            recommendation=(
                f"Update primary code to "
                f"'{spec.jurisdiction.adopted_building_code.value}' "
                "to match local adoption, or verify the jurisdiction has "
                "adopted the selected code by amendment."
            ),
        )
    return None


def _rule_seismic_structural_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1613 – High seismic design categories (D/E/F) require
    structural drawings in the set.
    """
    HIGH_SDC = {"D", "E", "F"}
    if (
        spec.jurisdiction.seismic_design_category in HIGH_SDC
        and DrawingSet.STRUCTURAL not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-1613-SEISMIC",
            severity=Severity.ERROR,
            code_section="IBC 2023 §1613 / ASCE 7-22 Ch.12",
            description=(
                f"Seismic Design Category '{spec.jurisdiction.seismic_design_category}' "
                "requires structural drawings with seismic resistance analysis."
            ),
            recommendation="Add 'Structural Drawings' to the drawing set.",
        )
    return None


def _rule_occupant_load_egress(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1006 – Buildings with occupant load > 500 require
    at least one accessible exit route analysis note in the drawing set.
    """
    if (
        spec.occupant_load is not None
        and spec.occupant_load > 500
        and DrawingSet.FIRE_LIFE not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-1006-EGRESS",
            severity=Severity.WARNING,
            code_section="IBC 2023 §1006.3",
            description=(
                f"Occupant load of {spec.occupant_load} exceeds 500; "
                "egress path analysis must be documented."
            ),
            recommendation=(
                "Include means-of-egress diagrams in Fire & Life Safety drawings."
            ),
        )
    return None


def _rule_residential_code_version(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    Informational: R-3/R-4 occupancies are often regulated by the IRC,
    not the IBC. Flag this for designer awareness.
    """
    IRC_GROUPS = [OccupancyGroup.R3, OccupancyGroup.R4]
    if spec.occupancy_group in IRC_GROUPS and spec.primary_code == CodeVersion.IBC_2023:
        return ComplianceFinding(
            rule_id="IBC-INFO-IRC",
            severity=Severity.INFO,
            code_section="IBC 2023 §101.2 Exception",
            description=(
                f"'{spec.occupancy_group.value}' occupancies (1 & 2 family) "
                "are typically governed by the IRC, not the IBC."
            ),
            recommendation=(
                "Confirm with the AHJ whether IRC or IBC governs; update "
                "primary code accordingly."
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Rule Registry
# ---------------------------------------------------------------------------

IBC_2023_RULES: List[RuleFunc] = [
    _rule_occupancy_construction_compatibility,
    _rule_sprinkler_required,
    _rule_height_limit,
    _rule_story_limit,
    _rule_accessibility_drawings_required,
    _rule_fire_life_safety_drawings,
    _rule_jurisdiction_code_alignment,
    _rule_seismic_structural_required,
    _rule_occupant_load_egress,
    _rule_residential_code_version,
]

# Code-to-rule-set mapping (other codes fall back to IBC_2023_RULES)
CODE_RULE_MAP: Dict[CodeVersion, List[RuleFunc]] = {
    CodeVersion.IBC_2023: IBC_2023_RULES,
    CodeVersion.IBC_2021: IBC_2023_RULES,   # reuse; real implementation would differ
    CodeVersion.IBC_2018: IBC_2023_RULES,
    CodeVersion.CBC_2022: IBC_2023_RULES,   # California amendments not yet modelled
    CodeVersion.NFPA_5000: IBC_2023_RULES,
}


# ---------------------------------------------------------------------------
# Jurisdiction Loader
# ---------------------------------------------------------------------------

class JurisdictionLoader:
    """
    Factory that returns pre-configured Jurisdiction objects for
    commonly used U.S. metros, incorporating known code adoptions,
    seismic data, and design parameters.
    """

    _PRESETS: Dict[str, Jurisdiction] = {
        "New York City, NY": Jurisdiction(
            state="NY", county="New York", city="New York City",
            zip_code="10001",
            adopted_building_code=CodeVersion.IBC_2021,
            adopted_fire_code="NYCFC 2022",
            adopted_energy_code="NYCECC 2020",
            local_amendments=["NYC Building Code 2022", "Local Law 97 (Carbon)"],
            seismic_design_category="B",
            wind_exposure_category="D",
            wind_speed_mph=115.0,
            frost_depth_in=36.0,
        ),
        "Los Angeles, CA": Jurisdiction(
            state="CA", county="Los Angeles", city="Los Angeles",
            zip_code="90001",
            adopted_building_code=CodeVersion.CBC_2022,
            adopted_fire_code="CFC 2022",
            adopted_energy_code="Title 24 Part 6 (2022)",
            local_amendments=["LA Green Building Code", "LAMC Ch. IX"],
            seismic_design_category="D",
            wind_exposure_category="B",
            wind_speed_mph=95.0,
            frost_depth_in=0.0,
        ),
        "Chicago, IL": Jurisdiction(
            state="IL", county="Cook", city="Chicago",
            zip_code="60601",
            adopted_building_code=CodeVersion.IBC_2021,
            adopted_fire_code="IFC 2021",
            adopted_energy_code="IECC 2021",
            local_amendments=["Chicago Building Code (CBC)"],
            seismic_design_category="B",
            wind_exposure_category="B",
            wind_speed_mph=90.0,
            snow_load_psf=25.0,
            frost_depth_in=42.0,
        ),
        "Houston, TX": Jurisdiction(
            state="TX", county="Harris", city="Houston",
            zip_code="77001",
            adopted_building_code=CodeVersion.IBC_2021,
            adopted_fire_code="IFC 2021",
            adopted_energy_code="IECC 2021",
            local_amendments=["Houston Amendments to IBC 2021"],
            seismic_design_category="A",
            wind_exposure_category="D",
            wind_speed_mph=130.0,
            flood_zone="AE",
            frost_depth_in=0.0,
        ),
        "Miami, FL": Jurisdiction(
            state="FL", county="Miami-Dade", city="Miami",
            zip_code="33101",
            adopted_building_code=CodeVersion.IBC_2023,
            adopted_fire_code="FBC Fire 2023",
            adopted_energy_code="FBC Energy 2023",
            local_amendments=["FBC 2023", "Miami-Dade HVHZ"],
            seismic_design_category="A",
            wind_exposure_category="D",
            wind_speed_mph=175.0,
            flood_zone="VE",
            frost_depth_in=0.0,
        ),
        "Seattle, WA": Jurisdiction(
            state="WA", county="King", city="Seattle",
            zip_code="98101",
            adopted_building_code=CodeVersion.IBC_2021,
            adopted_fire_code="IFC 2021",
            adopted_energy_code="WSEC 2021",
            local_amendments=["Seattle Building Code", "SMC Title 22"],
            seismic_design_category="D",
            wind_exposure_category="B",
            wind_speed_mph=85.0,
            snow_load_psf=25.0,
            frost_depth_in=12.0,
        ),
        "Phoenix, AZ": Jurisdiction(
            state="AZ", county="Maricopa", city="Phoenix",
            zip_code="85001",
            adopted_building_code=CodeVersion.IBC_2018,
            adopted_fire_code="IFC 2018",
            adopted_energy_code="IECC 2018",
            local_amendments=["Phoenix Fire Code Amendments"],
            seismic_design_category="C",
            wind_exposure_category="B",
            wind_speed_mph=90.0,
            frost_depth_in=0.0,
        ),
        "Denver, CO": Jurisdiction(
            state="CO", county="Denver", city="Denver",
            zip_code="80201",
            adopted_building_code=CodeVersion.IBC_2021,
            adopted_fire_code="IFC 2021",
            adopted_energy_code="IECC 2021",
            local_amendments=["Denver Building and Fire Code"],
            seismic_design_category="B",
            wind_exposure_category="B",
            wind_speed_mph=105.0,
            snow_load_psf=30.0,
            frost_depth_in=36.0,
        ),
        "Custom / Manual Entry": Jurisdiction(
            state="", county="", city="",
            zip_code="",
            adopted_building_code=CodeVersion.IBC_2023,
        ),
    }

    @classmethod
    def available_jurisdictions(cls) -> List[str]:
        return list(cls._PRESETS.keys())

    @classmethod
    def load(cls, name: str) -> Jurisdiction:
        if name not in cls._PRESETS:
            raise KeyError(
                f"Unknown jurisdiction preset '{name}'. "
                f"Available: {cls.available_jurisdictions()}"
            )
        logger.debug("Loaded jurisdiction preset: %s", name)
        return cls._PRESETS[name]


# ---------------------------------------------------------------------------
# Compliance Engine
# ---------------------------------------------------------------------------

class ComplianceEngine:
    """
    Validates a ProjectSpecification against the applicable code rule set
    and any jurisdiction-specific overrides.

    Usage::

        engine = ComplianceEngine()
        report = engine.validate(spec)
        if not report.is_compliant:
            print(report.summary())
            for f in report.findings:
                print(f)
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)

    def validate(self, spec: ProjectSpecification) -> ComplianceReport:
        """
        Run all applicable compliance rules against *spec*.

        Returns a ComplianceReport containing all findings.
        Raises ValueError if the spec object is missing required fields.
        """
        self._logger.info(
            "Starting compliance validation for project '%s' [%s]",
            spec.project_name, spec.project_id,
        )

        self._pre_validate(spec)

        rules = CODE_RULE_MAP.get(spec.primary_code, IBC_2023_RULES)
        findings: List[ComplianceFinding] = []

        for rule_fn in rules:
            try:
                finding = rule_fn(spec)
                if finding is not None:
                    findings.append(finding)
                    self._logger.debug(
                        "Rule '%s' produced finding: [%s] %s",
                        rule_fn.__name__,
                        finding.severity.value,
                        finding.description[:80],
                    )
            except Exception as exc:  # pragma: no cover
                self._logger.error(
                    "Unexpected error in rule '%s': %s",
                    rule_fn.__name__, exc, exc_info=True,
                )

        report = ComplianceReport(spec_id=spec.project_id, findings=findings)
        self._logger.info(
            "Compliance validation complete: %s", report.summary()
        )
        return report

    @staticmethod
    def _pre_validate(spec: ProjectSpecification) -> None:
        """Raises ValueError on missing required fields."""
        errors: List[str] = []
        if not spec.project_name or not spec.project_name.strip():
            errors.append("project_name is required.")
        if spec.jurisdiction.state == "" and spec.jurisdiction.city == "":
            errors.append(
                "Jurisdiction is incomplete. Provide state and city, or select "
                "a preset jurisdiction."
            )
        if not spec.drawing_sets:
            errors.append("At least one drawing set must be selected.")
        if errors:
            raise ValueError("ProjectSpecification validation failed:\n" +
                             "\n".join(f"  • {e}" for e in errors))
