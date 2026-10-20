"""Bounded teardown for POSTs rejected before their body is consumed."""
from __future__ import annotations

import socket
import time


MAX_DRAIN_BYTES = 4 * 1024 * 1024
DRAIN_SECONDS = .5


def request_length(handler, limit: int) -> int:
    """Require unambiguous fixed-length framing on bounded POST endpoints."""
    values = handler.headers.get_all('Content-Length', [])
    if len(values) != 1 or handler.headers.get('Transfer-Encoding') is not None:
        raise ValueError('请求长度缺失、重复或编码不受支持。')
    value = values[0].strip()
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ValueError('请求长度不正确。')
    length = int(value)
    if not 0 < length <= limit:
        raise ValueError('请求过大或为空。')
    return length


def reject_json(handler, payload, *, status: int) -> None:
    """Send the rejection before waiting for a body that may never arrive.

    Half-close the response, then discard at most 4 MiB for at most .5 s.
    The client can read the final response and close without completing its
    upload. Never parse discarded bytes, reuse this connection, or dispatch
    a pipelined request. Above either bound, resource limits take priority.
    """
    handler.close_connection = True
    deadline = time.monotonic() + DRAIN_SECONDS
    try:
        handler.connection.settimeout(DRAIN_SECONDS)
        handler._respond_json(payload, status=status, close=True)
        handler.wfile.flush()
        handler.connection.shutdown(socket.SHUT_WR)
        remaining = MAX_DRAIN_BYTES
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            handler.connection.settimeout(timeout)
            chunk = handler.rfile.read1(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        # A timed-out/incomplete upload or disconnected peer is already denied.
        pass
