import os
# Disable system proxy detection to prevent httpx/openai client initialization issues
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

import logging
from datetime import datetime, timezone
from temporalio import activity
from temporalio.exceptions import ApplicationError
from openai import AsyncOpenAI, APITimeoutError, RateLimitError, APIStatusError
from pydantic import ValidationError

from schemas import ClaimInput, ExtractedClaimData, ValidatedClaimOutput, ExtractionOutput

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

LITELLM_BASE_URL = os.getenv("LITELLM_API_BASE", "http://litellm:4000/v1")
LITELLM_API_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-andaluz-master-key-dev")
PRIMARY_MODEL = "ark-fast"

llm_client = AsyncOpenAI(
    base_url=LITELLM_BASE_URL,
    api_key=LITELLM_API_KEY,
    timeout=60.0,
    max_retries=0
)

def validate_cnpj_checksum(cnpj: str) -> bool:
    numbers = [int(d) for d in cnpj if d.isdigit()]
    if len(numbers) != 14: return False
    
    weights_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    
    def calc_digit(nums, weights):
        s = sum(n * w for n, w in zip(nums, weights))
        r = s % 11
        return 0 if r < 2 else 11 - r

    d1 = calc_digit(numbers[:12], weights_1)
    d2 = calc_digit(numbers[:12] + [d1], weights_2)
    
    return numbers[12] == d1 and numbers[13] == d2

@activity.defn(name="extract_data_with_llm")
async def extract_data_with_llm(claim_input: ClaimInput) -> ExtractionOutput:
    activity.heartbeat("Starting LLM Extraction...")
    logger.info("LLM Extraction started", extra={"claim_id": claim_input.claim_id, "model": PRIMARY_MODEL})
    
    # NUNCA logar raw_text em produção para compliance com LGPD/GDPR (PII protection)

    system_prompt = """
You are an expert Insurance Claims Structurer. Extract structured data from the raw claim text.
Adhere STRICTLY to the following JSON Schema:

{
  "type": "object",
  "properties": {
    "policy_number": {
      "type": "string",
      "pattern": "^POL-\\\\d{4,}$",
      "description": "Policy reference number, format: POL-XXXX"
    },
    "policyholder_name": {
      "type": "string",
      "description": "Full name of the policyholder"
    },
    "claim_date": {
      "type": "string",
      "description": "ISO 8601 Date (YYYY-MM-DD)"
    },
    "estimated_value": {
      "type": "number",
      "description": "Claim amount in USD"
    },
    "damaged_items": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "List of damaged parts/items"
    },
    "repair_shop_cnpj": {
      "type": "string",
      "description": "Brazilian CNPJ formatted as XX.XXX.XXX/XXXX-XX"
    },
    "suspicious_claim_flag": {
      "type": "boolean",
      "description": "True if anomalies are detected"
    }
  },
  "required": [
    "policy_number",
    "policyholder_name",
    "claim_date",
    "estimated_value",
    "damaged_items",
    "repair_shop_cnpj",
    "suspicious_claim_flag"
  ],
  "additionalProperties": false
}

Rules:
1. Policy Number format: POL-XXXX (at least 4 digits).
2. Date format: YYYY-MM-DD.
3. CNPJ format: XX.XXX.XXX/XXXX-XX (Brazilian).
4. If data is missing, infer 'suspicious_claim_flag': true.
5. Output ONLY valid JSON matching the schema.
"""

    user_prompt = f"""
Claim ID: {claim_input.claim_id}
Raw Text: {claim_input.raw_text}
Policy Limit: ${claim_input.policy_limit_usd:,.2f}
"""

    try:
        # Request with raw response wrapper to read LiteLLM cost tracking headers
        raw_response = await llm_client.chat.completions.with_raw_response.create(
            model=PRIMARY_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        response = raw_response.parse()
        raw_json = response.choices[0].message.content
        logger.info(f"LLM Raw Output: {raw_json}")
        if not raw_json:
            raise ApplicationError("LLM returned empty content", type="RetryableActivityError")

        extracted_data = ExtractedClaimData.model_validate_json(raw_json)
        
        # Read exact cost calculated by LiteLLM gateway with fallback logic
        cost_header = raw_response.headers.get("x-litellm-response-cost")
        if cost_header is None:
            cost_header = getattr(response, "_hidden_params", {}).get("response_cost")
        cost = float(cost_header) if cost_header else 0.0

        logger.info("LLM Extraction successful", extra={
            "claim_id": claim_input.claim_id, 
            "tokens_in": response.usage.prompt_tokens if response.usage else 0,
            "tokens_out": response.usage.completion_tokens if response.usage else 0,
            "est_cost": cost
        })
        
        return ExtractionOutput(extracted_data=extracted_data, processing_cost_usd=cost)

    except ValidationError as e:
        logger.error("LLM Schema Validation Failed", extra={"claim_id": claim_input.claim_id, "errors": e.errors()})
        raise ApplicationError(f"LLM Output Schema Violation: {e}", type="NonRetryableError", non_retryable=True)
    
    except (APITimeoutError, RateLimitError) as e:
        logger.warning("LLM Gateway Transient Error", extra={"claim_id": claim_input.claim_id, "error": str(e)})
        raise ApplicationError(f"LLM Gateway Error: {e}", type="RetryableActivityError")
    
    except APIStatusError as e:
        if e.status_code >= 500:
            raise ApplicationError(f"Model Provider 5xx: {e}", type="RetryableActivityError")
        logger.error("LLM Gateway Client Error", extra={"status": e.status_code, "body": e.response.text})
        raise ApplicationError(f"LLM Gateway Client Error {e.status_code}: {e.message}", type="NonRetryableError", non_retryable=True)

    except Exception as e:
        logger.exception("Unexpected LLM Activity Error")
        raise ApplicationError(f"Unexpected: {e}", type="RetryableActivityError")

@activity.defn(name="validate_business_rules")
async def validate_business_rules(extracted: ExtractedClaimData, policy_limit: float, cost: float) -> ValidatedClaimOutput:
    activity.heartbeat("Running Business Rules Validation...")
    logger.info("Validation started", extra={"policy_number": extracted.policy_number})

    errors = []
    status = "PASSED"
    reason = None

    # 1. CNPJ Checksum Verification (Hard Failure)
    if not validate_cnpj_checksum(extracted.repair_shop_cnpj):
        errors.append(f"Invalid CNPJ Checksum: {extracted.repair_shop_cnpj}")
        status = "FAILED"
        reason = "INVALID_TAX_ID"

    # 2. Future Date Verification (Hard Failure)
    claim_dt = datetime.strptime(extracted.claim_date, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    if claim_dt > today:
        errors.append(f"Claim date {extracted.claim_date} is in the future")
        status = "FAILED"
        reason = "FUTURE_CLAIM_DATE"

    # 3. Policy Limit Verification (Only evaluate if hard failures haven't triggered)
    if status != "FAILED" and extracted.estimated_value > policy_limit:
        errors.append(f"Estimated value ${extracted.estimated_value:,.2f} exceeds policy limit ${policy_limit:,.2f}")
        status = "MANUAL_REVIEW"
        reason = "EXCEEDS_POLICY_LIMIT"

    output = ValidatedClaimOutput(
        **extracted.model_dump(),
        validation_status=status,
        validation_errors=errors,
        failure_reason=reason,
        validated_at=datetime.now(timezone.utc).isoformat(),
        processing_cost_usd=cost
    )

    logger.info("Validation complete", extra={
        "policy_number": extracted.policy_number, 
        "status": status, 
        "error_count": len(errors)
    })
    
    return output
