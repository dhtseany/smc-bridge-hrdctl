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
                                 ('slider:Odd:name', ('slider', 'Odd:name', None))):
            with self.subTest(target=target):
                self.assertEqual(parse_target(target), expected)

    def test_invalid(self):
        for target in ('', 'frequency', 'vfo:fast', 'vfo:0', 'slider:', 'slider::5'):
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
                     lambda: plugin.on_fader('slider:RF gain:5', 0.5), lambda: plugin.on_encoder('ptt', 1)):
            with self.assertRaises(ValueError):
                call()
        plugin.on_key('vfo', False)  # Releases are ignored, so a bad target counts once per press.

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
