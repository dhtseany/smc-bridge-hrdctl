#!/usr/bin/env bash

# hrd-rfgain.sh
# Query HRD for RF gain range and current position.

python3 - <<'PY'
import socket
import struct

HOST = "172.16.10.3"
PORT = 7809

RADIO = "FT-991"
SLIDER = "RF~gain"

def make_message(command):
    payload = command.encode("utf-16le") + b"\x00\x00"
    size = 16 + len(payload)

    return struct.pack(
        "<Iiii",
        size,
        0x1234ABCD,
        -0x5432EDCC,
        0
    ) + payload

def receive_message(sock):
    data = sock.recv(16384)

    size, magic1, magic2, checksum = struct.unpack(
        "<Iiii", data[:16]
    )

    return data[16:size].decode("utf-16le").rstrip("\x00")

with socket.create_connection((HOST, PORT), timeout=5) as s:

    # Get HRD context
    s.sendall(make_message("get context"))
    context = receive_message(s)

    print("Context:", context)

    # Get RF gain range
    command = f"[{context}] get slider-range {RADIO} {SLIDER}"
    print("Sending:", command)

    s.sendall(make_message(command))
    slider_range = receive_message(s)

    print("RF gain range:", repr(slider_range))

    # Get current RF gain position
    command = f"[{context}] get slider-pos {RADIO} {SLIDER}"
    print("Sending:", command)

    s.sendall(make_message(command))
    position = receive_message(s)

    print("RF gain position:", repr(position))
PY
