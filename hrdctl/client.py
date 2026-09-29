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
    # HRD pads replies with extra NULs after the terminator; the text ends at the first one.
    end = next((i for i in range(0, len(payload), 2) if payload[i:i + 2] == b"\0\0"), None)
    if end is None:
        raise ProtocolError("HRD response has no terminator")
    try:
        return payload[:end].decode("utf-16le")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Invalid UTF-16LE response") from exc


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

    def _get_list(self, command):
        with self._lock:
            return [name.strip() for name in self._command(command).split(",") if name.strip()]

    def get_sliders(self):
        return self._get_list("get sliders")

    def get_buttons(self):
        return self._get_list("get buttons")

    def get_dropdowns(self):
        return self._get_list("get dropdowns")

    def query(self, text):
        """Send `get <text>` and return HRD's raw reply. Only get commands can be sent."""
        if not text.strip():
            raise ValueError("Give the rest of a get command, such as 'radio'")
        with self._lock:
            return self._command(f"get {text.strip()}")

    def _slider_state(self, name):
        if not name.strip() or any(c in name for c in "\r\n\0~"):
            raise ValueError("Use a nonempty slider name with ordinary spaces")
        radio = self.get_radio()
        if name not in self.get_sliders():
            raise ValueError(f"Slider {name!r} is not exposed by {radio}")
        selector = f"{radio} {name.replace(' ', '~')}"
        bounds = self._command(f"get slider-range {selector}").split(",")
        # The position reply is "<raw>,<display text>", e.g. "179,70 W".
        position = self._command(f"get slider-pos {selector}").split(",", 1)
        if len(bounds) != 3 or len(position) != 2:
            raise ProtocolError("Unexpected slider range or position response")
        low, high, _ = [_integer(v, "slider range") for v in bounds]
        raw, displayed = _integer(position[0], "slider position"), position[1].strip()
        if low > high:
            raise ProtocolError("Inconsistent slider range")
        return selector, low, high, raw, displayed

    def get_slider(self, name):
        with self._lock:
            _, low, high, raw, displayed = self._slider_state(name)
            return {"minimum": low, "maximum": high, "raw": raw, "displayed": displayed}

    def _set_slider(self, name, choose):
        with self._lock:
            selector, low, high, raw, _ = self._slider_state(name)
            wanted = choose(low, high, raw)
            # The radio can sit outside HRD's range (an FT-991's Filter width reads 20 while
            # HRD accepts only 1-17). Moving further out does nothing; moving back enters the range.
            if (raw > high and wanted >= raw) or (raw < low and wanted <= raw):
                return raw
            target = max(low, min(high, wanted))
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

    # Button and dropdown commands follow WSJT-X's HRD transceiver: names and values
    # use ~ for spaces in set commands, dropdown reads take the name in braces, and
    # the dropdown index is the value's position in HRD's list. HRD answers every
    # button read with 0 (an FT-991 reported Power off while on), so button state is
    # never read.

    def press_button(self, name, on=True, *, check=True):
        """Set button `name` on (press, key) or off (release, unkey).

        `check` confirms the name against HRD's button list first; PTT skips it to
        keep keying quick.
        """
        selector = _tilde(name, "button")
        with self._lock:
            if check and name not in self.get_buttons():
                raise ValueError(f"Button {name!r} is not exposed by {self.get_radio()}")
            self._command(f"set button-select {selector} {1 if on else 0}", writing=True)

    def get_dropdown(self, name):
        """{"value": current selection, "options": [choices in HRD's order]}."""
        _tilde(name, "dropdown")
        with self._lock:
            if name not in self.get_dropdowns():
                raise ValueError(f"Dropdown {name!r} is not exposed by {self.get_radio()}")
            label, separator, value = self._command(f"get dropdown-text {{{name}}}").partition(":")
            if not separator or label.strip() != name:
                raise ProtocolError(f"Unexpected dropdown text for {name!r}")
            options = self._get_list(f"get dropdown-list {{{name}}}")
            return {"value": value.strip(), "options": options}

    def _select(self, name, options, value):
        self._command(f"set dropdown {_tilde(name, 'dropdown')} {_tilde(value, 'dropdown value')} "
                      f"{options.index(value)}", writing=True)
        return value

    def set_dropdown(self, name, value):
        with self._lock:
            options = self.get_dropdown(name)["options"]
            if value not in options:
                raise ValueError(f"{name} has no {value!r}; choose from {', '.join(options)}")
            return self._select(name, options, value)

    def step_dropdown(self, name, delta):
        """Move `delta` places through the dropdown's list, wrapping around at either end."""
        _delta(delta)
        with self._lock:
            state = self.get_dropdown(name)
            options, value = state["options"], state["value"]
            if value not in options:
                raise ProtocolError(f"{name} is at {value!r}, which is not in its list")
            index = (options.index(value) + delta) % len(options)
            return value if options[index] == value else self._select(name, options, options[index])


def _tilde(name, kind):
    if not name.strip() or any(c in name for c in "\r\n\0~{}"):
        raise ValueError(f"Use a nonempty {kind} name with ordinary spaces")
    return name.replace(" ", "~")
