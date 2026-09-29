#!/usr/bin/env bash

# hrd-sliders.sh
# Query HRD for the sliders exposed by the connected radio.

python3 - <<'PY'
import socket
import struct

HOST = "172.16.10.3"
PORT = 7809

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
    size, magic1, magic2, checksum = struct.unpack("<Iiii", data[:16])
    return data[16:size].decode("utf-16le").rstrip("\x00")

with socket.create_connection((HOST, PORT), timeout=5) as s:

    # Get HRD context
    s.sendall(make_message("get context"))
    context = receive_message(s)

    print("Context:", context)

    # Get connected radio name
    s.sendall(make_message(f"[{context}] get radio"))
    radio = receive_message(s)

    print("Radio:", repr(radio))

    # Get available sliders
    s.sendall(make_message(f"[{context}] get sliders"))
    sliders = receive_message(s)

    print()
    print("HRD sliders:")
    print(sliders)
PY
