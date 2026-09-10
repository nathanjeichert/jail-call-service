"""Per-job progress events for the SSE stream.

The pipeline runs on its own event loop in a background thread while the
API server streams progress to the browser, so events cross threads through
a bounded stdlib queue per job. Producers never block: if the browser falls
2,000 events behind, the oldest updates are simply dropped (the UI re-syncs
from the job record on reconnect).

Event shapes (``type`` plus fields):
  stage        {"stage": str, "total"?: int}
  discovered   {"count": int}
  call_update  {"index": int, "status": str, "stage": str, "completed": int, "total": int}
  warning      {"message": str}
  packaged     {"zip_path": str}
  done         {"zip_path": str}
  error        {"message": str}
"""

import queue
import threading
from typing import Dict

_MAX_QUEUED_EVENTS = 2000

_queues: Dict[str, queue.Queue] = {}
_lock = threading.Lock()


def get_event_queue(job_id: str) -> queue.Queue:
    with _lock:
        if job_id not in _queues:
            _queues[job_id] = queue.Queue(maxsize=_MAX_QUEUED_EVENTS)
        return _queues[job_id]


def cleanup_event_queue(job_id: str) -> None:
    """Drop a finished job's queue so idle jobs don't hold memory."""
    with _lock:
        _queues.pop(job_id, None)


def emit(job_id: str, event: dict) -> None:
    """Put an event on the job's queue (non-blocking, thread-safe)."""
    try:
        get_event_queue(job_id).put_nowait(event)
    except queue.Full:
        pass
