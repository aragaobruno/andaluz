from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from activities import extract_data_with_llm, validate_business_rules
from schemas import ClaimInput, ValidatedClaimOutput

@workflow.defn(name="ProcessInsuranceClaimWorkflow")
class ProcessInsuranceClaimWorkflow:
    def __init__(self) -> None:
        self._human_decision: str | None = None
        self._reviewer: str | None = None
        self._current_state: str = "PROCESSING"
        self._accumulated_cost: float = 0.0

    @workflow.signal
    async def submit_human_review(self, payload: list) -> None:
        # Ignore redundant signals to ensure review idempotency
        if self._human_decision is None:
            self._human_decision = payload[0]
            self._reviewer = payload[1]

    @workflow.query
    def get_status(self) -> dict:
        return {
            "state": self._current_state,
            "reviewer": self._reviewer,
            "accumulated_cost": self._accumulated_cost
        }

    @workflow.run
    async def run(self, claim_input: ClaimInput) -> ValidatedClaimOutput:
        workflow.logger.info(f"Workflow Started | Claim ID: {claim_input.claim_id}")

        # ---------------------------------------------------------
        # STEP 1: LLM EXTRACTION (Probabilistic)
        # ---------------------------------------------------------
        llm_retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=60),
            maximum_attempts=5,
            non_retryable_error_types=["NonRetryableError"], 
        )

        try:
            envelope = await workflow.execute_activity(
                extract_data_with_llm,
                args=[claim_input],
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=llm_retry_policy,
                heartbeat_timeout=timedelta(seconds=30),
            )
            self._accumulated_cost += envelope.processing_cost_usd
            workflow.logger.info(f"Extraction Complete | Policy Number: {envelope.extracted_data.policy_number}")

        except ActivityError as e:
            workflow.logger.error(f"Extraction Failed Permanently | Error: {e}")
            raise

        # ---------------------------------------------------------
        # FINOPS BUDGET ENFORCEMENT (Checked post-extraction step)
        # ---------------------------------------------------------
        if self._accumulated_cost > claim_input.budget_usd:
            self._current_state = "BUDGET_EXCEEDED"
            reason = f"FinOps Budget Exceeded. Spent: ${self._accumulated_cost:.5f} > Limit: ${claim_input.budget_usd:.5f}"
            return ValidatedClaimOutput(
                policy_number=envelope.extracted_data.policy_number,
                policyholder_name=envelope.extracted_data.policyholder_name,
                claim_date=envelope.extracted_data.claim_date,
                estimated_value=envelope.extracted_data.estimated_value,
                damaged_items=envelope.extracted_data.damaged_items,
                repair_shop_cnpj=envelope.extracted_data.repair_shop_cnpj,
                suspicious_claim_flag=envelope.extracted_data.suspicious_claim_flag,
                validation_status="FAILED",
                validation_errors=[reason],
                failure_reason="BUDGET_EXCEEDED",
                processing_cost_usd=self._accumulated_cost
            )

        # ---------------------------------------------------------
        # STEP 2: DETERMINISTIC VALIDATION
        # ---------------------------------------------------------
        validation_retry_policy = RetryPolicy(
            maximum_attempts=1, # Fail fast on data or rule bugs
        )

        result = await workflow.execute_activity(
            validate_business_rules,
            args=[envelope.extracted_data, claim_input.policy_limit_usd, self._accumulated_cost],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=validation_retry_policy,
        )

        # ---------------------------------------------------------
        # STEP 3: STATEFUL HUMAN-IN-THE-LOOP (HITL) ORCHESTRATION
        # ---------------------------------------------------------
        if result.validation_status == "MANUAL_REVIEW":
            self._current_state = "AWAITING_HUMAN"
            
            try:
                # Pause workflow state durably using wait_condition with 48h SLA timeout
                await workflow.wait_condition(
                    lambda: self._human_decision is not None,
                    timeout=172800.0,
                )
                
                # If we reach here, the condition became true before timeout
                if self._human_decision == "APPROVE":
                    result.validation_status = "PASSED"
                    result.validation_errors.append(f"Manually approved by {self._reviewer}")
                    result.failure_reason = None
                    self._current_state = "COMPLETED"
                else:
                    result.validation_status = "FAILED"
                    result.validation_errors.append(f"Manually rejected by {self._reviewer}")
                    result.failure_reason = "REJECTED_BY_HUMAN"
                    self._current_state = "REJECTED"
            except TimeoutError:
                result.validation_status = "FAILED"
                result.validation_errors.append("Human review SLA expired (48h)")
                result.failure_reason = "REVIEW_SLA_EXPIRED"
                self._current_state = "EXPIRED"
        else:
            self._current_state = "COMPLETED"

        workflow.logger.info(
            f"Workflow Completed | Claim ID: {claim_input.claim_id} | "
            f"Status: {result.validation_status} | State: {self._current_state} | "
            f"Cost: $ {result.processing_cost_usd:.5f}"
        )
        
        return result
