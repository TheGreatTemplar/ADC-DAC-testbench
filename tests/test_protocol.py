import unittest

from protocol import (FW018_LENGTHS, AdcStreamParser, ReplyParser, command_code,
                      decode_working_snapshot, describe_request, encode_request)


def response(command, data=b"", mode="raw", error=False):
    checksum = (command + sum(data)) & 255
    crc = bytes([checksum]) if mode == "raw" else f"{checksum:02X}".encode("ascii")
    return (b"?" if error else b"!") + f"{command:02X}".encode("ascii") + data.hex().upper().encode("ascii") + crc + b"\r"


class TestProtocol(unittest.TestCase):
    def test_working_snapshot_and_gain_frames_at_every_split(self):
        # Signal -17, trigger 300, state 1: little-endian fields from 0x080023F8.
        packet = b"!0BEFFF2C010127\r!0F0413\r!2D2D\r?2D0330\r"
        for split in range(len(packet) + 1):
            parser = ReplyParser(FW018_LENGTHS, error_size=1, checksum_modes=("hex",))
            replies = parser.feed(packet[:split]) + parser.feed(packet[split:])
            self.assertEqual(len(replies), 4, split)
            self.assertEqual(decode_working_snapshot(replies[0].data), (-17, 300, 1))
            self.assertEqual(replies[1].data, b"\x04")
            self.assertEqual(replies[2].data, b"")
            self.assertTrue(replies[3].device_error)
            self.assertEqual(parser.rejected, 0)
        self.assertEqual(encode_request(11, "hex"), b"&0B0B\r")
        self.assertEqual(encode_request(45, "hex", b"\x08"), b"&2D0835\r")
        self.assertIn("ступень 8; CRC hex: OK", describe_request(b"&2D0835\r", firmware="fw018"))

    def test_working_rejects_raw_and_bad_crc(self):
        parser = ReplyParser(FW018_LENGTHS, error_size=1, checksum_modes=("hex",))
        self.assertFalse(parser.feed(b"!0F04FF\r!0F04\x13\r"))
        self.assertEqual(parser.feed(b"!0F0413\r")[0].data, b"\x04")

    def test_adc_stream_all_fragment_boundaries(self):
        # Literal format recovered from testbench_1.hex at 0x08002970.
        for ending in (b"\r", b"\r\n", b"\n"):
            packet = b"A0: 0123; A1: 0ABC; A2: 0fff;" + ending
            for split in range(len(packet) + 1):
                parser = AdcStreamParser()
                samples = parser.feed(packet[:split]) + parser.feed(packet[split:])
                self.assertEqual(samples, [(0x123, 0xABC, 0xFFF)], (ending, split))
                self.assertFalse(parser.buffer)
                self.assertEqual(parser.rejected, 0)

    def test_adc_stream_real_cr_only_rows(self):
        parser = AdcStreamParser()
        packet = b"A0: 028D; A1: 073F; A2: 0EA8;\rA0: 0290; A1: 074B; A2: 0E7F;\r"
        samples = []
        for byte in packet:
            samples.extend(parser.feed(bytes([byte])))
        self.assertEqual(samples, [(653, 1855, 3752), (656, 1867, 3711)])
        self.assertFalse(parser.buffer)

    def test_adc_stream_recovery_and_multiple_rows(self):
        parser = AdcStreamParser()
        packet = b"A0: 0000; A1: 0800; A2: 0FFF;\r\n"
        self.assertEqual(parser.feed(b"A1: 0000; A2: 0000;\r\n" + packet * 2),
                         [(0, 2048, 4095)] * 2)
        self.assertFalse(parser.feed(b"A0: ZZZZ; A1: 0000; A2: 0000;\r\n"))
        self.assertFalse(parser.feed(b"\x87\xa4" * 1000))
        self.assertLessEqual(len(parser.buffer), 128)
        self.assertEqual(parser.feed(packet), [(0, 2048, 4095)])
        self.assertGreater(parser.rejected, 0)

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
