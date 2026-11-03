import csv
import io
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from app import CHANNEL_COUNT, QtWidgets, TestbenchWindow


class TestTestbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = TestbenchWindow(swd_auto_start=False)
        self.window.battery_filter_check.setChecked(False)
        self.window.receive_mode.setCurrentIndex(self.window.receive_mode.findData("protocol"))
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

    def test_legend_is_visible_without_swd_at_start_and_for_both_firmwares(self):
        window = self.window
        legend = window.plot.plotItem.legend
        self.assertTrue(legend.isVisible())
        self.assertEqual([label.text for _, label in legend.items], ["CH1"])
        for mode in ("adc_stream", "fw018"):
            window.receive_mode.setCurrentIndex(window.receive_mode.findData(mode))
            window.source = "device"
            window.buffer_format = mode
            window.configure_channels(mode == "adc_stream")
            self.application.processEvents()
            self.assertFalse(window.read_thresholds.isChecked())
            self.assertTrue(legend.isVisible())
            self.assertEqual(len(legend.items), 3 if mode == "adc_stream" else 1)
            self.assertGreater(legend.boundingRect().width(), 0)
            self.assertGreater(legend.boundingRect().height(), 0)
            self.assertGreater(legend.zValue(), window.threshold_view.zValue())
            window.clear_data()
            self.application.processEvents()
            self.assertTrue(legend.isVisible())
            self.assertEqual(len(legend.items), 3 if mode == "adc_stream" else 1)

    def working_window(self):
        window = self.window
        window.receive_mode.setCurrentIndex(window.receive_mode.findData("fw018"))
        window.source = "device"
        window.buffer_format = "fw018"
        window.configure_channels()
        window.worker = Mock()
        window.worker.isInterruptionRequested.return_value = False
        window.device_opened()
        self.addCleanup(setattr, window, "worker", None)
        return window

    def test_working_connection_poll_thresholds_and_csv(self):
        window = self.working_window()
        self.assertEqual(window.baud.currentText(), "19200")
        self.assertEqual(window.crc_mode.currentData(), "hex")
        self.assertFalse(window.hex_commands.isEnabled())
        window.worker.send.assert_called_with(b"&0505\r")
        self.assertTrue(window.poll.isChecked())
        window.receive_data(b"!05001217\r", window.session_start + 1)
        window.check_requests()
        window.worker.send.assert_called_with(b"&0F0F\r")
        window.receive_data(b"!0F0413\r", window.session_start + 1.1)
        self.assertIn("ступень: 4", window.gain_info.text())
        window.last_poll = 0
        window.check_requests()
        window.worker.send.assert_called_with(b"&0B0B\r")
        window.receive_data(b"!0BEFFF2C", window.session_start + 2)
        self.assertFalse(window.times)
        window.receive_data(b"010127\r", window.session_start + 2)
        self.assertEqual(list(window.values[0]), [-17])
        self.assertTrue(all(not values for values in window.values[1:]))
        self.assertEqual(window.trigger_lines[0].value(), 300)
        self.assertFalse(window.release_lines[0].isVisible())
        self.assertFalse(window.battery_axis.isVisible())
        self.assertIn("UART", window.threshold_info.text())
        self.assertFalse(window.read_thresholds.isChecked())
        window.receive_data(b"!1905001E\r", window.session_start + 3)
        self.assertEqual(len(window.times), 1)  # Background is not a raw ADC sample.
        window.receive_data(b"!0BEFFF2C0101FF\r", window.session_start + 4)
        self.assertEqual(len(window.times), 1)
        stream = io.StringIO()
        with patch("app.Path.open") as open_file:
            open_file.return_value.__enter__.return_value = stream
            window.write_csv("unused.csv")
        rows = list(csv.reader(io.StringIO(stream.getvalue()), delimiter=";"))
        self.assertEqual(rows[0], ["source", "time_s", "processed_signal", "trigger_threshold", "device_state"])
        self.assertEqual(rows[1][2:], ["-17", "300", "1"])

    def test_gain_write_is_explicit_and_read_back_even_without_ack(self):
        window = self.working_window()
        window.pending = None
        window.gain_read_needed = False
        window.gain_buttons[7].click()
        window.worker.send.assert_called_with(b"&2D0835\r")
        self.assertEqual(window.pending["code"], 45)
        window.data_transmitted(b"&2D0835\r", time.monotonic() - 0.3)
        window.check_requests()
        window.worker.send.assert_called_with(b"&0F0F\r")
        window.receive_data(b"!0F0817\r", window.session_start + 1)
        self.assertIn("подтверждено", window.gain_info.text())
        window.gain_buttons[7].click()
        window.receive_data(b"!2D2D\r", window.session_start + 2)
        window.check_requests()
        window.receive_data(b"!0F0413\r", window.session_start + 3)
        self.assertIn("НЕ совпало", window.gain_info.text())
        window.gain_buttons[3].click()
        window.receive_data(b"?2D0330\r", window.session_start + 4)
        self.assertIn("отклонена", window.gain_info.text())
        window.receive_mode.setCurrentIndex(window.receive_mode.findData("adc_stream"))
        count = window.worker.send.call_count
        window.select_gain(8)
        self.assertEqual(window.worker.send.call_count, count)
        self.assertTrue(all(not button.isEnabled() for button in window.gain_buttons))

    def test_gain_buttons_remain_available_during_poll_and_latest_choice_wins(self):
        window = self.working_window()
        window.pending = None
        window.gain_read_needed = False
        window.query(11)
        self.assertTrue(all(button.isEnabled() for button in window.query_buttons + window.gain_buttons))
        count = window.worker.send.call_count
        window.gain_buttons[1].click()
        window.gain_buttons[6].click()
        self.assertEqual(window.worker.send.call_count, count)
        self.assertEqual(window.gain_button_group.checkedId(), 7)
        self.assertEqual(sum(button.isChecked() for button in window.gain_buttons), 1)
        window.receive_data(b"!0BEFFF2C010127\r", window.session_start + 1)
        window.check_requests()
        window.worker.send.assert_called_with(b"&2D0734\r")
        window.gain_buttons[4].click()  # New choice during write; first verify step 7.
        window.receive_data(b"!2D2D\r", window.session_start + 2)
        window.check_requests()
        window.worker.send.assert_called_with(b"&0F0F\r")
        window.receive_data(b"!0F0716\r", window.session_start + 3)
        self.assertEqual(window.gain_button_group.checkedId(), 5)
        window.check_requests()
        window.worker.send.assert_called_with(b"&2D0532\r")
        self.assertTrue(all(button.isEnabled() for button in window.query_buttons + window.gain_buttons))

    def test_poll_timeout_cancels_queued_gain_without_writing(self):
        window = self.working_window()
        window.gain_read_needed = False
        window.gain_buttons[7].click()
        window.pending["deadline"] = 0
        count = window.worker.send.call_count
        window.check_requests()
        self.assertIsNone(window.queued_gain)
        self.assertEqual(window.worker.send.call_count, count)
        self.assertFalse(window.poll.isChecked())

    def test_cable_diagnostics_updates_from_poll_even_when_plot_is_paused(self):
        window = self.working_window()
        window.set_pause(True)
        stamp = window.session_start
        window.receive_data(b"!0B000035000444\r", stamp + 1)
        self.assertIn("ОБРЫВ КАБЕЛЯ", window.device_state_info.text())
        window.receive_data(b"!0B000035000040\r", stamp + 2)
        self.assertIn("не обнаружены", window.device_state_info.text())
        window.receive_data(b"!0B000035002060\r", stamp + 3)
        self.assertIn("КЗ КАБЕЛЯ", window.device_state_info.text())
        window.receive_data(b"!0B000035000545\r", stamp + 4)
        self.assertIn("ОБРЫВ КАБЕЛЯ", window.device_state_info.text())
        self.assertIn("тревога", window.device_state_info.text())
        window.receive_data(b"!0B0000350000FF\r", stamp + 5)
        self.assertIn("ОБРЫВ КАБЕЛЯ", window.device_state_info.text())  # Bad CRC cannot clear a fault.
        window.last_state_stamp = time.monotonic() - 3
        window.pending = None
        window.poll.setChecked(False)
        window.gain_read_needed = False
        window.check_requests()
        self.assertIn("нет свежего состояния", window.device_state_info.text())
        window.receive_data(b"!030407\r", stamp + 6)
        self.assertIn("ОБРЫВ КАБЕЛЯ", window.device_state_info.text())
        window.receive_data(b"?0B030E\r", stamp + 7)
        self.assertIn("отклонил", window.device_state_info.text())
        window.configure_channels(True)
        self.assertIn("тестовая прошивка не передаёт", window.device_state_info.text())

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
        self.window.receive_mode.setCurrentIndex(self.window.receive_mode.findData("adc_stream"))
        self.assertEqual(self.window.baud.currentText(), "19200")
        self.assertFalse(self.window.crc_mode.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.query_buttons))
        self.window.receive_mode.setCurrentIndex(self.window.receive_mode.findData("protocol"))
        self.assertEqual(self.window.baud.currentText(), "9600")
        self.assertFalse(self.window.hex_commands.isChecked())
        self.assertFalse(self.window.poll.isChecked())

    def test_battery_filter_switch_preserves_raw_data_and_exports_both(self):
        window = self.window
        window.source = "device"
        window.buffer_format = "adc_stream"
        window.configure_channels(True)
        window.battery_filter_check.setChecked(True)
        stamp = window.session_start
        codes = [3000] * 23 + [3700] * 210 + [4000] * 23
        for index, code in enumerate(codes[:255]):
            window.add_device_sample((600, 1800, code), stamp + index / 57)
        self.assertIn("набор 255/256", window.battery_readout.text())
        window.add_device_sample((600, 1800, codes[-1]), stamp + 255 / 57)
        self.assertEqual(window.curves[2].yData[-1], 3700)
        self.assertEqual(window.values[2][-1], 4000)
        window.battery_filter_check.setChecked(False)
        self.assertEqual(window.curves[2].yData[-1], 4000)
        window.battery_filter_check.setChecked(True)
        self.assertEqual(window.curves[2].yData[-1], 3700)
        stream = io.StringIO()
        with patch("app.Path.open") as open_file:
            open_file.return_value.__enter__.return_value = stream
            window.write_csv("unused.csv")
        rows = list(csv.reader(io.StringIO(stream.getvalue()), delimiter=";"))
        self.assertEqual(rows[1][-2:], ["", ""])
        self.assertEqual(rows[-1][4:6], ["4000", "3700.0000"])
        window.clear_data()
        self.assertFalse(window.filtered_battery_values)
        self.assertIsNone(window.battery_filter.value)

    def test_threshold_refresh_is_visible_when_values_are_unchanged(self):
        from swd import ThresholdSnapshot

        window = self.window
        window.read_thresholds.blockSignals(True)
        window.read_thresholds.setChecked(True)
        window.read_thresholds.blockSignals(False)
        window.source = "device"
        window.buffer_format = "adc_stream"
        window.configure_channels(True)
        snapshot = ThresholdSnapshot(1.5, 5.0129, 52.5065, 47.5065)
        window.show_thresholds(snapshot)
        self.assertTrue(window.threshold_axis.isVisible())
        self.assertAlmostEqual(window.trigger_lines[0].value(), 52.5065)
        self.assertAlmostEqual(window.trigger_lines[1].value(), -52.5065)
        self.assertAlmostEqual(window.release_lines[0].value(), 47.5065)
        self.assertAlmostEqual(window.release_lines[1].value(), -47.5065)
        self.assertIn("чтение №1", window.threshold_info.text())
        window.show_thresholds(snapshot)
        self.assertIn("чтение №2", window.threshold_info.text())
        self.assertIn("Обновлено", window.threshold_info.text())
        self.assertIn("±52.5065", window.threshold_info.text())
        window.set_pause(True)
        window.show_thresholds(ThresholdSnapshot(10, 6, 53, 48))
        self.assertAlmostEqual(window.trigger_lines[0].value(), 52.5065)
        window.set_pause(False)
        self.assertEqual(window.trigger_lines[0].value(), 53)
        window.thresholds_failed("Нет доступа")
        self.assertFalse(window.threshold_axis.isVisible())
        self.assertFalse(window.threshold_view.isVisible())
        self.assertNotIn("52.5065", window.threshold_info.text())
        self.assertTrue(window.read_thresholds.isChecked())

    def test_thresholds_start_automatically_with_adc_connection(self):
        window = self.window
        window.receive_mode.setCurrentIndex(window.receive_mode.findData("adc_stream"))
        window.swd_auto_start = True
        window.worker = Mock()
        window.worker.isInterruptionRequested.return_value = False
        try:
            with patch("app.ThresholdWorker") as worker_class:
                window.device_opened()
                self.assertTrue(window.read_thresholds.isChecked())
                worker_class.return_value.start.assert_called_once()
        finally:
            window.worker = None
            window.threshold_worker = None

    def test_adc_stream_connection_plot_and_csv(self):
        window = self.window
        window.receive_mode.setCurrentIndex(window.receive_mode.findData("adc_stream"))
        window.port.addItem("Loopback test", "loop://")
        window.port.setCurrentIndex(window.port.count() - 1)
        window.toggle_connection()
        deadline = time.monotonic() + 3
        while not window.device_ready and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertTrue(window.device_ready)
        self.assertFalse(window.receive_mode.isEnabled())
        self.assertFalse(window.poll.isEnabled())
        window.query(25)
        self.assertIsNone(window.pending)
        self.assertTrue(window.worker.outgoing.empty())
        stamp = window.session_start
        window.receive_data(b"A0: 0001; A1: 0800; A2:", stamp + 1)
        self.assertFalse(window.times)
        window.receive_data(b" 0FFF;\rA0: 0002; A1: 0801; A2: 0FFE;\r", stamp + 2)
        self.assertEqual([list(buffer) for buffer in window.values[:3]], [[1, 2], [2048, 2049], [4095, 4094]])
        for index in range(3):
            self.assertTrue(window.channel_checks[index].isEnabled())
            np.testing.assert_array_equal(window.curves[index].yData, window.values[index])
        self.assertFalse(any(check.isEnabled() for check in window.channel_checks[3:]))
        self.assertEqual(window.ch2_mv_axis.tickStrings([0, 2047.5, 4095], 1, 100),
                         ["0.0", "1650.0", "3300.0"])
        self.assertIs(window.ch2_mv_axis.linkedView(), window.plot.getViewBox())
        self.assertIn("2049 код / 1651.2 мВ", window.ch2_readout.text())
        window.channel_checks[1].setChecked(False)
        self.assertFalse(window.ch2_mv_axis.isVisible())
        window.channel_checks[1].setChecked(True)
        self.assertTrue(window.ch2_mv_axis.isVisible())
        self.assertEqual(window.battery_axis.tickStrings([0, 4095], 1, 100), ["0.000", "5.020"])
        self.assertIs(window.battery_axis.linkedView(), window.plot.getViewBox())
        self.assertIn("5.019 В", window.battery_readout.text())
        self.assertEqual([label.text() for label in window.stat_ch3_labels],
                         ["CH3 5.019 В", "CH3 5.019 В", "CH3 5.020 В", "CH3 0.001 В", "CH3 0.001 В"])
        window.channel_checks[2].setChecked(False)
        self.assertFalse(window.battery_axis.isVisible())
        window.channel_checks[2].setChecked(True)
        self.assertTrue(window.battery_axis.isVisible())
        self.assertIn("без CRC", window.device_info.text())
        self.assertIn("A2: 4094", window.log_view.toPlainText())
        self.assertNotIn("CRC не распознаны", window.log_view.toPlainText())
        stream = io.StringIO()
        with patch("app.Path.open") as open_file:
            open_file.return_value.__enter__.return_value = stream
            window.write_csv("unused.csv")
        rows = list(csv.reader(io.StringIO(stream.getvalue()), delimiter=";"))
        self.assertEqual(rows[0], ["source", "time_s", "a0_kche_integrity_code",
                                  "a1_alarm_processing_code", "a2_battery_supply_code",
                                  "a2_trimmed256_code", "battery_trimmed256_V"])
        self.assertEqual(rows[2][2:5], ["2", "2049", "4094"])
        window.set_pause(True)
        window.receive_data(b"A0: 0003; A1: 0004; A2: 0005;\r\n", stamp + 63)
        self.assertEqual([list(buffer) for buffer in window.values[:3]], [[3], [4], [5]])
        window.set_pause(False)
        self.assertEqual(len(window.curves[2].yData), 1)
        window.toggle_connection()
        while window.worker is not None and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertIsNone(window.worker)
        self.assertTrue(window.receive_mode.isEnabled())
        window.toggle_run()
        window.tick()
        self.assertFalse(window.channel_checks[1].isEnabled())
        self.assertFalse(window.values[1])
        self.assertFalse(window.ch2_mv_axis.isVisible())
        self.assertFalse(window.battery_axis.isVisible())

    def test_log_decodes_fragmented_and_combined_replies(self):
        window = self.window
        window.reset_protocol()
        window.record("TX", b"&05\x05\r", 0)
        self.assertIn("запрос версии ПО", window.log_view.toPlainText())
        window.record("RX", b"!0501", 0)
        self.assertIn("ожидается продолжение", window.log_view.toPlainText().splitlines()[-1])
        window.record("RX", b"02\x08\r!0301\x04\r", 0)
        line = window.log_view.toPlainText().splitlines()[-1]
        self.assertIn("версия 1.2", line)
        self.assertIn("тревога", line)
        self.assertIn("CRC raw: OK", line)
        window.record("RX", b"!050102FF\r", 0)
        self.assertNotIn("версия 1.2", window.log_view.toPlainText().splitlines()[-1])

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
                self.assertIn("[ДЕМО]", lines[0])
                self.assertIn(" TX ", lines[0])
                self.assertIn("[ДЕМО]", lines[1])
                self.assertIn(" RX ", lines[1])
                self.assertIn("запрос текущего уровня CH1", lines[0])
                self.assertIn("ответ: уровень", lines[1])
                self.assertIn("мВ", lines[1])
                self.assertEqual(capture.getvalue(), "")
                self.assertTrue(window.log_view.isVisible())
                self.assertIs(window.log_view.window(), window)
                self.assertIsNone(window.worker)
            window.set_pause(True)
            window.log_view.clear()
            for _ in range(5):
                window.tick()
            self.assertIn(" RX ", window.log_view.toPlainText())
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
