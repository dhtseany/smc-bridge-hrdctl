import contextlib
import io
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from hrdctl.client import (HRDClient, HRDError, OutcomeUnknown, ProtocolError,
                           MAX_FRAME, make_message, receive_message)
from hrdctl.cli import main


class FragmentedSocket:
    def __init__(self, data, chunk=1):
        self.data = data
        self.chunk = chunk

    def recv(self, size):
        result = self.data[:min(size, self.chunk)]
        self.data = self.data[len(result):]
        return result


@contextlib.contextmanager
def server(script):
    """Local TCP peer checking exact commands and fragmenting all replies."""
    errors = []
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(3)
    port = listener.getsockname()[1]

    def run():
        try:
            with listener.accept()[0] as conn:
                conn.settimeout(3)
                for expected, response in script:
                    actual = receive_message(conn)
                    if actual != expected:
                        raise AssertionError(f'{actual!r} != {expected!r}')
                    if response is None:
                        return
                    packet = make_message(response)
                    for byte in packet:
                        conn.sendall(bytes([byte]))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        yield HRDClient('127.0.0.1', port, timeout=2)
    finally:
        worker.join(4)
        listener.close()
        if worker.is_alive():
            raise AssertionError('Mock server did not stop')
        if errors:
            raise errors[0]


class FramingTests(unittest.TestCase):
    def test_wire_format(self):
        expected = struct.pack('<IIII', 40, 0x1234ABCD, 0xABCD1234, 0)
        self.assertEqual(make_message('get context'), expected + 'get context\0'.encode('utf-16le'))

    def test_fragmentation_and_coalescing(self):
        sock = FragmentedSocket(make_message('7153000') + make_message('OK'), 3)
        self.assertEqual(receive_message(sock), '7153000')
        self.assertEqual(receive_message(sock), 'OK')

    def test_truncation(self):
        for count in (0, 8, 20, 31):
            with self.subTest(count=count), self.assertRaises(ProtocolError):
                receive_message(FragmentedSocket(make_message('7153000')[:count]))

    def test_bad_headers(self):
        for size, magic, reserved in ((16, 0x1234ABCD, 0), (19, 0x1234ABCD, 0),
                                      (MAX_FRAME + 2, 0x1234ABCD, 0),
                                      (18, 0, 0), (18, 0x1234ABCD, 1)):
            with self.subTest(size=size, magic=magic, reserved=reserved), self.assertRaises(ProtocolError):
                receive_message(FragmentedSocket(struct.pack('<IIII', size, magic, 0xABCD1234, reserved)))

    def test_nul_padding(self):
        """HRD pads replies with NULs after the terminator."""
        for padding in (b'\0\0', b'\0\0' * 8):
            payload = '1234'.encode('utf-16le') + b'\0\0' + padding
            packet = struct.pack('<IIII', 16 + len(payload), 0x1234ABCD, 0xABCD1234, 0) + payload
            with self.subTest(padding=len(padding)):
                self.assertEqual(receive_message(FragmentedSocket(packet, 5)), '1234')

    def test_bad_payloads(self):
        for payload in (b'A\x00', b'\x00\xd8\x00\x00', b'A\x00B\x00'):
            packet = struct.pack('<IIII', 16 + len(payload), 0x1234ABCD, 0xABCD1234, 0) + payload
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                receive_message(FragmentedSocket(packet))


class ClientTests(unittest.TestCase):
    def test_tune_reads_external_change(self):
        with server([('get context', '123'), ('[123] get frequency', '7153000'),
                     ('[123] set frequency-hz 7154000', 'OK'),
                     ('[123] get frequency', '14200000'),
                     ('[123] set frequency-hz 14199000', 'OK')]) as client:
            with client:
                self.assertEqual(client.tune(1000), 7154000)
                self.assertEqual(client.tune(-1000), 14199000)

    def test_slider_clamps_both_ends(self):
        for raw, delta, target in ((253, 5, 255), (2, -5, 0)):
            script = [('get context', '42'), ('[42] get radio', 'Test-Radio'),
                      ('[42] get sliders', 'AF gain,RF gain,Squelch'),
                      ('[42] get slider-range Test-Radio RF~gain', '0,255,0'),
                      ('[42] get slider-pos Test-Radio RF~gain', f'{raw},50'),
                      (f'[42] set slider-pos Test-Radio RF~gain {target}', 'OK')]
            with self.subTest(target=target), server(script) as client:
                with client:
                    self.assertEqual(client.adjust_slider('RF gain', delta), target)

    def test_slider_display_text(self):
        """HRD's position reply is '<raw>,<display text>', and the text may hold units or commas."""
        range_and_pos = [('[42] get radio', 'FT-991'), ('[42] get sliders', 'MAX RF power,AF gain'),
                         ('[42] get slider-range FT-991 MAX~RF~power', '0,255,1'),
                         ('[42] get slider-pos FT-991 MAX~RF~power', '179,1,070 W')]
        with server([('get context', '42'), *range_and_pos, *range_and_pos,
                     ('[42] set slider-pos FT-991 MAX~RF~power 174', 'OK')]) as client:
            with client:
                self.assertEqual(client.get_slider('MAX RF power'),
                                 {'minimum': 0, 'maximum': 255, 'raw': 179, 'displayed': '1,070 W'})
                self.assertEqual(client.adjust_slider('MAX RF power', -5), 174)

    def test_position_beyond_hrd_range(self):
        """FT-991 Filter width: HRD accepts 1-17, the radio sits at 20. Up does nothing; down enters the range."""
        state = [('[42] get radio', 'FT-991'), ('[42] get sliders', 'Filter width'),
                 ('[42] get slider-range FT-991 Filter~width', '1,17,0'),
                 ('[42] get slider-pos FT-991 Filter~width', '20,Unknown (20)')]
        with server([('get context', '42'), *state, *state, *state,
                     ('[42] set slider-pos FT-991 Filter~width 17', 'OK')]) as client:
            with client:
                self.assertEqual(client.get_slider('Filter width'),
                                 {'minimum': 1, 'maximum': 17, 'raw': 20, 'displayed': 'Unknown (20)'})
                self.assertEqual(client.adjust_slider('Filter width', 1), 20)  # Further out: no write.
                self.assertEqual(client.adjust_slider('Filter width', -1), 17)

    def test_bad_frequency_never_writes(self):
        with server([('get context', '7'), ('[7] get frequency', 'ERROR')]) as client:
            with client, self.assertRaises(ProtocolError):
                client.tune(1000)

    def test_invalid_context(self):
        with server([('get context', 'ERROR')]) as client:
            with self.assertRaises(HRDError):
                client.connect()
            self.assertIsNone(client._socket)

    def test_write_disconnect_is_unknown(self):
        with server([('get context', '9'), ('[9] get frequency', '7153000'),
                     ('[9] set frequency-hz 7154000', None)]) as client:
            with client, self.assertRaises(OutcomeUnknown):
                client.tune(1000)
            self.assertIsNone(client._socket)

    def test_rejected_write_fails(self):
        with server([('get context', '9'), ('[9] get frequency', '7153000'),
                     ('[9] set frequency-hz 7154000', 'ERROR')]) as client:
            with client, self.assertRaises(HRDError):
                client.tune(1000)

    def test_cli_negative_delta(self):
        with server([('get context', '9'), ('[9] get frequency', '7153000'),
                     ('[9] set frequency-hz 7152000', 'OK')]) as client:
            output = io.StringIO()
            with patch('hrdctl.cli.HRDClient', return_value=client), contextlib.redirect_stdout(output):
                self.assertEqual(main(['tune', '-1000']), 0)
            self.assertEqual(output.getvalue(), '7152000\n')

    def test_discovery_lists_and_get(self):
        with server([('get context', '5'), ('[5] get buttons', 'TX, Tune,MOX'),
                     ('[5] get dropdowns', 'Mode,Band'),
                     ('[5] get button-select TX', '0')]) as client:
            with client:
                self.assertEqual(client.get_buttons(), ['TX', 'Tune', 'MOX'])
                self.assertEqual(client.get_dropdowns(), ['Mode', 'Band'])
                self.assertEqual(client.query(' button-select TX '), '0')
                with self.assertRaises(ValueError):
                    client.query(' ')

    def test_cli_get_is_read_only(self):
        with server([('get context', '5'), ('[5] get dropdown-text Mode', 'USB')]) as client:
            output = io.StringIO()
            with patch('hrdctl.cli.HRDClient', return_value=client), contextlib.redirect_stdout(output):
                self.assertEqual(main(['get', 'dropdown-text', 'Mode']), 0)
            self.assertEqual(output.getvalue(), 'USB\n')

    def test_buttons_and_dropdowns(self):
        mode = [('[5] get dropdowns', 'Mode,AGC'), ('[5] get dropdown-text {Mode}', 'Mode: LSB'),
                ('[5] get dropdown-list {Mode}', 'LSB,USB,CW')]
        with server([('get context', '5'), ('[5] get buttons', 'TX,Band +'),
                     ('[5] set button-select Band~+ 1', 'OK'),
                     ('[5] set button-select TX 0', 'OK'),
                     *mode, *mode, ('[5] set dropdown Mode USB 1', 'OK'),
                     *mode, ('[5] set dropdown Mode CW 2', 'OK'),
                     *mode]) as client:
            with client:
                client.press_button('Band +')
                client.press_button('TX', False, check=False)
                self.assertEqual(client.get_dropdown('Mode'), {'value': 'LSB', 'options': ['LSB', 'USB', 'CW']})
                self.assertEqual(client.set_dropdown('Mode', 'USB'), 'USB')
                self.assertEqual(client.step_dropdown('Mode', 5), 'CW')  # LSB + 5 wraps: USB, CW, LSB, USB, CW.
                with self.assertRaises(ValueError):
                    client.set_dropdown('Mode', 'SSB')

    def test_unknown_button_never_writes(self):
        with server([('get context', '5'), ('[5] get buttons', 'TX'), ('[5] get radio', 'FT-991')]) as client:
            with client, self.assertRaises(ValueError):
                client.press_button('Band +')

    def test_cli_unkey(self):
        with server([('get context', '5'), ('[5] set button-select TX 0', 'OK')]) as client:
            output = io.StringIO()
            with patch('hrdctl.cli.HRDClient', return_value=client), contextlib.redirect_stdout(output):
                self.assertEqual(main(['unkey']), 0)
            self.assertEqual(output.getvalue(), 'off\n')

    def test_cli_unknown_exit_code(self):
        with patch('hrdctl.cli.HRDClient') as factory, contextlib.redirect_stderr(io.StringIO()):
            factory.return_value.__enter__.return_value.tune.side_effect = OutcomeUnknown('unknown')
            self.assertEqual(main(['tune', '+1000']), 3)


if __name__ == '__main__':
    unittest.main()
