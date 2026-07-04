import sys
import os
import asyncio
import uuid
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

# Resolve local imports boundary dynamically
sys.path.append(os.path.join(os.path.dirname(__file__), "worker"))
from schemas import ClaimInput

async def run_test():
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))
    
    scenarios = [
        # Scenario A: Manual Review Triggered (Estimated cost 7500 > Policy Limit 5000)
        {
            "name": "Scenario A: Manual Review approval",
            "input": ClaimInput(
                claim_id=f"clm-{uuid.uuid4()}",
                raw_text="""
                Claim request under Policy POL-556677. Claimant Name: Maria Silva. 
                Claim Date: 2024-05-20. Workshop: Auto Oficina Real, CNPJ: 11.222.333/0001-81. 
                Damage description: Full radiator repair. Estimated cost is $7,500.00.
                """,
                source_document_hash="doc-hash-556677",
                policy_limit_usd=5000.00,
                budget_usd=0.50
            ),
            "simulate_review": True,
            "approve": True
        },
        # Scenario B: Future Date Rejection (Automatic validation reject)
        {
            "name": "Scenario B: Future Date Rejection",
            "input": ClaimInput(
                claim_id=f"clm-{uuid.uuid4()}",
                raw_text="""
                Claim request under Policy POL-992211. Claimant Name: John Doe. 
                Claim Date: 2029-08-10. Workshop: Fix Auto, CNPJ: 22.333.444/0001-81. 
                Damage description: Windshield replacement costing $450.00.
                """,
                source_document_hash="doc-hash-992211",
                policy_limit_usd=2000.00,
                budget_usd=0.50
            ),
            "simulate_review": False,
            "approve": False
        },
        # Scenario C: FinOps Budget Exceeded (Tiny budget limit)
        {
            "name": "Scenario C: FinOps Budget Exceeded Fail",
            "input": ClaimInput(
                claim_id=f"clm-{uuid.uuid4()}",
                raw_text="""
                Claim request under Policy POL-121212. Claimant Name: Bob Smith. 
                Claim Date: 2024-02-10. Workshop: Fix Auto, CNPJ: 22.333.444/0001-81. 
                Damage description: Mirror replacement costing $250.00.
                """,
                source_document_hash="doc-hash-121212",
                policy_limit_usd=1000.00,
                budget_usd=0.000001  # Extremely low budget to trigger guardrail
            ),
            "simulate_review": False,
            "approve": False
        }
    ]

    for scenario in scenarios:
        claim = scenario["input"]
        print(f"\n🚀 Running: {scenario['name']} | Claim ID: {claim.claim_id}")
        
        # Idempotency requirement: Use document hash as unique workflow identity.
        # Set REJECT_DUPLICATE policy to enforce idempotency at temporal server engine boundaries.
        workflow_id = f"claim-{claim.source_document_hash}-{uuid.uuid4().hex[:6]}"
        
        try:
            handle = await client.start_workflow(
                "ProcessInsuranceClaimWorkflow",
                claim,
                id=workflow_id,
                task_queue="ark-tasks",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE
            )
            print(f"Workflow ID: {handle.id} execution started.")
        except Exception as e:
            print(f"❌ Idempotency Protection triggered or workflow start error: {e}")
            continue
        
        # Simulate human signal input post-delay
        if scenario["simulate_review"]:
            await asyncio.sleep(4)  # Simulate worker processing interval
            
            # Query state info
            status = await handle.query("get_status")
            print(f"Current Workflow State (Query): {status['state']}")
            
            if status['state'] == "AWAITING_HUMAN":
                decision = "APPROVE" if scenario["approve"] else "REJECT"
                print(f"📣 Sending manual review signal: {decision}...")
                await handle.signal("submit_human_review", [decision, "reviewer-user-99"])
        
        result_raw = await handle.result()
        from schemas import ValidatedClaimOutput
        result = ValidatedClaimOutput.model_validate(result_raw) if isinstance(result_raw, dict) else result_raw
        print(f"🏁 Final Result: {result.validation_status} (Reason: {result.failure_reason})")
        print(f"Errors Logged: {result.validation_errors}")
        print(f"Actual Cost Logged: ${result.processing_cost_usd:.5f}")

if __name__ == "__main__":
    asyncio.run(run_test())
