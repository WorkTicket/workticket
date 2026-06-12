from collections.abc import Callable
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, field_validator

_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0
_MIN_HOURS = 0.0
_MAX_HOURS = 200.0
_MIN_LABOR_COST = 0.0
_MAX_LABOR_COST = 50000.0
_MAX_MATERIALS = 50


class ProcessJobRequest(BaseModel):
    job_id: UUID


class AIOutputSchema(BaseModel):
    problem_type: str = ""
    summary: str = ""
    recommended_fix: str = ""
    materials: list[str] = []
    estimated_hours: float = 0.0
    labor_cost_estimate: float = 0.0
    permit_required: bool = False
    confidence: float = 0.0
    is_fallback: bool = False
    partial_failure: bool = False

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v):
        if v < _MIN_CONFIDENCE or v > _MAX_CONFIDENCE:
            raise ValueError(f"confidence must be between {_MIN_CONFIDENCE} and {_MAX_CONFIDENCE}, got {v}")
        return v

    @field_validator("estimated_hours")
    @classmethod
    def validate_estimated_hours(cls, v):
        if v < _MIN_HOURS or v > _MAX_HOURS:
            raise ValueError(f"estimated_hours must be between {_MIN_HOURS} and {_MAX_HOURS}, got {v}")
        return v

    @field_validator("labor_cost_estimate")
    @classmethod
    def validate_labor_cost(cls, v):
        if v < _MIN_LABOR_COST or v > _MAX_LABOR_COST:
            raise ValueError(f"labor_cost_estimate must be between {_MIN_LABOR_COST} and {_MAX_LABOR_COST}, got {v}")
        return v

    @field_validator("materials")
    @classmethod
    def validate_materials_count(cls, v):
        if len(v) > _MAX_MATERIALS:
            raise ValueError(f"materials count {len(v)} exceeds max {_MAX_MATERIALS}")
        return v

    @field_validator("summary")
    @classmethod
    def validate_summary_non_empty(cls, v):
        if not v.strip():
            raise ValueError("summary cannot be empty")
        return v

    def sanitize_and_dump(
        self,
        sanitize_text: Callable[[str], str],
    ) -> dict:
        """Produce a DB-ready dict with all text fields sanitized.
        Callers MUST pass _sanitize_output_text (from app.ai.gateway) as the callback.
        This ensures no raw LLM output enters the database.
        """
        sanitized = self.model_dump()
        sanitized["summary"] = sanitize_text(self.summary)
        sanitized["recommended_fix"] = sanitize_text(self.recommended_fix)
        sanitized["problem_type"] = sanitize_text(self.problem_type)
        sanitized["materials"] = [sanitize_text(str(m)) for m in self.materials]
        return sanitized


class AIProcessResponse(BaseModel):
    job_id: UUID
    status: str
    output: AIOutputSchema | None = None


class FeedbackAction(StrEnum):
    accepted = "accepted"
    rejected = "rejected"
    modified = "modified"


class AIOutputFeedbackRequest(BaseModel):
    ai_output_id: UUID
    action: FeedbackAction
    modifications: dict | None = None


class AIOutputResponse(BaseModel):
    model_config = {"protected_namespaces": ()}
    job_id: UUID
    status: str
    output: AIOutputSchema | None = None
    model_used: str | None = None
    confidence_score: float | None = None
    system_state: str = "healthy"
    partial_failure: bool = False
    cost_estimate_usd: float = 0.0
