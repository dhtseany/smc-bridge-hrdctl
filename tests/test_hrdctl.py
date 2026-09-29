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

    def test_bad_payloads(self):
        for payload in (b'A\x00', b'\x00\xd8\x00\x00', b'\0\0\0\0'):
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

    def test_cli_unknown_exit_code(self):
        with patch('hrdctl.cli.HRDClient') as factory, contextlib.redirect_stderr(io.StringIO()):
            factory.return_value.__enter__.return_value.tune.side_effect = OutcomeUnknown('unknown')
            self.assertEqual(main(['tune', '+1000']), 3)


if __name__ == '__main__':
    unittest.main()
