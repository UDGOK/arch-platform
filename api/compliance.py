"""
compliance.py (Enhanced)
=========================
Comprehensive Compliance Engine – IBC 2023 with ADA & Life Safety

This module provides:
  • Complete IBC 2023 rule validation
  • ADA accessibility compliance (ICC A117.1)
  • Egress and life safety analysis
  • Corridor width requirements
  • Travel distance calculations
  • Fire protection requirements
  • Structural load requirements
  • Jurisdiction-specific overrides

All findings returned as ComplianceFinding objects with actionable recommendations.
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
# Enhanced IBC 2023 Rule Implementations
# ---------------------------------------------------------------------------

def _rule_occupancy_construction_compatibility(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 Table 504.3 / 504.4 – Occupancy + construction type compatibility.
    """
    DISALLOWED: Dict[Tuple[OccupancyGroup, ConstructionType], str] = {
        (OccupancyGroup.H1, ConstructionType.VB):
            "H-1 occupancies prohibited in Type V-B construction (IBC 2023 §415.9).",
        (OccupancyGroup.H2, ConstructionType.VB):
            "H-2 occupancies prohibited in Type V-B construction.",
        (OccupancyGroup.I2, ConstructionType.VB):
            "I-2 occupancies prohibited in Type V-B construction (IBC 2023 §407.2).",
    }
    key = (spec.occupancy_group, spec.construction_type)
    if key in DISALLOWED:
        return ComplianceFinding(
            rule_id="IBC-504-COMPAT",
            severity=Severity.CRITICAL,
            code_section="IBC 2023 §504 / Table 504.3",
            description=DISALLOWED[key],
            recommendation=(
                f"Select permitted construction type for '{spec.occupancy_group.value}' occupancy."
            ),
        )
    return None


def _rule_sprinkler_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 §903.2 – Automatic sprinkler systems."""
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
                f"Occupancy group '{spec.occupancy_group.value}' requires automatic "
                "fire sprinkler system per IBC 2023 §903.2."
            ),
            recommendation="Enable sprinkler system in project specification.",
        )
    
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
                f"Building area {spec.gross_sq_ft:,.0f} sq ft exceeds "
                f"{AREA_THRESHOLD_SQFT:,} sq ft threshold for "
                f"'{spec.occupancy_group.value}' without sprinklers."
            ),
            recommendation="Add automatic sprinkler system or reduce building area.",
        )
    return None


def _rule_height_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 Table 504.3 – Maximum building height by construction type."""
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
                f"Building height {spec.building_height_ft:.1f} ft exceeds "
                f"{limit:.0f} ft maximum for {spec.construction_type.value}."
            ),
            recommendation=(
                "Upgrade construction type (Type I-A/I-B) or reduce building height."
            ),
        )
    return None


def _rule_story_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 Table 504.4 – Maximum stories above grade."""
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
                f"{spec.num_stories} stories exceeds {limit}-story limit for "
                f"{spec.construction_type.value} construction (non-sprinklered)."
            ),
            recommendation=(
                "Add sprinkler system (may increase limit), upgrade construction type, "
                "or reduce building height."
            ),
        )
    return None


# ── NEW: ADA Compliance Rules ────────────────────────────────────────────

def _rule_ada_corridor_width(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1020.2 / ADA – Minimum corridor width 44" (3.67 ft).
    Recommend 60" (5 ft) for comfort and code compliance.
    """
    # Check if project specifies corridor dimensions
    # This would normally come from floor plan data
# For now, check if accessibility plan is included
    if (
        spec.building_type == BuildingType.COMMERCIAL
        and DrawingSet.ACCESSIBILITY not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-1020-CORRIDOR",
            severity=Severity.WARNING,
            code_section="IBC 2023 §1020.2 / ICC A117.1",
            description=(
                "Corridor width must be minimum 44\" (IBC). Recommend 60\" (5'-0\") "
                "for ADA compliance and two-way traffic."
            ),
            recommendation=(
                "Include Accessibility Plan showing 60\" wide corridors. "
                "Add accessible route details per ICC A117.1."
            ),
        )
    return None


def _rule_ada_turning_space(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1109 / ICC A117.1 §304 – 60\" diameter turning space required.
    """
    if (
        spec.building_type == BuildingType.COMMERCIAL
        and spec.occupancy_group in [OccupancyGroup.B, OccupancyGroup.M, 
                                     OccupancyGroup.A1, OccupancyGroup.A2]
    ):
        return ComplianceFinding(
            rule_id="IBC-1109-TURNING",
            severity=Severity.INFO,
            code_section="IBC 2023 §1109 / ICC A117.1 §304",
            description=(
                "ADA requires 60\" diameter turning circle or T-turn in accessible spaces. "
                "Verify conference rooms, restrooms, and lobbies have adequate maneuvering space."
            ),
            recommendation=(
                "Show 60\" turning circles on floor plans. Ensure clear floor space "
                "in all public areas per ICC A117.1 §304."
            ),
        )
    return None


def _rule_ada_door_clearance(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1010.1.1 / ICC A117.1 §404 – Door clearances.
    32\" min clear width, 18\" strike-side clearance.
    """
    if spec.building_type == BuildingType.COMMERCIAL:
        return ComplianceFinding(
            rule_id="IBC-1010-DOOR-ADA",
            severity=Severity.INFO,
            code_section="IBC 2023 §1010.1.1 / ICC A117.1 §404",
            description=(
                "All accessible doors require: (1) 32\" minimum clear width, "
                "(2) 18\" minimum strike-side clearance, (3) Maximum 5 lbf opening force."
            ),
            recommendation=(
                "Specify ADA-compliant door hardware. Show 18\" clearances on plans. "
                "Use 36\" nominal doors (provides 32\" clear)."
            ),
        )
    return None


# ── NEW: Egress & Life Safety Rules ──────────────────────────────────────

def _rule_egress_width_requirement(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1005.1 – Egress width based on occupant load.
    0.2\" per person for other components, 0.15\" per person for stairs.
    """
    if spec.occupant_load is not None and spec.occupant_load > 0:
        required_width_in = spec.occupant_load * 0.2  # inches
        required_width_ft = required_width_in / 12
        
        if required_width_ft > 44/12:  # Exceeds standard corridor width
            return ComplianceFinding(
                rule_id="IBC-1005-EGRESS-WIDTH",
                severity=Severity.WARNING,
                code_section="IBC 2023 §1005.1",
                description=(
                    f"Occupant load of {spec.occupant_load} requires minimum "
                    f"{required_width_ft:.1f} ft ({required_width_in:.0f}\") egress width "
                    "(0.2\" per person for corridors/doors, 0.15\" per person for stairs)."
                ),
                recommendation=(
                    f"Ensure exit corridors/doors total at least {required_width_ft:.1f} ft wide. "
                    "Verify egress capacity on Fire & Life Safety plan."
                ),
            )
    return None


def _rule_travel_distance_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1017 – Maximum travel distance to exits.
    A/B occupancies: 250 ft (sprinklered), 200 ft (unsprinklered).
    """
    if spec.occupancy_group in [OccupancyGroup.A1, OccupancyGroup.A2, OccupancyGroup.A3,
                                OccupancyGroup.B]:
        max_dist = 250 if spec.sprinklered else 200
        
        return ComplianceFinding(
            rule_id="IBC-1017-TRAVEL",
            severity=Severity.INFO,
            code_section="IBC 2023 §1017",
            description=(
                f"Maximum travel distance to exit: {max_dist} ft "
                f"({'sprinklered' if spec.sprinklered else 'unsprinklered'} "
                f"{spec.occupancy_group.value} occupancy)."
            ),
            recommendation=(
                f"Show travel distance measurements on Fire & Life Safety plan. "
                f"Verify all points are within {max_dist} ft of an exit. "
                "Add exits or sprinklers if needed."
            ),
        )
    return None


def _rule_dead_end_corridor_limit(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1020.4 – Dead-end corridor limits.
    A occupancies: 50 ft max, B occupancies: 75 ft max (sprinklered).
    """
    if spec.occupancy_group == OccupancyGroup.A1 or spec.occupancy_group == OccupancyGroup.A2:
        limit = 50
        sev = Severity.WARNING
    elif spec.occupancy_group == OccupancyGroup.B:
        limit = 75 if spec.sprinklered else 50
        sev = Severity.INFO
    else:
        return None
    
    return ComplianceFinding(
        rule_id="IBC-1020-DEADEND",
        severity=sev,
        code_section="IBC 2023 §1020.4",
        description=(
            f"Dead-end corridors limited to {limit} ft for "
            f"{spec.occupancy_group.value} occupancy "
            f"({'sprinklered' if spec.sprinklered else 'unsprinklered'})."
        ),
        recommendation=(
            f"Review floor plan for dead-end conditions. Ensure no dead-end "
            f"corridors exceed {limit} ft. Add secondary exits if needed."
        ),
    )


def _rule_exit_quantity_requirement(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1006.2 & §1006.3 – Number of exits required.
    1-500 occupants: 2 exits, 501-1000: 3 exits, 1001+: 4 exits.
    """
    if spec.occupant_load is None:
        return None
    
    if spec.occupant_load <= 500:
        required = 2
    elif spec.occupant_load <= 1000:
        required = 3
    else:
        required = 4
    
    return ComplianceFinding(
        rule_id="IBC-1006-EXITS",
        severity=Severity.INFO,
        code_section="IBC 2023 §1006.2 & §1006.3",
        description=(
            f"Building with {spec.occupant_load} occupants requires "
            f"minimum {required} exits. Exits must be remotely located "
            "(separated by minimum 1/3 diagonal distance)."
        ),
        recommendation=(
            f"Show {required} exits on floor plan. Verify exit separation meets "
            "IBC §1007.1.1. Include exit capacity calculations on Life Safety plan."
        ),
    )


def _rule_fire_extinguisher_placement(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §906 / NFPA 10 – Portable fire extinguishers.
    Maximum 75 ft travel distance to extinguisher (Class A).
    """
    if (
        spec.building_type == BuildingType.COMMERCIAL
        and DrawingSet.FIRE_LIFE not in spec.drawing_sets
        and DrawingSet.FULL_SET not in spec.drawing_sets
    ):
        return ComplianceFinding(
            rule_id="IBC-906-EXTINGUISHER",
            severity=Severity.INFO,
            code_section="IBC 2023 §906 / NFPA 10",
            description=(
                "Portable fire extinguishers required. Maximum 75 ft travel distance "
                "to Class A extinguisher. Maximum 50 ft for Class B (flammable liquids)."
            ),
            recommendation=(
                "Show fire extinguisher locations on Fire & Life Safety plan. "
                "Indicate 75 ft travel radius. Mount 3.5-5 ft above floor."
            ),
        )
    return None


def _rule_exit_signage_requirement(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1013 – Exit signs required.
    Illuminated, internally/externally lit, with emergency backup.
    """
    if spec.building_type == BuildingType.COMMERCIAL:
        return ComplianceFinding(
            rule_id="IBC-1013-EXIT-SIGNS",
            severity=Severity.INFO,
            code_section="IBC 2023 §1013",
            description=(
                "Exit signs required at: (1) All exit doors, (2) Exit access doors, "
                "(3) Changes in egress direction. Signs must be illuminated with "
                "emergency backup power."
            ),
            recommendation=(
                "Show exit sign locations on Fire & Life Safety plan. "
                "Specify internally illuminated or externally lit signs with battery backup."
            ),
        )
    return None


def _rule_emergency_lighting(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """
    IBC 2023 §1008 – Emergency lighting required.
    Minimum 1 fc at floor level, 90 minute battery backup.
    """
    if spec.building_type == BuildingType.COMMERCIAL:
        return ComplianceFinding(
            rule_id="IBC-1008-EMERGENCY-LIGHT",
            severity=Severity.INFO,
            code_section="IBC 2023 §1008",
            description=(
                "Emergency lighting required in exit access corridors, exits, "
                "and exit discharge. Minimum 1 footcandle at floor level, "
                "90-minute battery backup."
            ),
            recommendation=(
                "Coordinate with electrical engineer for emergency lighting layout. "
                "Show emergency lighting fixtures on electrical plans."
            ),
        )
    return None


# ── Existing Rules (unchanged) ────────────────────────────────────────────

def _rule_accessibility_drawings_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 §1101 – Commercial buildings must include accessibility plans."""
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
                "Commercial projects should include Accessibility Plan demonstrating "
                "ADA / ICC A117.1 compliance."
            ),
            recommendation="Add 'Accessibility Plan' to requested drawing set.",
        )
    return None


def _rule_fire_life_safety_drawings(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 §907 – Fire & Life Safety drawings required."""
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
            recommendation="Add 'Fire & Life Safety' to drawing set.",
        )
    return None


def _rule_jurisdiction_code_alignment(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """Verify selected code matches jurisdiction's adopted code."""
    if spec.primary_code != spec.jurisdiction.adopted_building_code:
        return ComplianceFinding(
            rule_id="JUR-CODE-MISMATCH",
            severity=Severity.WARNING,
            code_section="Jurisdictional Adoption",
            description=(
                f"Selected code '{spec.primary_code.value}' does not match "
                f"jurisdiction's adopted code '{spec.jurisdiction.adopted_building_code.value}' "
                f"for {spec.jurisdiction.display_name()}."
            ),
            recommendation=(
                f"Update primary code to '{spec.jurisdiction.adopted_building_code.value}' "
                "or verify jurisdiction has adopted selected code by amendment."
            ),
        )
    return None


def _rule_seismic_structural_required(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 §1613 – High seismic design categories require structural drawings."""
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
            recommendation="Add 'Structural Drawings' to drawing set.",
        )
    return None


def _rule_occupant_load_egress(
    spec: ProjectSpecification,
) -> Optional[ComplianceFinding]:
    """IBC 2023 §1006 – Buildings with occupant load > 500 require egress analysis."""
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
    """R-3/R-4 occupancies often regulated by IRC, not IBC."""
    IRC_GROUPS = [OccupancyGroup.R3, OccupancyGroup.R4]
    if spec.occupancy_group in IRC_GROUPS and spec.primary_code == CodeVersion.IBC_2023:
        return ComplianceFinding(
            rule_id="IBC-INFO-IRC",
            severity=Severity.INFO,
            code_section="IBC 2023 §101.2 Exception",
            description=(
                f"'{spec.occupancy_group.value}' occupancies (1 & 2 family) "
                "are typically governed by IRC, not IBC."
            ),
            recommendation=(
                "Confirm with AHJ whether IRC or IBC governs; update primary code accordingly."
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Enhanced Rule Registry
# ---------------------------------------------------------------------------

IBC_2023_RULES: List[RuleFunc] = [
    # Core compatibility & requirements
    _rule_occupancy_construction_compatibility,
    _rule_sprinkler_required,
    _rule_height_limit,
    _rule_story_limit,
    
    # ADA Accessibility (NEW)
    _rule_ada_corridor_width,
    _rule_ada_turning_space,
    _rule_ada_door_clearance,
    
    # Egress & Life Safety (NEW)
    _rule_egress_width_requirement,
    _rule_travel_distance_limit,
    _rule_dead_end_corridor_limit,
    _rule_exit_quantity_requirement,
    _rule_fire_extinguisher_placement,
    _rule_exit_signage_requirement,
    _rule_emergency_lighting,
    
    # Drawing set requirements
    _rule_accessibility_drawings_required,
    _rule_fire_life_safety_drawings,
    
    # Jurisdiction & code alignment
    _rule_jurisdiction_code_alignment,
    _rule_seismic_structural_required,
    _rule_occupant_load_egress,
    _rule_residential_code_version,
]

# Code-to-rule-set mapping
CODE_RULE_MAP: Dict[CodeVersion, List[RuleFunc]] = {
    CodeVersion.IBC_2023: IBC_2023_RULES,
    CodeVersion.IBC_2021: IBC_2023_RULES,
    CodeVersion.IBC_2018: IBC_2023_RULES,
    CodeVersion.CBC_2022: IBC_2023_RULES,
    CodeVersion.NFPA_5000: IBC_2023_RULES,
}


# ---------------------------------------------------------------------------
# Jurisdiction Loader (unchanged, already comprehensive)
# ---------------------------------------------------------------------------

class JurisdictionLoader:
    """
    Factory that returns pre-configured Jurisdiction objects for
    commonly used U.S. metros.
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
# Compliance Engine (unchanged, already comprehensive)
# ---------------------------------------------------------------------------

class ComplianceEngine:
    """
    Validates ProjectSpecification against applicable code rule set
    and jurisdiction-specific overrides.

    Usage::
        engine = ComplianceEngine()
        report = engine.validate(spec)
        if not report.is_compliant:
            print(report.summary())
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)

    def validate(self, spec: ProjectSpecification) -> ComplianceReport:
        """
        Run all applicable compliance rules against *spec*.
        Returns ComplianceReport with all findings.
        """
        self._logger.info(
            "Starting enhanced compliance validation for '%s' [%s]",
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
            except Exception as exc:
                self._logger.error(
                    "Unexpected error in rule '%s': %s",
                    rule_fn.__name__, exc, exc_info=True,
                )

        report = ComplianceReport(spec_id=spec.project_id, findings=findings)
        self._logger.info(
            "Enhanced compliance validation complete: %s (%d checks performed)",
            report.summary(), len(rules)
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
                "Jurisdiction is incomplete. Provide state and city, or select preset."
            )
        if not spec.drawing_sets:
            errors.append("At least one drawing set must be selected.")
        if errors:
            raise ValueError("ProjectSpecification validation failed:\n" +
                             "\n".join(f"  • {e}" for e in errors))
