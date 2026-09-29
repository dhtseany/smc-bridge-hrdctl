import socket
import threading
import unittest

from hrdctl.plugin import HrdPlugin, parse_target
from test_hrdctl import server

try:
    from smc_bridge.plugins import PluginHost, PluginSettings
except ImportError:
    PluginHost = None

SLIDER = [('[42] get radio', 'Test-Radio'), ('[42] get sliders', 'AF gain,RF gain,Squelch'),
          ('[42] get slider-range Test-Radio RF~gain', '0,255,0'),
          ('[42] get slider-pos Test-Radio RF~gain', '100,39')]


def plugin_for(client, **settings):
    return HrdPlugin({'host': '127.0.0.1', 'port': str(client.port), 'timeout': '2', **settings})


class Entry:
    """Stands in for an importlib.metadata entry point; signals each handled encoder event."""
    handled = threading.Event()

    def load(self):
        handled = self.handled

        class Signalling(HrdPlugin):
            def on_encoder(self, target, delta):
                super().on_encoder(target, delta)
                handled.set()
        return Signalling


class TargetTests(unittest.TestCase):
    def test_valid(self):
        for target, expected in (('vfo', ('vfo', None, None)), ('vfo:-1000', ('vfo', None, -1000)),
                                 ('slider:RF gain', ('slider', 'RF gain', None)),
                                 ('slider:RF gain:+5', ('slider', 'RF gain', 5)),
                                 ('slider:Odd:name', ('slider', 'Odd:name', None)),
                                 ('ptt', ('ptt', None, None)), ('ptt:foot', ('ptt', 'foot', None)), ('button:V > M', ('button', 'V > M', 'on')),
                                 ('button:Nar:off', ('button', 'Nar', 'off')),
                                 ('dropdown:Mode', ('dropdown', 'Mode', None)),
                                 ('dropdown:Mode:USB', ('dropdown', 'Mode', 'USB'))):
            with self.subTest(target=target):
                self.assertEqual(parse_target(target), expected)

    def test_invalid(self):
        for target in ('', 'frequency', 'vfo:fast', 'vfo:0', 'slider:', 'slider::5', 'button:', 'dropdown:'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                parse_target(target)

    def test_invalid_settings(self):
        for key, value in (('port', 'x'), ('vfo_step', '0'), ('timeout', 'nan'), ('retry_after', '-1')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                HrdPlugin({key: value})


class PluginTests(unittest.TestCase):
    def test_encoder_tunes_by_steps(self):
        with server([('get context', '42'), ('[42] get frequency', '7153000'),
                     ('[42] set frequency-hz 7152700', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.start()
            plugin.on_encoder('vfo', -3)
            plugin.stop()

    def test_encoder_slider_step(self):
        with server([('get context', '42'), *SLIDER,
                     ('[42] set slider-pos Test-Radio RF~gain 110', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_encoder('slider:RF gain:5', 2)
            plugin.stop()

    def test_fader_sets_slider_level(self):
        with server([('get context', '42'), *SLIDER,
                     ('[42] set slider-pos Test-Radio RF~gain 191', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_fader('slider:RF gain', 0.75)
            plugin.stop()

    def test_key_press_only(self):
        with server([('get context', '42'), ('[42] get frequency', '7153000'),
                     ('[42] set frequency-hz 7154000', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_key('vfo:+1000', True)
            plugin.on_key('vfo:+1000', False)
            plugin.stop()

    def test_misrouted_targets_raise(self):
        plugin = HrdPlugin({})
        for call in (lambda: plugin.on_key('vfo', True), lambda: plugin.on_fader('vfo', 0.5),
                     lambda: plugin.on_fader('slider:RF gain:5', 0.5), lambda: plugin.on_encoder('ptt', 1),
                     lambda: plugin.on_encoder('button:Nar', 1), lambda: plugin.on_key('dropdown:Mode', True),
                     lambda: plugin.on_fader('ptt', 1.0)):
            with self.assertRaises(ValueError):
                call()
        plugin.on_key('vfo', False)  # Releases are ignored, so a bad target counts once per press.

    def test_ptt_keys_while_held(self):
        with server([('get context', '42'), ('[42] set button-select TX 1', 'OK'),
                     ('[42] set button-select TX 0', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_key('ptt', True)
            plugin.on_key('ptt', False)
            self.assertFalse(plugin._keyed)
            plugin.stop()  # Not keyed: no extra unkey.

    def test_ptt_unkeys_after_last_labelled_key(self):
        with server([('get context', '42'), ('[42] set button-select TX 1', 'OK'),
                     ('[42] set button-select TX 1', 'OK'),
                     ('[42] set button-select TX 0', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_key('ptt:left', True)
            plugin.on_key('ptt:foot', True)
            plugin.on_key('ptt:left', False)  # Foot key still down: stays keyed.
            self.assertTrue(plugin._keyed)
            plugin.on_key('ptt:foot', False)
            self.assertFalse(plugin._keyed)
            plugin.stop()

    def test_failed_unkey_is_retried_first(self):
        with server([('get context', '42'), ('[42] set button-select MOX 1', 'OK'),
                     ('[42] set button-select MOX 0', 'ERROR'),
                     ('[42] set button-select MOX 0', 'OK'),
                     ('[42] get frequency', '7153000'), ('[42] set frequency-hz 7153100', 'OK')]) as client:
            plugin = plugin_for(client, ptt_button='MOX')
            plugin.on_key('ptt', True)
            with self.assertLogs('hrdctl.plugin', 'ERROR'):
                plugin.on_key('ptt', False)
            self.assertTrue(plugin._keyed)
            plugin.on_encoder('vfo', 1)  # Unkeys before tuning.
            self.assertFalse(plugin._keyed)
            plugin.stop()

    def test_stop_unkeys(self):
        with server([('get context', '42'), ('[42] set button-select TX 1', 'OK'),
                     ('[42] set button-select TX 0', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_key('ptt', True)
            plugin.stop()
            self.assertFalse(plugin._keyed)

    def test_release_unkeys_during_backoff(self):
        with server([('get context', '42'), ('[42] set button-select TX 0', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin._offline_until = float('inf')
            plugin.on_key('ptt', True)   # Dropped: HRD is being retried.
            plugin.on_key('ptt', False)  # Sent anyway.
            plugin.stop()

    def test_button_and_dropdown_targets(self):
        mode = [('[42] get dropdowns', 'Mode'), ('[42] get dropdown-text {Mode}', 'Mode: LSB'),
                ('[42] get dropdown-list {Mode}', 'LSB,USB,CW')]
        with server([('get context', '42'), ('[42] get buttons', 'Band +,Nar'),
                     ('[42] set button-select Band~+ 1', 'OK'),
                     ('[42] get buttons', 'Band +,Nar'), ('[42] set button-select Nar 0', 'OK'),
                     *mode, ('[42] set dropdown Mode USB 1', 'OK'),
                     *mode, ('[42] set dropdown Mode USB 1', 'OK')]) as client:
            plugin = plugin_for(client)
            plugin.on_key('button:Band +', True)
            plugin.on_key('button:Band +', False)  # Releases are ignored.
            plugin.on_key('button:Nar:off', True)
            plugin.on_key('dropdown:Mode:USB', True)
            plugin.on_encoder('dropdown:Mode', 1)
            plugin.stop()

    def test_rejected_write_is_logged_not_raised(self):
        with server([('get context', '42'), ('[42] get frequency', '7153000'),
                     ('[42] set frequency-hz 7153100', 'ERROR')]) as client:
            plugin = plugin_for(client)
            with self.assertLogs('hrdctl.plugin', 'WARNING'):
                plugin.on_encoder('vfo', 1)
            self.assertTrue(plugin.client.connected)
            plugin.stop()

    def test_unreachable_hrd_backs_off(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        plugin = HrdPlugin({'host': '127.0.0.1', 'port': str(port), 'retry_after': '60'})
        with self.assertLogs('hrdctl.plugin', 'WARNING') as logs:
            plugin.start()
        self.assertIn('dropping events for 60 s', logs.output[0])
        with self.assertNoLogs('hrdctl.plugin', 'WARNING'):
            plugin.on_encoder('vfo', 1)  # Dropped without another connection attempt.
        self.assertFalse(plugin.client.connected)


@unittest.skipIf(PluginHost is None, 'smc-bridge is not installed')
class BridgeHostTests(unittest.TestCase):
    def test_runs_under_plugin_host(self):
        with server([('get context', '42'), ('[42] get frequency', '7153000'),
                     ('[42] set frequency-hz 7153500', 'OK')]) as client:
            host = PluginHost(discover=lambda: {'hrdctl': Entry()})
            host.configure({'hrdctl': PluginSettings(True, {'host': '127.0.0.1', 'port': str(client.port),
                                                           'vfo_step': '500'})})
            host.encoder('hrdctl', 'vfo', 1)
            self.assertTrue(Entry.handled.wait(3))
            self.assertEqual(host.status(), {'hrdctl': 'running'})
            host.stop()


if __name__ == '__main__':
    unittest.main()
