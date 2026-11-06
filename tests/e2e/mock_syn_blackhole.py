"""Silent SYN loss on one fixture-owned socket; no host firewall changes."""

import ctypes
import json
from pathlib import Path
import socket
import threading

EVENT_PREFIX = "SYN_FIXTURE "
PHASES = frozenset({"socket", "bind", "filter", "listen", "marker", "waiting"})
STATUSES = frozenset({"started", "passed", "failed", "interrupted"})
ERROR_TYPES = frozenset({"OSError", "PermissionError", "FileNotFoundError", "RuntimeError",
                         "TypeError", "ValueError", "AttributeError", "OverflowError",
                         "ArgumentError", "KeyboardInterrupt", "SystemExit"})


def emit(phase, status, exc=None):
    if phase not in PHASES or status not in STATUSES:
        raise ValueError("Unknown SYN fixture event")
    event = {"phase": phase, "status": status}
    if exc is not None:
        kind = type(exc).__name__
        number = getattr(exc, "errno", None)
        event.update(error_type=kind if kind in ERROR_TYPES else "OtherError",
                     errno=number if type(number) is int and 0 <= number < 4096 else None)
    print(EVENT_PREFIX + json.dumps(event, sort_keys=True), flush=True)


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(Filter))]


def serve():
    phase = "socket"
    try:
        emit(phase, "started")
        with socket.socket() as listener:
            phase = "bind"
            emit(phase, "started")
            listener.bind(("0.0.0.0", 5001))
            phase = "filter"
            emit(phase, "started")
            # Linux UAPI: BPF_RET|BPF_K returns zero (drop); SO_ATTACH_FILTER=26.
            # Patched kernels require NET_ADMIN in this test container's namespace.
            instructions = (Filter * 1)(Filter(0x06, 0, 0, 0))
            program = Program(1, instructions)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.setsockopt(listener.fileno(), socket.SOL_SOCKET, 26,
                               ctypes.byref(program), ctypes.sizeof(program)):
                raise OSError(ctypes.get_errno(), "Fixture socket filter unavailable")
            phase = "listen"
            emit(phase, "started")
            listener.listen(8)
            phase = "marker"
            emit(phase, "started")
            Path("/tmp/blackhole-ready").touch()
            emit(phase, "passed")
            phase = "waiting"
            emit(phase, "started")
            threading.Event().wait(600)
    except BaseException as exc:
        emit(phase, "failed" if isinstance(exc, Exception) else "interrupted", exc)
        raise


if __name__ == "__main__":
    serve()
