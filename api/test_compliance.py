"""
test_compliance.py
==================
Unit tests for the ComplianceEngine and JurisdictionLoader.

Run with:
    python -m pytest test_compliance.py -v
    OR
    python test_compliance.py  (uses unittest discovery)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure the package root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from compliance import ComplianceEngine, JurisdictionLoader
from models import (
    BuildingType,
    CodeVersion,
    ComplianceReport,
    ConstructionType,
    DrawingSet,
    EngineProvider,
    OccupancyGroup,
    ProjectSpecification,
    Severity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(**overrides) -> ProjectSpecification:
    """Return a valid baseline commercial spec with optional overrides."""
    base = {
        "project_name": "Test Office Building",
        "building_type": BuildingType.COMMERCIAL,
        "occupancy_group": OccupancyGroup.B,
        "construction_type": ConstructionType.IIA,
        "jurisdiction": JurisdictionLoader.load("Chicago, IL"),
        "primary_code": CodeVersion.IBC_2021,
        "drawing_sets": [DrawingSet.FLOOR_PLAN, DrawingSet.ELEVATIONS],
        "engine_provider": EngineProvider.MOCK,
        "gross_sq_ft": 8_000,
        "num_stories": 3,
        "building_height_ft": 45.0,
        "sprinklered": False,
    }
    base.update(overrides)
    return ProjectSpecification(**base)


class TestJurisdictionLoader(unittest.TestCase):

    def test_available_jurisdictions_nonempty(self):
        jurisdictions = JurisdictionLoader.available_jurisdictions()
        self.assertGreater(len(jurisdictions), 0)
        self.assertIn("Chicago, IL", jurisdictions)
        self.assertIn("Los Angeles, CA", jurisdictions)

    def test_load_returns_correct_state(self):
        j = JurisdictionLoader.load("Chicago, IL")
        self.assertEqual(j.state, "IL")
        self.assertEqual(j.city, "Chicago")
        self.assertEqual(j.adopted_building_code, CodeVersion.IBC_2021)

    def test_load_los_angeles_seismic(self):
        j = JurisdictionLoader.load("Los Angeles, CA")
        self.assertEqual(j.seismic_design_category, "D")

    def test_load_invalid_raises_key_error(self):
        with self.assertRaises(KeyError):
            JurisdictionLoader.load("Narnia")

    def test_miami_wind_speed(self):
        j = JurisdictionLoader.load("Miami, FL")
        self.assertGreaterEqual(j.wind_speed_mph, 170.0)

    def test_custom_entry_available(self):
        j = JurisdictionLoader.load("Custom / Manual Entry")
        self.assertEqual(j.state, "")


class TestComplianceEnginePreValidation(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_empty_project_name_raises(self):
        spec = _make_spec(project_name="")
        with self.assertRaises(ValueError) as ctx:
            self.engine.validate(spec)
        self.assertIn("project_name", str(ctx.exception))

    def test_no_drawing_sets_raises(self):
        spec = _make_spec(drawing_sets=[])
        with self.assertRaises(ValueError) as ctx:
            self.engine.validate(spec)
        self.assertIn("drawing set", str(ctx.exception).lower())

    def test_valid_spec_does_not_raise(self):
        spec = _make_spec()
        # Should not raise
        report = self.engine.validate(spec)
        self.assertIsInstance(report, ComplianceReport)


class TestOccupancyConstructionCompatibility(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_h1_with_type_vb_is_critical(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.H1,
            construction_type=ConstructionType.VB,
            sprinklered=True,
        )
        report = self.engine.validate(spec)
        ids = [f.rule_id for f in report.findings]
        self.assertIn("IBC-504-COMPAT", ids)
        critical = [f for f in report.findings if f.rule_id == "IBC-504-COMPAT"]
        self.assertEqual(critical[0].severity, Severity.CRITICAL)

    def test_b_with_type_ia_no_compat_error(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.B,
            construction_type=ConstructionType.IA,
        )
        report = self.engine.validate(spec)
        compat_findings = [f for f in report.findings if f.rule_id == "IBC-504-COMPAT"]
        self.assertEqual(len(compat_findings), 0)


class TestSprinklerRule(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_i2_without_sprinkler_is_critical(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.I2,
            construction_type=ConstructionType.IIA,
            sprinklered=False,
        )
        report = self.engine.validate(spec)
        sprinkler_findings = [
            f for f in report.findings if f.rule_id == "IBC-903-SPRINKLER"
        ]
        self.assertGreater(len(sprinkler_findings), 0)
        self.assertEqual(sprinkler_findings[0].severity, Severity.CRITICAL)

    def test_i2_with_sprinkler_no_sprinkler_finding(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.I2,
            construction_type=ConstructionType.IIA,
            sprinklered=True,
        )
        report = self.engine.validate(spec)
        sprinkler_findings = [
            f for f in report.findings if f.rule_id == "IBC-903-SPRINKLER"
        ]
        self.assertEqual(len(sprinkler_findings), 0)

    def test_b_over_12000_sqft_without_sprinkler_is_error(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.B,
            gross_sq_ft=15_000,
            sprinklered=False,
        )
        report = self.engine.validate(spec)
        area_findings = [f for f in report.findings if f.rule_id == "IBC-903-AREA"]
        self.assertGreater(len(area_findings), 0)
        self.assertEqual(area_findings[0].severity, Severity.ERROR)

    def test_b_under_threshold_no_area_finding(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.B,
            gross_sq_ft=8_000,
            sprinklered=False,
        )
        report = self.engine.validate(spec)
        area_findings = [f for f in report.findings if f.rule_id == "IBC-903-AREA"]
        self.assertEqual(len(area_findings), 0)


class TestHeightAndStoreyLimits(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_type_vb_exceeds_height_is_error(self):
        spec = _make_spec(
            construction_type=ConstructionType.VB,
            building_height_ft=50.0,  # limit is 40 ft
            num_stories=1,
        )
        report = self.engine.validate(spec)
        height_findings = [f for f in report.findings if f.rule_id == "IBC-504-HEIGHT"]
        self.assertGreater(len(height_findings), 0)

    def test_type_ia_no_height_restriction(self):
        spec = _make_spec(
            construction_type=ConstructionType.IA,
            building_height_ft=300.0,
        )
        report = self.engine.validate(spec)
        height_findings = [f for f in report.findings if f.rule_id == "IBC-504-HEIGHT"]
        self.assertEqual(len(height_findings), 0)

    def test_type_vb_exceeds_stories_is_error(self):
        spec = _make_spec(
            construction_type=ConstructionType.VB,
            num_stories=3,  # limit is 1 for VB non-sprinklered
            building_height_ft=35.0,
        )
        report = self.engine.validate(spec)
        story_findings = [f for f in report.findings if f.rule_id == "IBC-504-STORIES"]
        self.assertGreater(len(story_findings), 0)

    def test_type_iia_within_story_limit(self):
        spec = _make_spec(
            construction_type=ConstructionType.IIA,
            num_stories=3,  # limit is 4
        )
        report = self.engine.validate(spec)
        story_findings = [f for f in report.findings if f.rule_id == "IBC-504-STORIES"]
        self.assertEqual(len(story_findings), 0)


class TestAccessibilityRule(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_commercial_missing_accessibility_warning(self):
        spec = _make_spec(
            building_type=BuildingType.COMMERCIAL,
            drawing_sets=[DrawingSet.FLOOR_PLAN],
        )
        report = self.engine.validate(spec)
        access_findings = [
            f for f in report.findings if f.rule_id == "IBC-1101-ACCESS"
        ]
        self.assertGreater(len(access_findings), 0)
        self.assertEqual(access_findings[0].severity, Severity.WARNING)

    def test_full_set_satisfies_accessibility_rule(self):
        spec = _make_spec(drawing_sets=[DrawingSet.FULL_SET])
        report = self.engine.validate(spec)
        access_findings = [
            f for f in report.findings if f.rule_id == "IBC-1101-ACCESS"
        ]
        self.assertEqual(len(access_findings), 0)

    def test_residential_no_accessibility_warning(self):
        spec = _make_spec(
            building_type=BuildingType.RESIDENTIAL,
            occupancy_group=OccupancyGroup.R2,
            drawing_sets=[DrawingSet.FLOOR_PLAN],
        )
        report = self.engine.validate(spec)
        access_findings = [
            f for f in report.findings if f.rule_id == "IBC-1101-ACCESS"
        ]
        self.assertEqual(len(access_findings), 0)


class TestJurisdictionCodeAlignment(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_mismatched_code_produces_warning(self):
        # Chicago adopts IBC_2021 but we select IBC_2023
        spec = _make_spec(primary_code=CodeVersion.IBC_2023)
        report = self.engine.validate(spec)
        mismatch = [f for f in report.findings if f.rule_id == "JUR-CODE-MISMATCH"]
        self.assertGreater(len(mismatch), 0)
        self.assertEqual(mismatch[0].severity, Severity.WARNING)

    def test_matching_code_no_mismatch(self):
        spec = _make_spec(primary_code=CodeVersion.IBC_2021)  # matches Chicago
        report = self.engine.validate(spec)
        mismatch = [f for f in report.findings if f.rule_id == "JUR-CODE-MISMATCH"]
        self.assertEqual(len(mismatch), 0)


class TestSeismicStructuralRule(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_high_sdc_without_structural_drawings_is_error(self):
        la_spec = _make_spec(
            jurisdiction=JurisdictionLoader.load("Los Angeles, CA"),
            primary_code=CodeVersion.CBC_2022,
            drawing_sets=[DrawingSet.FLOOR_PLAN],
        )
        report = self.engine.validate(la_spec)
        seismic = [f for f in report.findings if f.rule_id == "IBC-1613-SEISMIC"]
        self.assertGreater(len(seismic), 0)
        self.assertEqual(seismic[0].severity, Severity.ERROR)

    def test_high_sdc_with_structural_drawings_passes(self):
        la_spec = _make_spec(
            jurisdiction=JurisdictionLoader.load("Los Angeles, CA"),
            primary_code=CodeVersion.CBC_2022,
            drawing_sets=[DrawingSet.FLOOR_PLAN, DrawingSet.STRUCTURAL],
        )
        report = self.engine.validate(la_spec)
        seismic = [f for f in report.findings if f.rule_id == "IBC-1613-SEISMIC"]
        self.assertEqual(len(seismic), 0)

    def test_low_sdc_no_seismic_finding(self):
        spec = _make_spec(drawing_sets=[DrawingSet.FLOOR_PLAN])
        # Chicago SDC = B (not in HIGH_SDC set)
        report = self.engine.validate(spec)
        seismic = [f for f in report.findings if f.rule_id == "IBC-1613-SEISMIC"]
        self.assertEqual(len(seismic), 0)


class TestComplianceReportAggregates(unittest.TestCase):

    def setUp(self):
        self.engine = ComplianceEngine()

    def test_is_compliant_false_on_critical(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.H1,
            construction_type=ConstructionType.VB,
            sprinklered=False,
        )
        report = self.engine.validate(spec)
        self.assertFalse(report.is_compliant)

    def test_is_compliant_true_on_warnings_only(self):
        # Trigger only warnings: commercial without accessibility sheet
        spec = _make_spec(
            drawing_sets=[DrawingSet.FLOOR_PLAN],
            primary_code=CodeVersion.IBC_2021,
        )
        report = self.engine.validate(spec)
        # All findings should be warnings/info, not errors
        blocking = [f for f in report.findings if f.is_blocking()]
        if not blocking:
            self.assertTrue(report.is_compliant)

    def test_summary_contains_status(self):
        spec = _make_spec()
        report = self.engine.validate(spec)
        summary = report.summary()
        self.assertIn("PASS", summary)

    def test_blocking_count_matches(self):
        spec = _make_spec(
            occupancy_group=OccupancyGroup.H1,
            construction_type=ConstructionType.VB,
            sprinklered=False,
        )
        report = self.engine.validate(spec)
        expected = sum(1 for f in report.findings if f.is_blocking())
        self.assertEqual(report.blocking_count, expected)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
