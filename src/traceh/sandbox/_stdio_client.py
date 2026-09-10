"""Fixed root control helper; only connects to the core-owned stdio socket."""

import socket
import sys

maximum = int(sys.argv[1])
request = sys.stdin.buffer.read(maximum + 1)
if len(request) > maximum:
    raise SystemExit(2)
with socket.socket(socket.AF_UNIX) as channel:
    channel.settimeout(6)
    channel.connect("/tmp/.traceh-control/stdio")
    channel.sendall(request)
    channel.shutdown(socket.SHUT_WR)
    output = bytearray()
    while True:
        part = channel.recv(min(65536, maximum + 1 - len(output)))
        if not part:
            break
        output.extend(part)
        if len(output) > maximum:
            raise SystemExit(3)
sys.stdout.buffer.write(output)
