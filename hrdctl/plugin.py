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

Targets, entered per control in the smc-bridge mapping editor:

    vfo                  encoder: tune steps x vfo_step Hz
    vfo:<hz>             encoder: tune steps x <hz>; key: tune <hz> per press
    slider:<name>        fader: move the slider to the fader's position;
                         encoder: adjust steps x slider_step raw units
    slider:<name>:<n>    encoder: adjust steps x <n>; key: adjust <n> per press

Key steps are signed (vfo:+1000, vfo:-1000); releases are ignored. No PTT or
transmit target exists. Slider names use ordinary spaces and must match HRD's
slider list exactly.

The class does not subclass smc_bridge.plugins.Plugin, so this package keeps
no dependency on smc-bridge; the bridge only needs the same methods.
"""
import logging
import math
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
    """'vfo[:step]' or 'slider:<name>[:step]' -> (kind, slider name or None, step or None)."""
    kind, _, rest = target.strip().partition(":")
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
        if not name.strip():
            raise ValueError(f"hrdctl: target {target!r} needs a slider name")
        return "slider", name, step
    raise ValueError(f"hrdctl: unknown target {target!r}; use vfo[:hz] or slider:<name>[:step]")


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
        self._offline_until = 0.0

    def start(self):
        # An unreachable HRD at startup is not fatal: raising here would keep
        # the plugin off until smc-bridge is reloaded.
        self._run("connect", self.client.connect)

    def stop(self):
        self.client.close()

    def on_encoder(self, target, delta):
        kind, name, step = parse_target(target)
        if kind == "vfo":
            self._run(target, self.client.tune, delta * (step or self.vfo_step))
        else:
            self._run(target, self.client.adjust_slider, name, delta * (step or self.slider_step))

    def on_fader(self, target, level):
        kind, name, step = parse_target(target)
        if kind != "slider" or step is not None:
            raise ValueError(f"hrdctl: faders take slider:<name> targets, not {target!r}")
        self._run(target, self.client.set_slider_level, name, level)

    def on_key(self, target, pressed):
        if not pressed:
            return
        kind, name, step = parse_target(target)
        if step is None:
            raise ValueError(f"hrdctl: key targets need a signed step, such as vfo:+1000, not {target!r}")
        if kind == "vfo":
            self._run(target, self.client.tune, step)
        else:
            self._run(target, self.client.adjust_slider, name, step)

    def _run(self, label, action, *args):
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
