"""Synchronous HRD IP Server client. No automatic retries of writes."""
import math
import socket
import struct
import threading

HEADER = struct.Struct("<IIII")
MAGIC = (0x1234ABCD, 0xABCD1234)
MAX_FRAME = 1024 * 1024


class HRDError(Exception):
    """Connection, protocol, or command failure."""


class ProtocolError(HRDError):
    """Unexpected HRD response."""


class OutcomeUnknown(HRDError):
    """A write may have reached the radio; do not retry automatically."""


def make_message(command):
    if "\0" in command or "\n" in command or "\r" in command:
        raise ValueError("Commands must not contain NUL or line breaks")
    payload = command.encode("utf-16le") + b"\0\0"
    size = HEADER.size + len(payload)
    if size > MAX_FRAME:
        raise ValueError("Command exceeds frame limit")
    return HEADER.pack(size, *MAGIC, 0) + payload


def _read_exact(sock, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = sock.recv(size - len(parts))
        if not chunk:
            raise ProtocolError("HRD closed the connection before a complete response")
        parts.extend(chunk)
    return bytes(parts)


def receive_message(sock):
    size, first, second, reserved = HEADER.unpack(_read_exact(sock, HEADER.size))
    if (first, second) != MAGIC or reserved != 0:
        raise ProtocolError("Invalid HRD response header")
    if size < 18 or size > MAX_FRAME or (size - HEADER.size) % 2:
        raise ProtocolError(f"Invalid HRD frame size: {size}")
    payload = _read_exact(sock, size - HEADER.size)
    if not payload.endswith(b"\0\0"):
        raise ProtocolError("HRD response has no terminator")
    try:
        result = payload[:-2].decode("utf-16le")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Invalid UTF-16LE response") from exc
    if "\0" in result:
        raise ProtocolError("Unexpected embedded NUL in HRD response")
    return result


def _integer(value, label):
    try:
        return int(value)
    except ValueError as exc:
        raise ProtocolError(f"Invalid {label}: {value!r}") from exc


def _delta(value):
    if type(value) is not int:
        raise ValueError("Adjustment must be an integer")


class HRDClient:
    """One connection with serialized read/modify/write operations.

    Serialization is per client, not across processes or other radio software.
    Current frequency, slider positions, and limits are queried on each operation.
    """

    def __init__(self, host="172.16.10.3", port=7809, timeout=5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be finite and positive")
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        self.host, self.port, self.timeout = host, port, timeout
        self._socket = None
        self._context = None
        self._lock = threading.RLock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    def connect(self):
        with self._lock:
            if self._socket is not None:
                return
            try:
                self._socket = socket.create_connection((self.host, self.port), self.timeout)
                context = self._exchange("get context")
                if not context.isascii() or not context.isdecimal():
                    raise ProtocolError(f"Invalid HRD context: {context!r}")
                self._context = context
            except (OSError, HRDError) as exc:
                self.close()
                raise HRDError(f"Cannot initialize HRD connection: {exc}") from exc

    @property
    def connected(self):
        """False after close() or any failed exchange; the next operation reconnects."""
        return self._socket is not None

    def close(self):
        with self._lock:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._context = None

    def _exchange(self, command, *, writing=False):
        packet = make_message(command)
        try:
            self._socket.sendall(packet)
            return receive_message(self._socket)
        except (OSError, HRDError) as exc:
            self.close()
            if writing:
                raise OutcomeUnknown(
                    "HRD write outcome is unknown; inspect the radio before retrying"
                ) from exc
            raise HRDError(f"HRD response failed: {exc}") from exc

    def _command(self, command, *, writing=False):
        self.connect()
        response = self._exchange(f"[{self._context}] {command}", writing=writing)
        if writing and response.strip() != "OK":
            raise HRDError(f"HRD did not acknowledge command: {response!r}")
        return response

    def get_frequency(self):
        with self._lock:
            frequency = _integer(self._command("get frequency"), "frequency")
            if frequency <= 0:
                raise ProtocolError("HRD returned a nonpositive frequency")
            return frequency

    def tune(self, delta_hz):
        _delta(delta_hz)
        with self._lock:
            target = self.get_frequency() + delta_hz
            if target <= 0:
                raise ValueError("Target frequency must be positive")
            if delta_hz:
                self._command(f"set frequency-hz {target}", writing=True)
            return target

    def get_radio(self):
        with self._lock:
            radio = self._command("get radio")
            if not radio.strip() or any(c in radio for c in "\r\n\0"):
                raise ProtocolError(f"Invalid radio name: {radio!r}")
            return radio

    def get_sliders(self):
        with self._lock:
            return [name.strip() for name in self._command("get sliders").split(",") if name.strip()]

    def _slider_state(self, name):
        if not name.strip() or any(c in name for c in "\r\n\0~"):
            raise ValueError("Use a nonempty slider name with ordinary spaces")
        radio = self.get_radio()
        if name not in self.get_sliders():
            raise ValueError(f"Slider {name!r} is not exposed by {radio}")
        selector = f"{radio} {name.replace(' ', '~')}"
        bounds = self._command(f"get slider-range {selector}").split(",")
        position = self._command(f"get slider-pos {selector}").split(",")
        if len(bounds) != 3 or len(position) != 2:
            raise ProtocolError("Unexpected slider range or position response")
        low, high, _ = [_integer(v, "slider range") for v in bounds]
        raw, displayed = [_integer(v, "slider position") for v in position]
        if low > high or not low <= raw <= high:
            raise ProtocolError("Inconsistent slider range or position")
        return selector, low, high, raw, displayed

    def get_slider(self, name):
        with self._lock:
            _, low, high, raw, displayed = self._slider_state(name)
            return {"minimum": low, "maximum": high, "raw": raw, "displayed": displayed}

    def _set_slider(self, name, choose):
        with self._lock:
            selector, low, high, raw, _ = self._slider_state(name)
            target = max(low, min(high, choose(low, high, raw)))
            if target != raw:
                self._command(f"set slider-pos {selector} {target}", writing=True)
            return target

    def adjust_slider(self, name, delta):
        _delta(delta)
        return self._set_slider(name, lambda low, high, raw: raw + delta)

    def set_slider_level(self, name, level):
        """Move a slider to `level` of its range, 0.0 (minimum) to 1.0 (maximum)."""
        if not 0.0 <= level <= 1.0:
            raise ValueError("Slider level must be between 0.0 and 1.0")
        return self._set_slider(name, lambda low, high, raw: low + round(level * (high - low)))
