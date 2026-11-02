import csv
import io
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from app import CHANNEL_COUNT, QtWidgets, TestbenchWindow


class TestTestbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = TestbenchWindow()
        self.window.show()
        self.application.processEvents()

    def tearDown(self):
        self.window.timer.stop()
        self.window.port_timer.stop()
        self.window.request_timer.stop()
        self.window.close()

    def test_one_channel_and_seven_reserved(self):
        window = self.window
        window.tick()
        self.assertEqual(CHANNEL_COUNT, 8)
        self.assertEqual(len(window.curves), 8)
        for index, check in enumerate(window.channel_checks):
            check.setChecked(False)
            self.assertFalse(window.curves[index].isVisible())
            check.setChecked(True)
            self.assertEqual(window.curves[index].isVisible(), index == 0)
            self.assertEqual(check.isEnabled(), index == 0)
            self.assertEqual(len(window.values[index]), 40 if index == 0 else 0)

    def test_hotplug_and_selection(self):
        window = self.window
        first = SimpleNamespace(device="COM41", description="First")
        second = SimpleNamespace(device="COM42", description="Second")
        with patch("app.list_ports.comports", return_value=[]):
            window.refresh_ports()
            self.assertIsNone(window.port.currentData())
        with patch("app.list_ports.comports", return_value=[first]):
            window.refresh_ports()
            self.assertEqual(window.port.currentData(), "COM41")
        with patch("app.list_ports.comports", return_value=[first, second]):
            window.refresh_ports()
            self.assertEqual(window.port.currentData(), "COM41")
            window.port.setCurrentIndex(1)
            window.refresh_ports()
            self.assertEqual(window.port.currentData(), "COM42")
        with patch("app.list_ports.comports", return_value=[first]):
            window.refresh_ports()
            self.assertEqual(window.port.currentData(), "COM41")
        self.assertTrue(window.port_timer.isActive())

    def test_pause_and_clear(self):
        window = self.window
        window.toggle_run()
        self.assertTrue(window.timer.isActive())
        window.tick()
        before = window.curves[0].yData.copy()
        window.set_pause(True)
        window.tick()
        self.assertEqual(len(window.times), 80)
        np.testing.assert_array_equal(before, window.curves[0].yData)
        window.set_pause(False)
        self.assertEqual(len(window.curves[0].yData), 80)
        window.toggle_run()
        self.assertFalse(window.timer.isActive())
        window.clear_data()
        self.assertFalse(window.times)
        self.assertTrue(all(not buffer for buffer in window.values))

    def test_csv_contains_level_and_millivolts(self):
        window = self.window
        window.tick()
        window.channel_checks[7].setChecked(False)
        stream = io.StringIO()
        with patch("app.Path.open") as open_file:
            open_file.return_value.__enter__.return_value = stream
            window.write_csv("unused.csv")
        rows = list(csv.reader(io.StringIO(stream.getvalue()), delimiter=";"))
        self.assertEqual(len(rows), 41)
        self.assertEqual(rows[0][-1], "ch1_level_mV")
        self.assertEqual(len(rows[0]), 4)
        self.assertEqual(int(rows[1][2]), window.values[0][0])
        self.assertAlmostEqual(float(rows[1][3]), window.values[0][0] * 0.6105, places=4)

    def test_buffer_is_bounded(self):
        window = self.window
        window.set_pause(True)
        for _ in range(1501):
            window.tick()
        self.assertEqual(len(window.times), 60000)
        self.assertEqual(len(window.values[0]), 60000)
        self.assertTrue(all(not buffer for buffer in window.values[1:]))

    def test_device_reply_and_error(self):
        window = self.window
        window.source = "device"
        window.reset_protocol()
        window.pending = dict(code=25)
        # Command 25 decimal = 0x19; level 0x1234; CRC = 0x5F.
        window.receive_data(b"!193412\x5f\r", window.session_start + 1)
        self.assertIsNone(window.pending)
        self.assertEqual(window.values[0][-1], 0x1234)
        window.receive_data(b"!193412\x00\r", window.session_start + 2)
        self.assertEqual(len(window.times), 1)
        window.hex_commands.setChecked(True)
        window.receive_data(b"!2534126B\r", window.session_start + 3)
        self.assertEqual(len(window.times), 2)
        window.pending = dict(code=0x25, deadline=0)
        window.check_requests()
        self.assertIsNone(window.pending)
        self.assertFalse(window.poll.isChecked())

    def test_defaults(self):
        self.assertEqual(self.window.baud.currentText(), "9600")
        self.assertFalse(self.window.hex_commands.isChecked())
        self.assertFalse(self.window.poll.isChecked())

    def test_demo_fills_embedded_log_without_device_capture(self):
        window = self.window
        capture = io.StringIO()
        window.log_file = capture
        try:
            for hex_numbers, mode in [(False, "raw"), (True, "hex")]:
                window.hex_commands.setChecked(hex_numbers)
                window.crc_mode.setCurrentIndex(window.crc_mode.findData(mode))
                window.log_view.clear()
                capture.seek(0)
                capture.truncate()
                for _ in range(5):
                    window.tick()
                lines = window.log_view.toPlainText().splitlines()
                self.assertEqual(len(lines), 2)
                self.assertIn("[ДЕМО] TX", lines[0])
                self.assertIn("[ДЕМО] RX", lines[1])
                self.assertEqual(capture.getvalue(), "")
                self.assertTrue(window.log_view.isVisible())
                self.assertIs(window.log_view.window(), window)
                self.assertIsNone(window.worker)
            window.set_pause(True)
            window.log_view.clear()
            for _ in range(5):
                window.tick()
            self.assertIn("[ДЕМО] RX", window.log_view.toPlainText())
        finally:
            window.log_file = None

    def test_connection_capture_and_no_automatic_requests(self):
        window = self.window
        class Capture(io.StringIO):
            def close(self):
                self.was_closed = True

        capture = Capture()
        window.port.addItem("Loopback test", "loop://")
        window.port.setCurrentIndex(window.port.count() - 1)
        with patch("app.Path.open", return_value=capture):
            window.toggle_connection()
        deadline = time.monotonic() + 3
        while not window.device_ready and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertTrue(window.device_ready)
        self.assertNotIn('"direction": "TX"', capture.getvalue())
        window.query(5)
        while '"direction": "RX"' not in capture.getvalue() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertIn('"hex": "26 30 35 05 0D"', capture.getvalue())
        window.worker.send(b"!050102\x08\r")
        while window.pending is not None and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertIsNone(window.pending)
        self.assertIn("1.2", window.device_info.text())
        window.toggle_connection()
        while window.worker is not None and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertIsNone(window.worker)
        self.assertTrue(capture.was_closed)


if __name__ == "__main__":
    unittest.main()
