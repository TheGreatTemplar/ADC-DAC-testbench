import unittest

from protocol import ReplyParser, command_code, encode_request


def response(command, data=b"", mode="raw", error=False):
    checksum = (command + sum(data)) & 255
    crc = bytes([checksum]) if mode == "raw" else f"{checksum:02X}".encode("ascii")
    return (b"?" if error else b"!") + f"{command:02X}".encode("ascii") + data.hex().upper().encode("ascii") + crc + b"\r"


class TestProtocol(unittest.TestCase):
    def test_command_bases_and_wire_bytes(self):
        self.assertEqual(command_code(25), 0x19)
        self.assertEqual(command_code(25, True), 0x25)
        self.assertEqual(encode_request(0x19), b"&19\x19\r")
        self.assertEqual(encode_request(0x25, "hex"), b"&2525\r")

    def test_all_fragment_boundaries_and_both_crc_encodings(self):
        for mode in ("raw", "hex"):
            for value in (0, 10, 30, 60, 255):
                packet = response(3, bytes([value]), mode)
                for split in range(len(packet) + 1):
                    parser = ReplyParser({3: 1})
                    replies = parser.feed(packet[:split]) + parser.feed(packet[split:])
                    self.assertEqual(len(replies), 1, (packet, split))
                    self.assertEqual(replies[0].data, bytes([value]))
                    self.assertEqual(replies[0].checksum_mode, mode)

    def test_multiple_packets_noise_and_bad_checksum(self):
        good = response(5, b"\x01\x02")
        bad = good[:-2] + b"\xff\r"
        parser = ReplyParser({5: 2})
        replies = parser.feed(b"noise" + bad + good + good)
        self.assertEqual(len(replies), 2)
        self.assertGreater(parser.rejected, 0)
        self.assertFalse(parser.buffer)

    def test_checksum_overflow_and_error_reply(self):
        parser = ReplyParser({0x25: 2})
        replies = parser.feed(response(0x25, b"\xff\xff") + response(0x25, error=True))
        self.assertEqual(len(replies), 2)
        self.assertFalse(replies[0].device_error)
        self.assertTrue(replies[1].device_error)
        self.assertEqual(replies[1].data, b"")

    def test_invalid_ascii_and_unknown_commands_are_not_data(self):
        parser = ReplyParser({3: 1})
        self.assertFalse(parser.feed(b"!03zz\x03\r!FF0011\r"))
        self.assertGreater(parser.rejected, 0)

    def test_incomplete_packet_remains_buffered(self):
        parser = ReplyParser({5: 2})
        self.assertFalse(parser.feed(b"!05"))
        self.assertEqual(bytes(parser.buffer), b"!05")
