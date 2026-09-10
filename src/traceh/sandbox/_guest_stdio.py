"""Trusted control transport, prepended to the existing guest supervisor script.

This is byte IO only. It does not interpret an adapter's application protocol.
"""

import base64
import json
import os
import select
import socket
import threading
import time
from pathlib import Path


class GuestStdio:
    def __init__(self, process, streams, eof, limits):
        self.process = process
        self.streams = streams
        self.eof = eof
        self.limits = limits
        self.input_bytes = 0
        self.closed = False
        self.server = socket.socket(socket.AF_UNIX)
        directory = Path("/tmp/.traceh-control")
        directory.mkdir(mode=0o700)
        self.server.bind(str(directory / "stdio"))
        self.server.listen(4)
        os.set_blocking(process.stdin.fileno(), False)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            connection, _ = self.server.accept()
            with connection:
                connection.settimeout(5)
                try:
                    wire = bytearray()
                    maximum = 2 * self.limits["frame_bytes"] + 4096
                    while True:
                        part = connection.recv(min(65536, maximum + 1 - len(wire)))
                        if not part:
                            break
                        wire.extend(part)
                        if len(wire) > maximum:
                            raise ValueError("frame-limit")
                    response = self.dispatch(json.loads(wire))
                except (ValueError, KeyError, TypeError, OSError):
                    response = {"ok": False, "error": "sandbox-stdio-request-failed"}
                try:
                    connection.sendall(json.dumps(response).encode())
                except OSError:
                    # An uncertain write is never retried by the host. The
                    # execution owner closes the whole connection on IO failure.
                    pass

    def dispatch(self, operation):
        kind = operation["operation"]
        if kind == "hello":
            return {"ok": True}
        if kind == "read":
            index = operation["stream"]
            offset = operation["offset"]
            size = operation["size"]
            if (type(index) is not int or index not in (0, 1)
                    or type(offset) is not int or not 0 <= offset <= len(self.streams[index])
                    or type(size) is not int or not 1 <= size <= self.limits["frame_bytes"]):
                raise ValueError("read-invalid")
            content = bytes(self.streams[index][offset:offset + size])
            return {"ok": True, "data": base64.b64encode(content).decode(),
                    "eof": self.eof[index] and offset + len(content) == len(self.streams[index])}
        if kind == "close-input":
            if not self.closed:
                self.process.stdin.close()
                self.closed = True
            return {"ok": True}
        if kind != "write" or self.closed:
            raise ValueError("write-invalid")
        content = base64.b64decode(operation["data"], validate=True)
        if (len(content) > self.limits["frame_bytes"]
                or self.input_bytes + len(content) > self.limits["input_bytes"]):
            raise ValueError("input-limit")
        # Reserve before IO. A partial failed write consumes its whole request;
        # neither the host nor this controller may replay it.
        self.input_bytes += len(content)
        offset = 0
        deadline = time.monotonic() + 5
        while offset < len(content):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise ValueError("write-timeout")
            try:
                offset += os.write(self.process.stdin.fileno(), content[offset:])
            except BlockingIOError:
                continue
        return {"ok": True, "written": offset}
