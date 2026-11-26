"""Optional self-hosted Temporal dispatch for the existing desktop runtime.

Workflow state lives in Temporal. Payloads, memory and acceptance state remain
local. Non-idempotent whole-agent activities deliberately have no automatic
retry: an interrupted external action requires its adapter's reconciliation.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker


@workflow.defn
class DesktopJobWorkflow:
    @workflow.run
    async def run(self, job_id: str, timeout_seconds: int) -> dict:
        return await workflow.execute_activity(
            "openvarak.execute_job",
            job_id,
            start_to_close_timeout=timedelta(seconds=timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=1),
            result_type=dict,
        )


class TemporalDispatcher:
    def __init__(self, runtime, config, *, client=None):
        self.runtime, self.config = runtime, config
        self.client = client
        self.worker = None
        self._task = None

    @activity.defn(name="openvarak.execute_job")
    async def execute_job(self, job_id: str) -> dict:
        return await self.runtime._execute_job(job_id)

    async def start(self):
        if self.client is None:
            self.client = await Client.connect(
                self.config.temporal_address, namespace=self.config.temporal_namespace
            )
        self.worker = Worker(
            self.client,
            task_queue=self.config.temporal_task_queue,
            workflows=[DesktopJobWorkflow],
            activities=[self.execute_job],
            max_concurrent_activities=1,
        )
        self._task = asyncio.create_task(self.worker.run())
        await asyncio.sleep(0)
        if self._task.done():
            self._task.result()

    async def dispatch(self, job_id):
        workflow_id = "openvarak-job-" + job_id
        try:
            handle = await self.client.start_workflow(
                DesktopJobWorkflow.run,
                args=[job_id, self.config.temporal_activity_timeout_seconds],
                id=workflow_id,
                task_queue=self.config.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            handle = self.client.get_workflow_handle(workflow_id)
        return await handle.result()

    async def close(self):
        if self.worker is not None:
            await self.worker.shutdown()
        if self._task is not None:
            await self._task
