import hashlib
import struct
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from swd import RAM_ADDRESS, ThresholdSnapshot, ThresholdWorker, read_thresholds


class TestSwd(unittest.TestCase):
    def test_production_profile_uses_its_own_signature_and_ram(self):
        expected = hashlib.sha256(struct.pack("<I", 0x12345678)).hexdigest()
        words = struct.unpack("<6I", struct.pack("<6f", -17, 17, -191, 5, 53, 48))
        output = ("0x08002F54 : 12345678\n0x2000044C : "
                  + " ".join(f"{word:08X}" for word in words) + "\n").encode("ascii")
        with patch("swd.FW018_BLOCKS", ((0x08002F54, 4, expected),)), \
                patch("swd.subprocess.run", return_value=SimpleNamespace(stdout=output, returncode=0)) as run:
            result = read_thresholds("cli.exe", "fw018")
        self.assertEqual(run.call_args[0][0][-3:], ["-r32", "0x2000044C", "0x18"])
        self.assertEqual((result.signal, result.trigger, result.release), (-17, 53, 48))

    def test_worker_recovers_after_a_temporary_error(self):
        class TwoReads:
            count = 0

            def is_set(self):
                return self.count >= 2

            def wait(self, seconds):
                self.count += 1

        worker = ThresholdWorker()
        worker.stop_event = TwoReads()
        failed, updated = [], []
        worker.failed.connect(failed.append)
        worker.updated.connect(updated.append)
        snapshot = ThresholdSnapshot(1, 5, 52.5, 47.5)
        with patch("swd.find_stlink_cli", return_value="cli.exe"), \
                patch("swd.read_thresholds", side_effect=[RuntimeError("USB busy"), snapshot]):
            worker.run()
        self.assertEqual(failed, ["USB busy"])
        self.assertEqual(updated, [snapshot])

    def read(self, values, signature="12345678", returncode=0):
        expected = hashlib.sha256(struct.pack("<I", 0x12345678)).hexdigest()
        words = struct.unpack("<6I", struct.pack("<6f", *values))
        output = (f"0x080028B0 : {signature}\n0x{RAM_ADDRESS:08X} : "
                  + " ".join(f"{word:08X}" for word in words) + "\n").encode("ascii")
        with patch("swd.FIRMWARE_BLOCKS", ((0x080028B0, 4, expected),)), \
                patch("swd.subprocess.run", return_value=SimpleNamespace(stdout=output, returncode=returncode)) as run:
            result = read_thresholds("cli.exe")
            args = run.call_args[0][0]
            self.assertEqual(args[:4], ["cli.exe", "-c", "SWD", "HOTPLUG"])
            self.assertEqual(args[-3:], ["-r32", "0x2000033C", "0x18"])
            return result

    def test_live_layout_and_read_only_hotplug_command(self):
        result = self.read((6.5, 6.5, -191, 5.04, 52.52, 47.52))
        self.assertAlmostEqual(result.trigger, 52.52, places=4)
        self.assertAlmostEqual(result.release, 47.52, places=4)
        self.assertEqual(result.signal, 6.5)
        self.assertAlmostEqual(result.noise, 5.04, places=4)

    def test_different_firmware_is_not_interpreted(self):
        with self.assertRaisesRegex(RuntimeError, "Прошивка отличается"):
            self.read((0, 0, 0, 5, 50, 45), signature="87654321")

    def test_incomplete_read_and_usb_error_are_not_values(self):
        with patch("swd.subprocess.run", return_value=SimpleNamespace(stdout=b"USB error", returncode=0)):
            with self.assertRaisesRegex(RuntimeError, "полный блок"):
                read_thresholds("cli.exe")
        with self.assertRaisesRegex(RuntimeError, "Нет доступа"):
            self.read((0, 0, 0, 5, 50, 45), returncode=1)

    def test_invalid_floats_and_reversed_thresholds_are_rejected(self):
        for trigger, release in ((float("nan"), 45), (50, 60), (float("inf"), 45)):
            with self.assertRaisesRegex(RuntimeError, "Некорректные значения"):
                self.read((0, 0, 0, 5, trigger, release))
