"""Silent SYN loss on one fixture-owned socket; no firewall or network capability."""

import ctypes
from pathlib import Path
import socket
import threading


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("instructions", ctypes.POINTER(Filter))]


def serve():
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 5001))
        # Linux UAPI: BPF_RET|BPF_K returns zero (drop); SO_ATTACH_FILTER=26.
        instructions = (Filter * 1)(Filter(0x06, 0, 0, 0))
        program = Program(1, instructions)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setsockopt(listener.fileno(), socket.SOL_SOCKET, 26,
                           ctypes.byref(program), ctypes.sizeof(program)):
            raise OSError(ctypes.get_errno(), "Fixture socket filter unavailable")
        listener.listen(8)
        Path("/tmp/blackhole-ready").touch()
        threading.Event().wait(600)


if __name__ == "__main__":
    serve()
