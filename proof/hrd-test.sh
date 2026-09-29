#!/usr/bin/env bash

# hrd-test.sh
# Tune HRD VFO up 1 kHz

python3 - <<'PY'
import socket
import struct

HOST = "172.16.10.3"
PORT = 7809
STEP = 1000

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
    data = sock.recv(4096)
    size, magic1, magic2, checksum = struct.unpack("<Iiii", data[:16])
    return data[16:size].decode("utf-16le").rstrip("\x00")

with socket.create_connection((HOST, PORT), timeout=5) as s:
    # Get context
    s.sendall(make_message("get context"))
    context = receive_message(s)

    # Get current frequency
    s.sendall(make_message(f"[{context}] get frequency"))
    current = int(receive_message(s))

    # Add 1 kHz
    new_frequency = current + STEP

    print(f"Current: {current} Hz")
    print(f"New:     {new_frequency} Hz")
    print(f"Tuning:  +{STEP} Hz")

    # Set new frequency
    command = f"[{context}] set frequency-hz {new_frequency}"
    print("Sending:", command)

    s.sendall(make_message(command))

    # HRD should acknowledge/respond
    try:
        response = receive_message(s)
        print("HRD response:", repr(response))
    except TimeoutError:
        print("No response from HRD (frequency command was sent).")
PY
