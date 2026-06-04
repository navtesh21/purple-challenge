"""Structured request logging (Part C).

Every request emits one JSON line with: trace_id, store_id, endpoint, method,
status_code, latency_ms, and event_count (for ingest). A trace_id is generated per
request (or taken from an inbound X-Trace-Id) and echoed back in the response
header so a log line can be tied to a client call.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import get_settings

logger = logging.getLogger("store_intel.access")


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))  # message is already JSON
    root = logging.getLogger("store_intel")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(get_settings().log_level)
    root.propagate = False


def _store_id_from_path(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if "stores" in parts:
        i = parts.index("stores")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        request.state.trace_id = trace_id
        request.state.event_count = None  # ingest route fills this in
        start = time.perf_counter()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            record = {
                "trace_id": trace_id,
                "store_id": _store_id_from_path(request.url.path),
                "endpoint": request.url.path,
                "method": request.method,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "event_count": getattr(request.state, "event_count", None),
            }
            logger.info(json.dumps(record))
