import csv
import io
import os
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
        self.window.close()

    def test_eight_channels_and_visibility(self):
        window = self.window
        window.tick()
        self.assertEqual(CHANNEL_COUNT, 8)
        self.assertEqual(len(window.curves), 8)
        for index, check in enumerate(window.channel_checks):
            check.setChecked(False)
            self.assertFalse(window.curves[index].isVisible())
            check.setChecked(True)
            self.assertTrue(window.curves[index].isVisible())
            self.assertEqual(len(window.values[index]), 40)

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

    def test_csv_includes_hidden_channels(self):
        window = self.window
        window.tick()
        window.channel_checks[7].setChecked(False)
        stream = io.StringIO()
        with patch("app.Path.open") as open_file:
            open_file.return_value.__enter__.return_value = stream
            window.write_csv("unused.csv")
        rows = list(csv.reader(io.StringIO(stream.getvalue()), delimiter=";"))
        self.assertEqual(len(rows), 41)
        self.assertEqual(rows[0][-1], "adc8_code")
        self.assertEqual(len(rows[0]), 10)
        self.assertEqual(int(rows[1][-1]), window.values[7][0])

    def test_buffer_is_bounded(self):
        window = self.window
        window.set_pause(True)
        for _ in range(1501):
            window.tick()
        self.assertEqual(len(window.times), 60000)
        self.assertTrue(all(len(buffer) == 60000 for buffer in window.values))


if __name__ == "__main__":
    unittest.main()
