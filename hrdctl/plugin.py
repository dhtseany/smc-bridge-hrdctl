"""smc-bridge plugin: SMC faders, encoders and keys drive HRD.

Registered as the "hrdctl" entry point in the "smc_bridge.plugins" group.
Enable it in smc-bridge's plugins.ini (every setting is optional):

    [hrdctl]
    enabled = yes
    host = 172.16.10.3
    port = 7809
    timeout = 5
    vfo_step = 100
    slider_step = 1
    retry_after = 5
    ptt_button = TX

Targets, entered per control in the smc-bridge mapping editor:

    vfo                  encoder: tune steps x vfo_step Hz
    vfo:<hz>             encoder: tune steps x <hz>; key: tune <hz> per press
    slider:<name>        fader: move the slider to the fader's position;
                         encoder: adjust steps x slider_step raw units
    slider:<name>:<n>    encoder: adjust steps x <n>; key: adjust <n> per press
    dropdown:<name>      encoder: step through the dropdown's choices, wrapping around
    dropdown:<name>:<v>  key: select <v>, e.g. dropdown:Mode:USB
    dropdown:<name>:<n>  <n> with a sign: key: step <n> choices per press
                         (dropdown:Mode:+1); encoder: steps x <n>
    button:<name>        key: press HRD's button, e.g. button:Band +
    button:<name>:off    key: set the button off
    ptt                  key: transmit while held (HRD's ptt_button)
    ptt:<label>          the same, for one of several PTT keys (ptt:left, ptt:foot)
    tune                 key: manual tune; while held, Mode is CW and PTT is keyed;
                         releasing unkeys and puts the previous mode back
    tune:<mode>          the same with another carrier mode, e.g. tune:AM

Key steps are signed (vfo:+1000, vfo:-1000). Only ptt and tune use key releases. Any
slider, button or dropdown HRD lists can be targeted, transmit settings such
as MAX RF power included. Names use ordinary spaces and must match HRD's lists
exactly.

PTT: releasing the last held PTT key unkeys. Give each PTT key its own label
(ptt:left, ptt:foot): two keys sharing one target can't be told apart, so
releasing either would unkey. Releasing unkeys even while HRD is being
retried after an outage. If an unkey fails, it is retried before every later event and when
the plugin stops; a keying attempt whose outcome is unknown counts as keyed.
Only unkeys this plugin keyed are retried, so another program transmitting
through HRD is left alone. Events can queue or be dropped inside smc-bridge,
so a panic stop belongs on a key running the Shell command `hrdctl unkey`,
which uses its own connection.

Tune is PTT with a mode change around it and shares PTT's held keys and
unkey retries. The previous mode is put back after the unkey succeeds (never
while the radio may still be transmitting) or when the plugin stops. If
the mode change fails before PTT is sent, release sends no unkey and only
puts the mode back. A
failed mode restore is logged, not retried. `hrdctl unkey` does not restore
the mode.

The class does not subclass smc_bridge.plugins.Plugin, so this package keeps
no dependency on smc-bridge; the bridge only needs the same methods.
"""
import logging
import math
import re
import time

from .client import HRDClient, HRDError

LOG = logging.getLogger(__name__)


def _setting(settings, key, convert, default):
    value = settings.get(key, "").strip()
    if not value:
        return default
    try:
        return convert(value)
    except ValueError as exc:
        raise ValueError(f"hrdctl: invalid {key} = {value!r}") from exc


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise ValueError("must be positive")
    return number


def _positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("must be finite and positive")
    return number


def _step(text, target):
    try:
        step = int(text)
    except ValueError:
        raise ValueError(f"hrdctl: invalid step in target {target!r}") from None
    if step == 0:
        raise ValueError(f"hrdctl: the step in target {target!r} must not be zero")
    return step


def parse_target(target):
    """A target string -> (kind, name or None, argument or None).

    The argument is a step for vfo and slider; a signed step (int) or a value
    (str) for dropdown; and "on" or "off" for button. The name is a label for
    ptt and a mode for tune. Kinds: vfo, slider, dropdown, button, ptt, tune.
    """
    kind, _, rest = target.strip().partition(":")
    if kind in ("ptt", "tune"):
        return kind, _named(rest, target) if rest else None, None
    if kind == "vfo":
        return "vfo", None, _step(rest, target) if rest else None
    if kind == "slider":
        name, separator, tail = rest.rpartition(":")
        step = None
        if separator:
            try:
                step = _step(tail, target)
            except ValueError:
                name = rest  # A colon in the slider name, not a step.
        else:
            name = rest
        return "slider", _named(name, target), step
    if kind == "dropdown":
        name, _, value = rest.partition(":")
        value = value.strip()
        if re.fullmatch(r"[+-][0-9]+", value):
            return "dropdown", _named(name, target), _step(value, target)
        return "dropdown", _named(name, target), value or None
    if kind == "button":
        name, separator, state = rest.rpartition(":")
        if not separator or state.strip() not in ("on", "off"):
            name, state = rest, "on"
        return "button", _named(name, target), state.strip()
    raise ValueError(f"hrdctl: unknown target {target!r}; use vfo, slider:, dropdown:, button:, ptt or tune")


def _named(name, target):
    if not name.strip():
        raise ValueError(f"hrdctl: target {target!r} needs a name")
    return name.strip()


class HrdPlugin:
    """Each event reads the radio fresh and writes at most once; nothing is retried.

    Invalid targets raise, so smc-bridge logs them. HRD being unreachable is
    logged here instead and events are dropped for retry_after seconds, so an
    HRD restart does not get the plugin switched off; the next event after
    that reconnects.
    """

    def __init__(self, settings):
        self.settings = settings
        self.vfo_step = _setting(settings, "vfo_step", _positive_int, 100)
        self.slider_step = _setting(settings, "slider_step", _positive_int, 1)
        self.retry_after = _setting(settings, "retry_after", _positive_float, 5.0)
        self.client = HRDClient(
            settings.get("host", "").strip() or "172.16.10.3",
            _setting(settings, "port", int, 7809),
            _setting(settings, "timeout", _positive_float, 5.0),
        )
        self.ptt_button = settings.get("ptt_button", "").strip() or "TX"
        self._offline_until = 0.0
        self._keyed = False  # This plugin may have keyed the radio.
        self._held = set()   # ptt and tune targets whose keys are down.
        self._restore_mode = None  # The mode a tune key replaced, until it is put back.

    def start(self):
        # An unreachable HRD at startup is not fatal: raising here would keep
        # the plugin off until smc-bridge is reloaded.
        self._run("connect", self.client.connect)

    def stop(self):
        if self._keyed:
            self._unkey()
        else:
            self._restore()
        self.client.close()

    def on_encoder(self, target, delta):
        kind, name, arg = parse_target(target)
        if kind == "vfo":
            self._run(target, self.client.tune, delta * (arg or self.vfo_step))
        elif kind == "slider":
            self._run(target, self.client.adjust_slider, name, delta * (arg or self.slider_step))
        elif kind == "dropdown" and not isinstance(arg, str):
            self._run(target, self.client.step_dropdown, name, delta * (arg or 1))
        else:
            raise ValueError(f"hrdctl: encoders take vfo, slider: or dropdown:<name> targets, not {target!r}")

    def on_fader(self, target, level):
        kind, name, arg = parse_target(target)
        if kind != "slider" or arg is not None:
            raise ValueError(f"hrdctl: faders take slider:<name> targets, not {target!r}")
        self._run(target, self.client.set_slider_level, name, level)

    def on_key(self, target, pressed):
        kind, name, arg = parse_target(target)
        if kind in ("ptt", "tune"):
            # smc-bridge doesn't say which physical key sent an event, so keys
            # sharing one target can't be told apart; each needs its own label.
            if pressed:
                self._held.add(target.strip())
                if kind == "tune":
                    self._run(target, self._tune, name or "CW")
                else:
                    self._run(target, self._key)
            else:
                self._held.discard(target.strip())
                if not self._held:
                    if kind == "tune" and not self._keyed:
                        self._restore()  # PTT was never sent (the mode change failed).
                    else:
                        self._unkey()
            return
        if not pressed:
            return
        if kind == "button":
            self._run(target, self.client.press_button, name, arg == "on")
        elif kind == "dropdown" and isinstance(arg, int):
            self._run(target, self.client.step_dropdown, name, arg)
        elif kind == "dropdown" and arg is not None:
            self._run(target, self.client.set_dropdown, name, arg)
        elif kind in ("vfo", "slider") and arg is not None:
            if kind == "vfo":
                self._run(target, self.client.tune, arg)
            else:
                self._run(target, self.client.adjust_slider, name, arg)
        else:
            raise ValueError(
                f"hrdctl: keys take ptt, tune, button:, dropdown:<name>:<value> or a signed step "
                f"such as vfo:+1000, not {target!r}"
            )

    def _key(self):
        self._keyed = True  # Before sending: an unknown outcome may have keyed the radio.
        self.client.press_button(self.ptt_button, True, check=False)

    def _tune(self, mode):
        if self._restore_mode is None:  # Another tune key may already have switched.
            current = self.client.get_dropdown("Mode")["value"]
            if current != mode:
                self._restore_mode = current  # Before sending, as in _key.
                self.client.set_dropdown("Mode", mode)
        self._key()

    def _unkey(self):
        """Release PTT now, ignoring any offline backoff, then undo a tune's mode change."""
        try:
            self.client.press_button(self.ptt_button, False, check=False)
        except HRDError as exc:
            self._keyed = True
            LOG.error("HRD unkey failed: %s; the radio may still be transmitting. "
                      "Retrying on the next event; use your STOP key now.", exc)
            return
        self._keyed = False
        self._restore()

    def _restore(self):
        """Put back the mode a tune key replaced, if any; only once PTT is released."""
        if self._restore_mode is not None:
            mode, self._restore_mode = self._restore_mode, None
            try:
                self.client.set_dropdown("Mode", mode)
            except (HRDError, ValueError) as exc:
                LOG.warning("HRD could not put Mode back to %s after tuning: %s", mode, exc)

    def _run(self, label, action, *args):
        if self._keyed and not self._held:
            self._unkey()  # Retry an unkey that failed earlier, before anything else.
        if time.monotonic() < self._offline_until:
            LOG.debug("HRD offline; dropping %s", label)
            return
        try:
            action(*args)
        except HRDError as exc:
            if self.client.connected:
                # The exchange completed, but HRD refused or answered oddly.
                LOG.warning("HRD %s failed: %s", label, exc)
            else:
                self._offline_until = time.monotonic() + self.retry_after
                LOG.warning("HRD %s failed: %s; dropping events for %g s", label, exc, self.retry_after)
