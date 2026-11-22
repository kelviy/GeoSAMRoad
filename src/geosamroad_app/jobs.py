"""A job registry that lives in this process.

Deliberately not Redis/RQ: a single box running one GPU model does not need a
broker, and dropping it removes two services from the deployment. One worker
thread at a time, because the GPU is the bottleneck -- concurrent jobs would
just fight over VRAM.
"""
import logging
import threading
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MAX_JOBS_RETAINED = 50


class Job:
    """One fetch+infer run, observable while it works."""

    def __init__(self, tile_ids, variant, date):
        self.id = uuid.uuid4().hex[:12]
        self.tile_ids = list(tile_ids)
        self.variant = variant
        self.date = date
        self.status = "queued"          # queued | running | completed | failed
        self.message = "Waiting for the worker"
        self.done = 0
        self.total = len(tile_ids)
        self.result = None
        self.error = None
        self.created_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self, include_result=True):
        payload = {
            "job_id": self.id,
            "status": self.status,
            "message": self.message,
            "done": self.done,
            "total": self.total,
            "variant": self.variant,
            "tile_ids": self.tile_ids,
            "created_at": self.created_at,
            "error": self.error,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class JobRegistry:
    """Thread-safe job store with a single serialised worker."""

    def __init__(self):
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self):
        with self._lock:
            return [self._jobs[j].as_dict(include_result=False)
                    for j in reversed(self._order)]

    def busy(self):
        """True when a job currently holds the worker."""
        return self._worker_lock.locked()

    def submit(self, tile_ids, variant, date, run_fn):
        """Register a job and start it on a background thread."""
        job = Job(tile_ids, variant, date)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > MAX_JOBS_RETAINED:
                self._jobs.pop(self._order.pop(0), None)

        threading.Thread(target=self._run, args=(job, run_fn), daemon=True).start()
        return job

    def _run(self, job, run_fn):
        # Serialised: the second job waits here rather than contending for VRAM.
        with self._worker_lock:
            job.status = "running"
            job.message = "Starting"

            def progress(done, total, message):
                job.done, job.total, job.message = done, total, message

            try:
                job.result = run_fn(job, progress)
                job.status = "completed"
                job.message = (
                    f"{job.result['total_edges']} road segments across "
                    f"{job.result['tiles_processed']} tile(s)"
                )
                job.done = job.total
            except Exception as exc:
                log.exception("Job %s failed", job.id)
                job.status = "failed"
                job.error = str(exc)
                job.message = "Failed"


registry = JobRegistry()
