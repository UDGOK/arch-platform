"""
orchestrator.py
===============
Engine Orchestration Layer – Dispatches architectural specifications to
external generative AI engines and manages the drawing generation lifecycle.

Architecture:
  • BaseDrawingEngine  – Abstract base class (ABC) all engine adapters implement
  • ClaudeDrawingEngine  – Adapter for Anthropic Claude API
  • NvidiaDrawingEngine  – Adapter for NVIDIA Picasso / Edify
  • OpenAIDrawingEngine  – Adapter for OpenAI GPT-4o (vision + text)
  • MockDrawingEngine    – Deterministic in-process engine used for testing
  • EngineDispatcher     – Orchestrates validation → dispatch → retrieval
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from compliance import ComplianceEngine
from models import (
    ComplianceReport,
    DrawingOutput,
    DrawingSet,
    EngineProvider,
    GenerationJob,
    JobStatus,
    ProjectSpecification,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EngineTimeoutError(TimeoutError):
    """Raised when an AI engine exceeds the configured response deadline."""


class EngineAPIError(RuntimeError):
    """Raised on non-recoverable API errors (4xx/5xx responses)."""


class ComplianceBlockedError(ValueError):
    """Raised when compliance engine returns blocking findings."""


class SpecificationError(ValueError):
    """Raised when the specification payload is malformed or incomplete."""


# ---------------------------------------------------------------------------
# Prompt Builder
# ---------------------------------------------------------------------------

class PromptBuilder:
    """
    Translates a ProjectSpecification into an engine-specific prompt string.
    Each engine adapter calls the appropriate builder method.
    """

    @staticmethod
    def build_claude_prompt(spec: ProjectSpecification) -> str:
        drawing_list = ", ".join(d.value for d in spec.drawing_sets)
        local_mods = (
            "; ".join(spec.jurisdiction.local_amendments)
            if spec.jurisdiction.local_amendments
            else "None"
        )
        return (
            f"You are an expert architectural drafting AI.\n\n"
            f"Generate a detailed 2D construction drawing specification and "
            f"drawing-list manifest for the following project:\n\n"
            f"Project Name: {spec.project_name}\n"
            f"Building Type: {spec.building_type.value}\n"
            f"Occupancy Group: {spec.occupancy_group.value} "
            f"(per {spec.primary_code.value})\n"
            f"Construction Type: {spec.construction_type.value}\n"
            f"Jurisdiction: {spec.jurisdiction.display_name()}\n"
            f"Adopted Code: {spec.jurisdiction.adopted_building_code.value}\n"
            f"Local Amendments: {local_mods}\n"
            f"Seismic Design Category: "
            f"{spec.jurisdiction.seismic_design_category}\n"
            f"Wind Exposure: {spec.jurisdiction.wind_exposure_category}\n"
            f"Building Area: "
            f"{spec.gross_sq_ft:,.0f} sq ft" if spec.gross_sq_ft else "Not specified" + "\n"
            f"Stories: {spec.num_stories or 'Not specified'}\n"
            f"Building Height: "
            f"{spec.building_height_ft:.1f} ft" if spec.building_height_ft else "Not specified" + "\n"
            f"Sprinklered: {'Yes' if spec.sprinklered else 'No'}\n"
            f"Requested Drawing Sets: {drawing_list}\n"
            f"Additional Notes: {spec.additional_notes or 'None'}\n\n"
            f"For each requested drawing set, provide:\n"
            f"1. Sheet number (e.g., A1.0)\n"
            f"2. Sheet title\n"
            f"3. Key notes and code references\n"
            f"4. Required details per {spec.primary_code.value}\n\n"
            f"Respond in structured JSON format."
        )

    @staticmethod
    def build_nvidia_prompt(spec: ProjectSpecification) -> Dict[str, Any]:
        return {
            "task": "architectural_2d_drawing",
            "project_name": spec.project_name,
            "building_type": spec.building_type.value,
            "occupancy": spec.occupancy_group.value,
            "construction_type": spec.construction_type.value,
            "code": spec.primary_code.value,
            "jurisdiction": spec.jurisdiction.to_dict(),
            "drawing_sets": [d.value for d in spec.drawing_sets],
            "parameters": {
                "area_sqft": spec.gross_sq_ft,
                "stories": spec.num_stories,
                "height_ft": spec.building_height_ft,
                "sprinklered": spec.sprinklered,
            },
        }

    @staticmethod
    def build_openai_prompt(spec: ProjectSpecification) -> List[Dict[str, Any]]:
        """Returns OpenAI messages array format."""
        return [
            {
                "role": "system",
                "content": (
                    "You are a licensed architect and code consultant specializing "
                    "in IBC 2023 construction documentation. Produce precise, "
                    "code-compliant drawing manifests in JSON."
                ),
            },
            {
                "role": "user",
                "content": PromptBuilder.build_claude_prompt(spec),
            },
        ]


# ---------------------------------------------------------------------------
# Abstract Base Engine
# ---------------------------------------------------------------------------

class BaseDrawingEngine(ABC):
    """Abstract interface that every engine adapter must implement."""

    TIMEOUT_SECONDS: float = 120.0

    def __init__(self, api_key: str = "", endpoint: str = "") -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    @abstractmethod
    def provider(self) -> EngineProvider:
        """Return the EngineProvider enum value for this adapter."""

    @abstractmethod
    def generate(self, spec: ProjectSpecification) -> List[DrawingOutput]:
        """
        Dispatch the specification and return a list of DrawingOutput objects.

        Raises:
            EngineTimeoutError: if the engine exceeds TIMEOUT_SECONDS.
            EngineAPIError: on HTTP 4xx/5xx responses.
            SpecificationError: if the payload cannot be serialised.
        """

    def _http_post(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Minimal HTTP POST using only urllib (no third-party dependencies).

        Returns parsed JSON response dict.
        Raises EngineTimeoutError or EngineAPIError on failure.
        """
        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)
        if self.api_key:
            req_headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            url, data=body, headers=req_headers, method="POST"
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.TIMEOUT_SECONDS
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise EngineAPIError(
                f"HTTP {exc.code} from {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise EngineTimeoutError(
                f"Engine at {url} timed out after {self.TIMEOUT_SECONDS}s."
            ) from exc
        except Exception as exc:
            raise EngineAPIError(
                f"Unexpected error contacting {url}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Concrete Engine Adapters
# ---------------------------------------------------------------------------

class ClaudeDrawingEngine(BaseDrawingEngine):
    """
    Adapter for Anthropic Claude API.
    Uses the /v1/messages endpoint to generate drawing manifests.
    """

    ENDPOINT = "https://api.anthropic.com/v1/messages"
    MODEL = "claude-opus-4-5"

    def __init__(self, api_key: str = "") -> None:
        super().__init__(api_key=api_key, endpoint=self.ENDPOINT)

    @property
    def provider(self) -> EngineProvider:
        return EngineProvider.CLAUDE

    def generate(self, spec: ProjectSpecification) -> List[DrawingOutput]:
        prompt = PromptBuilder.build_claude_prompt(spec)
        payload: Dict[str, Any] = {
            "model": self.MODEL,
            "max_tokens": 4096,
            "system": (
                "You are an expert architectural AI. Return ONLY valid JSON "
                "with a 'drawings' array. Each element must have: "
                "sheet_number, title, sheet_type, notes, code_references."
            ),
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        self._logger.info("Dispatching to Claude API for project %s", spec.project_id)
        response = self._http_post(self.ENDPOINT, payload, headers)
        return self._parse_claude_response(response, spec)

    def _parse_claude_response(
        self, response: Dict[str, Any], spec: ProjectSpecification
    ) -> List[DrawingOutput]:
        try:
            content_blocks = response.get("content", [])
            text = next(
                (b["text"] for b in content_blocks if b.get("type") == "text"),
                "{}",
            )
            data = json.loads(text)
            drawings_raw: List[Dict[str, Any]] = data.get("drawings", [])
        except (KeyError, json.JSONDecodeError) as exc:
            raise EngineAPIError(
                f"Failed to parse Claude response: {exc}"
            ) from exc

        outputs: List[DrawingOutput] = []
        for raw in drawings_raw:
            outputs.append(
                DrawingOutput(
                    sheet_type=DrawingSet(
                        raw.get("sheet_type", DrawingSet.FLOOR_PLAN.value)
                    ),
                    sheet_number=raw.get("sheet_number", "A0.0"),
                    title=raw.get("title", "Untitled Sheet"),
                    format="JSON",
                    metadata={
                        "notes": raw.get("notes", ""),
                        "code_references": raw.get("code_references", []),
                    },
                )
            )
        return outputs


class NvidiaDrawingEngine(BaseDrawingEngine):
    """
    Adapter for NVIDIA Picasso / Edify architectural generation API.
    """

    ENDPOINT = "https://api.nvidia.com/v1/genai/architecture/draw"

    def __init__(self, api_key: str = "", endpoint: str = "") -> None:
        super().__init__(
            api_key=api_key,
            endpoint=endpoint or self.ENDPOINT,
        )

    @property
    def provider(self) -> EngineProvider:
        return EngineProvider.NVIDIA

    def generate(self, spec: ProjectSpecification) -> List[DrawingOutput]:
        payload = PromptBuilder.build_nvidia_prompt(spec)
        self._logger.info(
            "Dispatching to NVIDIA Picasso for project %s", spec.project_id
        )
        response = self._http_post(self.endpoint, payload)
        return self._parse_nvidia_response(response)

    def _parse_nvidia_response(
        self, response: Dict[str, Any]
    ) -> List[DrawingOutput]:
        outputs: List[DrawingOutput] = []
        for item in response.get("artifacts", []):
            outputs.append(
                DrawingOutput(
                    sheet_type=DrawingSet.FLOOR_PLAN,   # map from API field
                    sheet_number=item.get("id", "X0.0"),
                    title=item.get("label", "Generated Sheet"),
                    format=item.get("format", "PNG"),
                    url=item.get("url"),
                    metadata=item,
                )
            )
        return outputs


class OpenAIDrawingEngine(BaseDrawingEngine):
    """Adapter for OpenAI GPT-4o drawing manifest generation."""

    ENDPOINT = "https://api.openai.com/v1/chat/completions"
    MODEL = "gpt-4o"

    def __init__(self, api_key: str = "") -> None:
        super().__init__(api_key=api_key, endpoint=self.ENDPOINT)

    @property
    def provider(self) -> EngineProvider:
        return EngineProvider.OPENAI

    def generate(self, spec: ProjectSpecification) -> List[DrawingOutput]:
        messages = PromptBuilder.build_openai_prompt(spec)
        payload: Dict[str, Any] = {
            "model": self.MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": 4096,
        }
        self._logger.info(
            "Dispatching to OpenAI GPT-4o for project %s", spec.project_id
        )
        response = self._http_post(self.endpoint, payload)
        try:
            content = response["choices"][0]["message"]["content"]
            data = json.loads(content)
            drawings_raw: List[Dict[str, Any]] = data.get("drawings", [])
        except (KeyError, json.JSONDecodeError) as exc:
            raise EngineAPIError(f"Failed to parse OpenAI response: {exc}") from exc

        return [
            DrawingOutput(
                sheet_type=DrawingSet.FLOOR_PLAN,
                sheet_number=r.get("sheet_number", "A0.0"),
                title=r.get("title", "Sheet"),
                format="JSON",
                metadata=r,
            )
            for r in drawings_raw
        ]


class MockDrawingEngine(BaseDrawingEngine):
    """
    Deterministic in-process engine for development and unit tests.
    Generates placeholder DrawingOutput objects without network calls.
    """

    @property
    def provider(self) -> EngineProvider:
        return EngineProvider.MOCK

    def generate(self, spec: ProjectSpecification) -> List[DrawingOutput]:
        self._logger.info(
            "MockEngine: generating %d drawing(s) for project '%s'",
            len(spec.drawing_sets), spec.project_name,
        )
        # Simulate processing time
        time.sleep(0.05)
        outputs: List[DrawingOutput] = []
        sheet_counter = 1
        for drawing_set in spec.drawing_sets:
            prefix = self._sheet_prefix(drawing_set)
            outputs.append(
                DrawingOutput(
                    sheet_type=drawing_set,
                    sheet_number=f"{prefix}{sheet_counter}.0",
                    title=f"{drawing_set.value} – {spec.project_name}",
                    format="SVG",
                    data=self._mock_svg(drawing_set, spec),
                    metadata={
                        "engine": "MockDrawingEngine",
                        "code": spec.primary_code.value,
                        "jurisdiction": spec.jurisdiction.display_name(),
                        "generated_at": datetime.utcnow().isoformat(),
                    },
                )
            )
            sheet_counter += 1
        return outputs

    @staticmethod
    def _sheet_prefix(drawing_set: DrawingSet) -> str:
        prefixes = {
            DrawingSet.SITE: "C",
            DrawingSet.FLOOR_PLAN: "A",
            DrawingSet.ELEVATIONS: "A",
            DrawingSet.SECTIONS: "A",
            DrawingSet.DETAILS: "A",
            DrawingSet.STRUCTURAL: "S",
            DrawingSet.MEP: "M",
            DrawingSet.FIRE_LIFE: "FP",
            DrawingSet.ACCESSIBILITY: "AC",
            DrawingSet.FULL_SET: "A",
        }
        return prefixes.get(drawing_set, "X")

    @staticmethod
    def _mock_svg(drawing_set: DrawingSet, spec: ProjectSpecification) -> bytes:
        label = drawing_set.value.upper()
        project = spec.project_name[:40]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600">'
            f'<rect width="800" height="600" fill="#f5f5f5" stroke="#333" '
            f'stroke-width="2"/>'
            f'<text x="400" y="280" text-anchor="middle" '
            f'font-family="monospace" font-size="28" fill="#222">{label}</text>'
            f'<text x="400" y="320" text-anchor="middle" '
            f'font-family="monospace" font-size="16" fill="#555">{project}</text>'
            f'<text x="400" y="350" text-anchor="middle" '
            f'font-family="monospace" font-size="12" fill="#888">'
            f'{spec.primary_code.value} | {spec.jurisdiction.display_name()}'
            f'</text>'
            f'</svg>'
        )
        return svg.encode("utf-8")


# ---------------------------------------------------------------------------
# Engine Registry
# ---------------------------------------------------------------------------

class EngineRegistry:
    """Provides engine instances by EngineProvider enum value."""

    _engines: Dict[EngineProvider, BaseDrawingEngine] = {
        EngineProvider.MOCK: MockDrawingEngine(),
    }

    @classmethod
    def register(cls, engine: BaseDrawingEngine) -> None:
        """Register or replace an engine adapter."""
        cls._engines[engine.provider] = engine
        logger.debug("Registered engine: %s", engine.provider.value)

    @classmethod
    def get(cls, provider: EngineProvider) -> BaseDrawingEngine:
        """Retrieve a registered engine. Raises KeyError if not found."""
        if provider not in cls._engines:
            raise KeyError(
                f"Engine '{provider.value}' is not registered. "
                f"Available: {[e.value for e in cls._engines]}"
            )
        return cls._engines[provider]

    @classmethod
    def available(cls) -> List[EngineProvider]:
        return list(cls._engines.keys())


# ---------------------------------------------------------------------------
# Engine Dispatcher
# ---------------------------------------------------------------------------

class EngineDispatcher:
    """
    Orchestrates the full drawing generation lifecycle:
      1. Pre-validate the specification
      2. Run compliance checks (blocking on ERROR/CRITICAL findings)
      3. Retrieve the appropriate engine adapter
      4. Dispatch and capture drawing outputs
      5. Return a completed (or failed) GenerationJob

    Usage::

        dispatcher = EngineDispatcher()
        job = dispatcher.dispatch(spec)
        if job.status == JobStatus.COMPLETED:
            for drawing in job.drawings:
                print(drawing.sheet_number, drawing.title)
    """

    def __init__(
        self,
        compliance_engine: Optional[ComplianceEngine] = None,
        engine_registry: Optional[EngineRegistry] = None,
    ) -> None:
        self._compliance = compliance_engine or ComplianceEngine()
        self._registry = engine_registry or EngineRegistry()
        self._logger = logging.getLogger(self.__class__.__name__)

    def dispatch(self, spec: ProjectSpecification) -> GenerationJob:
        """
        Full lifecycle dispatch.

        Returns a GenerationJob in COMPLETED or FAILED status.
        Never raises; all errors are captured into job.error_message.
        """
        job = GenerationJob(
            spec=spec,
            engine_provider=spec.engine_provider,
        )
        job.status = JobStatus.VALIDATING
        self._logger.info(
            "Job %s created for project '%s' using %s",
            job.job_id, spec.project_name, spec.engine_provider.value,
        )

        # ── Step 1: Compliance validation ─────────────────────────────────
        try:
            report: ComplianceReport = self._compliance.validate(spec)
            job.compliance_report = report
        except ValueError as exc:
            self._logger.error("Specification pre-validation failed: %s", exc)
            job.mark_failed(f"Specification error: {exc}")
            return job

        if not report.is_compliant:
            blocking = [
                f"[{f.severity.value}] {f.code_section}: {f.description}"
                for f in report.findings
                if f.is_blocking()
            ]
            msg = (
                f"Compliance check FAILED with {report.blocking_count} "
                f"blocking finding(s):\n" + "\n".join(blocking)
            )
            self._logger.warning(msg)
            job.mark_failed(msg)
            return job

        self._logger.info("Compliance: PASS (%d warnings)", report.warning_count)

        # ── Step 2: Retrieve engine ────────────────────────────────────────
        try:
            engine = EngineRegistry.get(spec.engine_provider)
        except KeyError as exc:
            self._logger.error("Engine lookup failed: %s", exc)
            job.mark_failed(str(exc))
            return job

        # ── Step 3: Dispatch ───────────────────────────────────────────────
        job.mark_dispatched()
        self._logger.info(
            "Dispatching job %s to %s", job.job_id, engine.provider.value
        )

        try:
            drawings = engine.generate(spec)
            job.mark_completed(drawings)
            self._logger.info(
                "Job %s completed: %d drawing(s) generated in %.2fs",
                job.job_id,
                len(drawings),
                job.duration_seconds() or 0.0,
            )
        except EngineTimeoutError as exc:
            self._logger.error("Engine timeout for job %s: %s", job.job_id, exc)
            job.mark_failed(f"Engine timeout: {exc}")
        except EngineAPIError as exc:
            self._logger.error("Engine API error for job %s: %s", job.job_id, exc)
            job.mark_failed(f"API error: {exc}")
        except Exception as exc:
            self._logger.exception(
                "Unexpected error during dispatch for job %s", job.job_id
            )
            job.mark_failed(f"Unexpected error: {exc}")

        return job
