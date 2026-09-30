"""Redis-based job store for multi-replica support."""

import asyncio
import json
import logging
import time

import redis.asyncio as redis

from orchard_env.orchestrator.job_store import Job, JobStatus
from orchard_env.orchestrator.redis_connection import (
    create_redis_client,
    redis_log_target,
)
from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)


class RedisJobStore:
    """Redis-based job storage for multi-replica deployment.

    Key schema:
    - job:{job_id} -> JSON of Job data
    - sandbox:jobs:{sandbox_id} -> Set of job IDs for a sandbox
    """

    JOB_PREFIX = "job:"
    SANDBOX_JOBS_PREFIX = "sandbox:jobs:"
    DEFAULT_TTL = 3600 * 24  # 24 hours

    def __init__(self, redis_url: str = None):
        """Initialize Redis job store.

        Args:
            redis_url: Redis connection URL
        """
        self.redis_url = redis_url or settings.redis_url
        self._client: redis.Redis | None = None
        self._completion_events: dict[str, asyncio.Event] = {}

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._client is None:
            self._client = create_redis_client(self.redis_url)
            await self._client.ping()
            logger.info(
                f"RedisJobStore connected to Redis at "
                f"{redis_log_target(self.redis_url)}"
            )

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None

    async def _ensure_connected(self) -> redis.Redis:
        """Ensure we have a Redis connection."""
        if self._client is None:
            await self.connect()
        return self._client

    def _job_to_dict(self, job: Job) -> dict:
        """Convert Job to dict for storage."""
        return {
            "job_id": job.job_id,
            "sandbox_id": job.sandbox_id,
            "command": job.command,
            "status": job.status.value,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "exit_code": job.exit_code,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "error": job.error,
        }

    def _dict_to_job(self, data: dict) -> Job:
        """Convert dict to Job."""
        return Job(
            job_id=data["job_id"],
            sandbox_id=data["sandbox_id"],
            command=data["command"],
            status=JobStatus(data["status"]),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=data.get("exit_code"),
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
        )

    async def create_job(self, job_id: str, sandbox_id: str, command: str) -> Job:
        """Create a new job."""
        client = await self._ensure_connected()

        job = Job(job_id=job_id, sandbox_id=sandbox_id, command=command)

        key = f"{self.JOB_PREFIX}{job_id}"
        sandbox_jobs_key = f"{self.SANDBOX_JOBS_PREFIX}{sandbox_id}"

        # Store job data
        await client.set(key, json.dumps(self._job_to_dict(job)), ex=self.DEFAULT_TTL)

        # Add to sandbox's job set
        await client.sadd(sandbox_jobs_key, job_id)
        await client.expire(sandbox_jobs_key, self.DEFAULT_TTL)

        logger.info(f"Created job {job_id} for sandbox {sandbox_id} in Redis")
        self._completion_events[job_id] = asyncio.Event()
        return job

    async def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        client = await self._ensure_connected()
        key = f"{self.JOB_PREFIX}{job_id}"

        data = await client.get(key)
        if data:
            return self._dict_to_job(json.loads(data))
        return None

    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        """Update job status."""
        client = await self._ensure_connected()
        key = f"{self.JOB_PREFIX}{job_id}"

        data = await client.get(key)
        if not data:
            logger.warning(f"Job {job_id} not found in Redis")
            return

        job_data = json.loads(data)
        job_data["status"] = status.value

        if stdout:
            job_data["stdout"] = stdout
        if stderr:
            job_data["stderr"] = stderr
        if exit_code is not None:
            job_data["exit_code"] = exit_code
        if error:
            job_data["error"] = error

        if status == JobStatus.RUNNING and job_data.get("started_at") is None:
            job_data["started_at"] = time.time()

        if status in [JobStatus.SUCCEEDED, JobStatus.FAILED]:
            job_data["completed_at"] = time.time()

        await client.set(key, json.dumps(job_data), ex=self.DEFAULT_TTL)
        logger.info(f"Updated job {job_id} status to {status} in Redis")

        # Notify local waiters
        if status in [JobStatus.SUCCEEDED, JobStatus.FAILED]:
            event = self._completion_events.get(job_id)
            if event:
                event.set()

    async def delete_job(self, job_id: str) -> None:
        """Delete a job."""
        client = await self._ensure_connected()
        key = f"{self.JOB_PREFIX}{job_id}"

        # Get job to find sandbox_id
        data = await client.get(key)
        if data:
            job_data = json.loads(data)
            sandbox_id = job_data.get("sandbox_id")
            if sandbox_id:
                sandbox_jobs_key = f"{self.SANDBOX_JOBS_PREFIX}{sandbox_id}"
                await client.srem(sandbox_jobs_key, job_id)

        await client.delete(key)
        self._completion_events.pop(job_id, None)
        logger.info(f"Deleted job {job_id} from Redis")

    async def get_jobs_by_sandbox(self, sandbox_id: str) -> list[Job]:
        """Get all jobs for a sandbox."""
        client = await self._ensure_connected()
        sandbox_jobs_key = f"{self.SANDBOX_JOBS_PREFIX}{sandbox_id}"

        job_ids = await client.smembers(sandbox_jobs_key)
        jobs = []

        for job_id in job_ids:
            job = await self.get_job(job_id)
            if job:
                jobs.append(job)

        return jobs

    async def cleanup_old_jobs(self, max_age_hours: int) -> int:
        """Clean up old completed jobs.

        Note: Redis TTL handles most cleanup, but this can be used
        for more aggressive cleanup of completed jobs.
        """
        client = await self._ensure_connected()
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        deleted_count = 0

        # Scan for job keys
        async for key in client.scan_iter(match=f"{self.JOB_PREFIX}*"):
            data = await client.get(key)
            if data:
                job_data = json.loads(data)
                status = job_data.get("status")
                created_at = job_data.get("created_at", 0)

                if status in ["succeeded", "failed"]:
                    age = now - created_at
                    if age > max_age_seconds:
                        job_id = job_data.get("job_id")
                        if job_id:
                            await self.delete_job(job_id)
                            deleted_count += 1

        if deleted_count:
            logger.info(f"Cleaned up {deleted_count} old jobs from Redis")

        return deleted_count

    async def wait_for_completion(
        self, job_id: str, timeout: float = 300
    ) -> Job | None:
        """Wait for a job to complete. Returns the completed Job or None."""
        # Check if already complete
        job = await self.get_job(job_id)
        if job and job.status in [JobStatus.SUCCEEDED, JobStatus.FAILED]:
            return job

        event = self._completion_events.get(job_id)
        if not event:
            return job

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass

        return await self.get_job(job_id)
