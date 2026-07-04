from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict
from datetime import datetime, timezone
import re

class ClaimInput(BaseModel):
    """
    Raw input payload entering the Workflow.
    """
    claim_id: str = Field(..., description="Unique identifier for the claim")
    raw_text: str = Field(..., min_length=10, description="Unstructured claim text from OCR/Email")
    source_document_hash: str = Field(..., description="SHA256 of source PDF for idempotency/audit")
    policy_limit_usd: float = Field(..., gt=0, description="Maximum coverage limit for this policy")
    budget_usd: float = Field(..., gt=0, description="FinOps safety budget limit for this workflow run")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "claim_id": "clm-2024-001",
                "raw_text": "Policy POL-12345. Claimant John Doe. Date 2024-01-15. Car repair at Shop CNPJ 12.345.678/0001-90. Estimated $5,000. Rear bumper damage.",
                "source_document_hash": "a1b2c3d4...",
                "policy_limit_usd": 10000.0,
                "budget_usd": 0.50
            }
        }
    )

class ExtractedClaimData(BaseModel):
    """
    STRICT Structured Output Target for the LLM.
    Note: extra="forbid" prevents hallucinated fields from being injected.
    """
    policy_number: str = Field(..., pattern=r"^POL-\d{4,}$", description="Policy reference number")
    policyholder_name: str = Field(..., min_length=2, max_length=100)
    claim_date: str = Field(..., description="ISO 8601 Date (YYYY-MM-DD)")
    estimated_value: float = Field(..., ge=0, description="Claim amount in USD")
    damaged_items: List[str] = Field(..., min_length=1, description="List of damaged parts/items")
    repair_shop_cnpj: str = Field(..., description="Brazilian CNPJ (XX.XXX.XXX/XXXX-XX)")
    suspicious_claim_flag: bool = Field(..., description="True if anomalies detected")

    @field_validator("repair_shop_cnpj")
    @classmethod
    def validate_cnpj_format(cls, v: str) -> str:
        numbers = re.sub(r"[^0-9]", "", v)
        if len(numbers) != 14:
            raise ValueError("CNPJ must have 14 digits")
        if not re.match(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$", v):
            raise ValueError("CNPJ must be formatted as XX.XXX.XXX/XXXX-XX")
        return v

    @field_validator("claim_date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "policy_number": "POL-12345",
                "policyholder_name": "John Doe",
                "claim_date": "2024-01-15",
                "estimated_value": 5000.00,
                "damaged_items": ["Rear Bumper"],
                "repair_shop_cnpj": "12.345.678/0001-90",
                "suspicious_claim_flag": False
            }
        }
    )

class ExtractionOutput(BaseModel):
    """
    Validation envelope to safely return data and actual cost.
    """
    extracted_data: ExtractedClaimData
    processing_cost_usd: float

class ValidatedClaimOutput(ExtractedClaimData):
    """
    Final Output after Validation and Human-in-the-Loop reviews.
    model_config sets extra to ignore here so system metadata fields don't trigger forbidden constraint validation.
    """
    validation_status: str = Field(default="PASSED", pattern="^(PASSED|FAILED|MANUAL_REVIEW)$")
    validation_errors: List[str] = Field(default_factory=list)
    failure_reason: Optional[str] = Field(default=None, description="Clear audit reason for failure or reviews")
    validated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    processing_cost_usd: float = Field(default=0.0, description="Total LLM token cost")

    model_config = ConfigDict(extra="ignore")
