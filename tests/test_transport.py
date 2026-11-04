import time
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore
from transport import SerialWorker


class TestTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    def test_loopback_and_clean_stop(self):
        received = []
        sent = []
        opened = []
        errors = []
        worker = SerialWorker("loop://", 9600)
        worker.opened.connect(lambda: opened.append(True))
        worker.received.connect(lambda data, stamp: received.append(data))
        worker.transmitted.connect(lambda data, stamp: sent.append(data))
        worker.failed.connect(errors.append)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not opened and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertTrue(opened)
            worker.send(b"&05\x05\r")
            while len(b"".join(received)) < 5 and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
            self.assertEqual(b"".join(sent), b"&05\x05\r")
            self.assertEqual(b"".join(received), b"&05\x05\r")
            self.assertFalse(errors)
        finally:
            worker.requestInterruption()
            self.assertTrue(worker.wait(1500))

    def test_selected_serial_format_is_passed_to_driver(self):
        worker = SerialWorker("loop://", 19200, bytesize=7, parity="E", stopbits=1.5)
        connection = Mock()
        with patch("transport.serial.serial_for_url", return_value=connection) as factory, \
                patch.object(worker, "isInterruptionRequested", return_value=True):
            worker.run()
        self.assertEqual(factory.call_args.kwargs["bytesize"], 7)
        self.assertEqual(factory.call_args.kwargs["parity"], "E")
        self.assertEqual(factory.call_args.kwargs["stopbits"], 1.5)
        connection.open.assert_called_once()
        connection.close.assert_called_once()
