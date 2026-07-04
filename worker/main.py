import os
import asyncio
import logging
from datetime import timedelta
from temporalio.client import Client
from temporalio.worker import Worker, UnsandboxedWorkflowRunner

from workflows import ProcessInsuranceClaimWorkflow
from activities import extract_data_with_llm, validate_business_rules

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logger = logging.getLogger("andaluz-worker")

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost:7233")
TASK_QUEUE = "andaluz-tasks"
NAMESPACE = "default"

async def main():
    logger.info("Starting Andaluz Worker...", extra={"temporal_host": TEMPORAL_HOST, "task_queue": TASK_QUEUE})

    client = await Client.connect(TEMPORAL_HOST, namespace=NAMESPACE)
    logger.info("Connected to Temporal Cluster")

    # Native Temporal SIGINT/SIGTERM handlers handle shutdown events cleanly
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProcessInsuranceClaimWorkflow],
        activities=[extract_data_with_llm, validate_business_rules],
        max_concurrent_workflow_tasks=50,
        max_concurrent_activities=20,
        graceful_shutdown_timeout=timedelta(seconds=30),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )

    logger.info("Worker running. Polling task queue '%s'...", TASK_QUEUE)
    await worker.run()
    logger.info("Worker stopped gracefully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Exiting.")
