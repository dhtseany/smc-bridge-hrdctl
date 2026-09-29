# smc-bridge HRD control

An [smc-bridge](https://github.com/dhtseany/smc-bridge) plugin, plus a dependency-free Python client and `hrdctl` CLI, for HRD Rig Control's native TCP IP Server.

Path: SMC MIDI controller → smc-bridge → HRD TCP/IP → radio. HRD retains ownership of the radio connection. No virtual serial ports, Windows helper, port forwarding, or direct CAT access is required.

## Install

A local, untracked `PKGBUILD` at the root of this directory builds a pacman package from this checkout: the plugin, the `hrdctl` command and the library, in the system Python's site-packages. From this directory:

```sh
makepkg -si
```

`makepkg` runs the test suite before packaging. The runtime needs only `python` (3.10 or newer) and its standard library. Building needs `python-build`, `python-installer`, `python-wheel` and `python-setuptools`, which `makepkg -s` installs through pacman. After changing the code, bump `pkgver` or `pkgrel` and run `makepkg -si` again; `pacman -R smc-bridge-hrdctl` removes it.

For development without installing, run `python3 -m hrdctl` from this directory, or the checkout's `bin/hrdctl` by its full path from anywhere. The smc-bridge plugin only works once the package is installed, because smc-bridge finds plugins through installed package metadata.

## smc-bridge plugin

The package registers the `hrdctl` entry point in smc-bridge's `smc_bridge.plugins` group, so it has to be installed where smc-bridge's system Python can find it. Install it (above), then enable it; a running bridge starts it within a second, no restart needed:

```sh
smc-bridge --enable-plugin hrdctl
```

Settings go in `plugins.ini` beside `mappings.ini`. All are optional; the defaults are shown:

```ini
[hrdctl]
enabled = yes
host = 172.16.10.3
port = 7809
timeout = 5
# Hz per encoder detent for a plain `vfo` target
vfo_step = 100
# Raw units per encoder detent for a plain `slider:<name>` target
slider_step = 1
# Seconds to drop events after HRD becomes unreachable
retry_after = 5
```

In the mapping editor, send a fader, encoder or key to plugin `hrdctl` with one of these targets:

| Target | Fader | Encoder | Key (on press) |
| --- | --- | --- | --- |
| `vfo` | — | tune detents × `vfo_step` Hz | — |
| `vfo:<hz>` | — | tune detents × `<hz>` | tune `<hz>` (signed, e.g. `vfo:-1000`) |
| `slider:<name>` | move slider to the fader's position in its range | adjust detents × `slider_step` | — |
| `slider:<name>:<n>` | — | adjust detents × `<n>` | adjust `<n>` (signed) |

Slider names use ordinary spaces and must match `hrdctl sliders` exactly. Key releases are ignored; there is no PTT or transmit target. Encoder direction follows smc-bridge's `ENCODER_INCREASES_PAN`.

Behavior under smc-bridge's plugin rules:

- An invalid target or setting raises, so smc-bridge logs it (`journalctl --user -u smc-bridge`). Five consecutive failures switch the plugin off until it is re-enabled or the bridge is reloaded.
- HRD being unreachable is not counted as a failure. Startup does not fail when HRD is down; the plugin logs a warning, drops events for `retry_after` seconds, then reconnects on the next event. This keeps a knob turned while HRD is restarting from queuing 5-second timeouts.
- A write whose outcome is unknown is logged and never retried, as in the CLI. A write HRD rejects is logged and the connection is kept.
- Each event is a fresh read/modify/write on one connection, serialized on the plugin's thread, so encoder detents no longer race each other as separate CLI processes could.
- A slider event costs five round trips. smc-bridge drops the plugin's events if 256 are queued, so a very fast fader sweep over a slow link can lose intermediate positions; if the final position is lost, nudge the fader.

## CLI

Defaults: `172.16.10.3:7809`, five-second socket timeout. Override with `HRD_HOST`, `HRD_PORT`, `HRD_TIMEOUT`, or CLI options before the subcommand:

```sh
hrdctl --host 172.16.10.3 --port 7809 frequency
hrdctl radio
hrdctl sliders
hrdctl slider-info "RF gain"
```

The following commands change the radio. Run only when ready to observe it:

```sh
hrdctl tune +1000
hrdctl tune -1000
hrdctl slider "RF gain" +5
hrdctl slider "RF gain" -5
hrdctl slider "AF gain" +5
hrdctl slider "Squelch" -5
```

Slider deltas are **raw units**, not percentages. Use ordinary spaces in slider names; the client converts them to `~` in commands and discovers the radio name. Names must match HRD's slider list exactly.

The plugin is the preferred way to connect smc-bridge. The CLI can still be run from a smc-bridge *Shell command* key action using the installed command's full path, for example `/usr/bin/hrdctl tune +1000`.

Successful tuning/slider adjustments print the acknowledged target integer; this is not a subsequent physical readback. `frequency` prints Hz, `radio` prints its name, and `sliders`/`slider-info` print JSON. Exit codes: `0` success, `1` operation/configuration failure, `2` CLI usage error, `3` unknown write outcome. Errors go to stderr.

### Embedding

```python
from hrdctl import HRDClient

with HRDClient(host="172.16.10.3", port=7809) as radio:
    radio.tune(+1000)
    radio.adjust_slider("RF gain", -5)
```

Reuse one client for a native integration. Operations on that client are serialized, including the entire read/modify/write sequence. Each connection obtains a fresh context. Every adjustment reads the current value; slider operations also discover the radio, available sliders, and range, and clamp to that range. No authoritative frequency or slider state is cached.

Separate CLI processes do **not** serialize their adjustments with one another; simultaneous reads can lose increments. The plugin avoids this by using one client. Other radio software or the physical VFO can also change values between a read and write. The protocol sequence cannot make those changes atomic. Encoder acceleration is outside this draft.

The receiver reads complete length-prefixed frames, checks header fields, bounds frame sizes, validates UTF-16LE, and rejects truncated responses. A broken exchange closes the connection. A failed write exchange has an unknown outcome and is never automatically retried; inspect the radio before issuing another adjustment. The timeout applies to individual socket operations, not a total transaction deadline.

No PTT, transmit, or arbitrary raw-command interface is exposed. Frequency limits beyond positivity are left to HRD; supported bands have not been inferred from one radio model.

## Verification and status

```sh
python3 -m unittest discover -s tests -v
```

Tests use loopback TCP and in-memory streams, never the configured radio. They cover fragmented frames, malformed/truncated responses, fresh frequency reads, dynamic context/radio selection, slider clamping, rejected writes, unknown write outcomes, plugin targets and settings, offline backoff, and (when smc-bridge is importable) a run under smc-bridge's own `PluginHost`.

The original `proof/` scripts are preserved as references. Their one-shot receive functions are not used by this package. This draft follows the supplied protocol and experimentally confirmed commands; slider writes (including fader-driven absolute positions) and strict response-header assumptions still need live verification. Live testing requires separate approval before any connection to the real HRD server.

## License

Copyright (C) 2026 Sean Snell

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See [LICENSE](LICENSE) for the full text.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE.
