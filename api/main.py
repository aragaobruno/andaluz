"""
Andaluz API — the bridge between the client interface and the Temporal engine.

This is intentionally thin: it does NOT reimplement any business logic.
It only translates HTTP calls (what a UI speaks) into Temporal actions
(what the Andaluz engine speaks): start workflow, list workflows,
query status, and send the human-review signal.
"""

import os
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "temporal:7233")
NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
TASK_QUEUE = os.getenv("TASK_QUEUE", "andaluz-tasks")
WORKFLOW_TYPE = "ProcessInsuranceClaimWorkflow"

app = FastAPI(title="Andaluz API", version="0.1.0")

# Allow the future front-end (any localhost port) to call this API during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# A single shared Temporal client, connected on startup.
_client: Optional[Client] = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_HOST, namespace=NAMESPACE)
    return _client


@app.on_event("startup")
async def _startup():
    # Connect eagerly so the first request isn't slow, but don't crash the API
    # if Temporal isn't ready yet — we retry lazily on each call.
    try:
        await get_client()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Request / response models (what the UI sends and receives)
# ---------------------------------------------------------------------------
class NewClaimRequest(BaseModel):
    raw_text: str = Field(..., min_length=10)
    policy_limit_usd: float = Field(..., gt=0)
    budget_usd: float = Field(default=0.50, gt=0)


class ReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(APPROVE|REJECT)$")
    reviewer_id: str = Field(default="reviewer-ui")


class ClaimSummary(BaseModel):
    workflow_id: str
    status: str            # Temporal execution status (Running / Completed / Failed)
    start_time: Optional[str] = None
    close_time: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        await get_client()
        return {"status": "healthy", "temporal": "connected"}
    except Exception as e:
        return {"status": "degraded", "temporal": f"error: {e}"}


# ---------------------------------------------------------------------------
# POST /claims — start a new claim workflow
# ---------------------------------------------------------------------------
@app.post("/claims")
async def create_claim(req: NewClaimRequest):
    client = await get_client()

    claim_id = f"clm-{uuid.uuid4()}"
    # Derive an idempotency hash from the document text (matches the engine's design).
    doc_hash = hashlib.sha256(req.raw_text.encode("utf-8")).hexdigest()[:12]
    workflow_id = f"claim-{doc_hash}-{uuid.uuid4().hex[:6]}"

    claim_input = {
        "claim_id": claim_id,
        "raw_text": req.raw_text,
        "source_document_hash": doc_hash,
        "policy_limit_usd": req.policy_limit_usd,
        "budget_usd": req.budget_usd,
    }

    try:
        handle = await client.start_workflow(
            WORKFLOW_TYPE,
            claim_input,
            id=workflow_id,
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start workflow: {e}")

    return {"workflow_id": handle.id, "claim_id": claim_id, "status": "started"}


# ---------------------------------------------------------------------------
# GET /claims — list all claim workflows
# ---------------------------------------------------------------------------
@app.get("/claims", response_model=List[ClaimSummary])
async def list_claims(limit: int = 50):
    client = await get_client()
    query = f'WorkflowType = "{WORKFLOW_TYPE}"'
    results: List[ClaimSummary] = []
    try:
        async for wf in client.list_workflows(query=query, page_size=limit):
            results.append(
                ClaimSummary(
                    workflow_id=wf.id,
                    status=wf.status.name if wf.status else "UNKNOWN",
                    start_time=wf.start_time.isoformat() if wf.start_time else None,
                    close_time=wf.close_time.isoformat() if wf.close_time else None,
                )
            )
            if len(results) >= limit:
                break
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list workflows: {e}")
    return results


# ---------------------------------------------------------------------------
# GET /claims/{workflow_id} — detail + live state via Query
# ---------------------------------------------------------------------------
@app.get("/claims/{workflow_id}")
async def get_claim(workflow_id: str):
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id)

    # Describe gives us the execution status (Running/Completed/Failed).
    try:
        desc = await handle.describe()
    except RPCError:
        raise HTTPException(status_code=404, detail="Workflow not found")

    exec_status = desc.status.name if desc.status else "UNKNOWN"

    detail = {
        "workflow_id": workflow_id,
        "execution_status": exec_status,
        "start_time": desc.start_time.isoformat() if desc.start_time else None,
        "close_time": desc.close_time.isoformat() if desc.close_time else None,
        "state": None,
        "reviewer": None,
        "accumulated_cost": None,
        "result": None,
    }

    # If still running, the Query gives us the live internal state (e.g. AWAITING_HUMAN).
    if exec_status == "RUNNING":
        try:
            state = await handle.query("get_status")
            detail["state"] = state.get("state")
            detail["reviewer"] = state.get("reviewer")
            detail["accumulated_cost"] = state.get("accumulated_cost")
        except Exception:
            pass
    else:
        # If finished, fetch the final result payload.
        try:
            detail["result"] = await handle.result()
        except Exception as e:
            detail["result"] = {"error": str(e)}

    return detail


# ---------------------------------------------------------------------------
# POST /claims/{workflow_id}/review — send the human decision signal
# ---------------------------------------------------------------------------
@app.post("/claims/{workflow_id}/review")
async def review_claim(workflow_id: str, req: ReviewRequest):
    client = await get_client()
    handle = client.get_workflow_handle(workflow_id)

    # The engine's signal expects a LIST payload: [decision, reviewer_id].
    try:
        await handle.signal("submit_human_review", [req.decision, req.reviewer_id])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to send signal: {e}")

    return {"workflow_id": workflow_id, "decision": req.decision, "status": "signal_sent"}
